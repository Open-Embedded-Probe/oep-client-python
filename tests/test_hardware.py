"""Equipment input boundaries, connected assignments and actual virtual OEP lifecycle."""
import copy
import json
from pathlib import Path

import pytest

from oep_client import hardware, virtual_bench

HERE = Path(__file__).parent
PAIR = HERE / 'hw/hardware.resolved.example.toml'
VIRTUAL = HERE / 'fixtures/hardware.virtual.toml'


def equipment(data):
    hardware.validate(data)
    return hardware.Equipment(Path('/tmp/equipment/config.toml'), 'test-hash', data)


def test_examples_and_connected_matrix(monkeypatch):
    from oep_client import link
    monkeypatch.setattr(link, 'open_host', lambda *a, **kw: pytest.fail('offline planner opened hardware'))
    pair = hardware.load(PAIR)
    hardware.load(HERE / 'hw/hardware.example.toml')
    result = hardware.plan(pair, 'USB-DATA')
    assert len(result['assignments']) == 1
    assignment = result['assignments'][0]
    assert assignment['roles'] == {'dut': 'target-a', 'peer': 'target-b', 'usb': 'usb-a-b'}
    assert assignment['required_connections'] == {'probe-a': 2}
    assert {'probe:probe-a', 'target:target-a', 'target:target-b', 'power:power-a'} <= set(assignment['resources'])
    assert result['capabilities'] == 'not checked; preflight required'
    disconnected = copy.deepcopy(pair.data)
    disconnected['usb_links'] = []
    assert hardware.plan(equipment(disconnected), 'USB-DATA')['status'] == 'equipment-unavailable'


def test_channel_collision_cannot_become_usb_pair():
    data = copy.deepcopy(hardware.load(PAIR).data)
    data['debug_links'][1]['channels'] = data['debug_links'][0]['channels'].copy()
    assert hardware.plan(equipment(data), 'USB-DATA')['assignments'] == []


def test_two_probes_can_control_one_usb_pair():
    data = copy.deepcopy(hardware.load(PAIR).data)
    second = copy.deepcopy(data['probes'][0])
    second['id'] = 'probe-b'
    second['identity']['value'] = 'OTHER-PROBE'
    data['probes'].append(second)
    data['debug_links'][1]['probe'] = second['id']
    data['power_domains'][0]['members'].append(second['id'])
    assignment = hardware.plan(equipment(data), 'USB-DATA')['assignments'][0]
    assert assignment['required_connections'] == {'probe-a': 1, 'probe-b': 1}


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(schema_version=True),
    lambda d: d.update(generation=True),
    lambda d: d.update(kind=[]),
    lambda d: d.update(extra_typo='ignored?'),
    lambda d: d.update(confirmed_at='2026-10-09T00:00:00'),
    lambda d: d['debug_links'][0].update(target='missing'),
    lambda d: d['debug_links'][0].update(state='candidate'),
    lambda d: d['debug_links'][0]['channels'].update(data=True),
    lambda d: d['targets'][0]['identity'].update(reason=''),
    lambda d: d['targets'].append(copy.deepcopy(d['targets'][0])),
    lambda d: d['usb_links'][0].update(vbus_source='target-b'),
    lambda d: d['signal_nets'][0]['endpoints'][0].update(channel=3),
])
def test_invalid_configuration_is_error_not_skip(mutate):
    data = copy.deepcopy(hardware.load(PAIR).data)
    mutate(data)
    with pytest.raises(hardware.ConfigurationError):
        equipment(data)


def test_explicit_selection_does_not_fall_back():
    pair = hardware.load(PAIR)
    with pytest.raises(hardware.ConfigurationError, match='unknown selector'):
        hardware.plan(pair, probes=['absent'])
    with pytest.raises(hardware.ConfigurationError, match='no matching'):
        hardware.plan(pair, 'USB-DATA', links=['control-a'])
    assignment = hardware.plan(pair, 'TOOL-FLASH', targets=['target-b'])['assignments']
    assert len(assignment) == 1 and assignment[0]['roles']['dut'] == 'target-b'
    with pytest.raises(hardware.ConfigurationError, match='resolved'):
        hardware.plan(hardware.load(HERE / 'hw/hardware.example.toml'))


def test_paths_and_missing_explicit_files(tmp_path, monkeypatch):
    config = hardware.load(VIRTUAL)
    assert config.resolve_path('evidence/result.json') == VIRTUAL.parent / 'evidence/result.json'
    monkeypatch.chdir(tmp_path)
    with pytest.raises(hardware.ConfigurationError):
        hardware.load('hardware.resolved.local.toml')


def test_virtual_smoke_requests_and_session_cleanup(monkeypatch):
    from oep_client import link
    monkeypatch.setattr(link, 'open_host', lambda *a, **kw: pytest.fail('virtual smoke opened hardware'))
    result = hardware.virtual_smoke(hardware.load(VIRTUAL))
    assert result['status'] == 'passed'
    observed = result['probes'][0]
    assert observed['unit_id'] == 'fafe00000003'
    assert observed['firmware']
    assert observed['confirm']['revision'] == 1
    assert observed['session_released'] is True
    assert observed['declarations']


def test_identity_mismatch_fails_with_artifact():
    data = copy.deepcopy(hardware.load(VIRTUAL).data)
    data['probes'][0]['identity']['value'] = 'other-device'
    result = hardware.virtual_smoke(equipment(data))
    assert result['status'] == 'failed'
    assert 'identity' in result['probes'][0]['error']
    assert result['probes'][0]['session_released']


def test_missing_probe_declaration_is_failure(monkeypatch):
    original = virtual_bench.PROFILES['esp32-v003']
    def broken():
        probe = original()
        offered = [virtual_bench.Offered(o.fn, o.instance, o.name,
                   tuple(t for t in o.tlvs if t[0] != virtual_bench.CORE_MAX_OP_MS), inner=o.inner)
                   if o.fn == 0 else o for o in probe.offered]
        return virtual_bench.VirtualProbe(probe.label, probe.max_frame, offered)
    monkeypatch.setitem(virtual_bench.PROFILES, 'esp32-v003', broken)
    result = hardware.virtual_smoke(hardware.load(VIRTUAL))
    assert result['status'] == 'failed'
    assert 'missing' in result['probes'][0]['error']


def test_smoke_refuses_physical_transport_before_requests(monkeypatch):
    from oep_client import endpoint
    monkeypatch.setattr(endpoint, 'Endpoint', lambda *a, **k: pytest.fail('smoke sent a request'))
    with pytest.raises(hardware.ConfigurationError, match='virtual'):
        hardware.virtual_smoke(hardware.load(PAIR))


def test_cli_env_override_and_no_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('OEP_HW_CONFIG', '/absent/config.toml')
    assert hardware.main(['plan', '--config', str(VIRTUAL)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'planned'
    assert hardware.main(['plan']) == 2
    artifact = tmp_path / 'result.json'
    artifact.write_text('previous run')
    assert hardware.main(['smoke', '--config', str(VIRTUAL), '--out', str(artifact)]) == 2
    assert artifact.read_text() == 'previous run'


def test_cleanup_failure_is_preserved(monkeypatch):
    from oep_client import host
    def failed_end(self):
        raise RuntimeError('end transport failure')
    monkeypatch.setattr(host.Host, 'end', failed_end)
    result = hardware.virtual_smoke(hardware.load(VIRTUAL))
    assert result['status'] == 'failed'
    observed = result['probes'][0]
    assert observed['cleanup_error'] == 'end transport failure'
    assert observed['session_released'] is False
    assert result['client_version'] and result['virtual_bench_version'] and result['registry_hash']
