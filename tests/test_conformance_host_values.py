import pytest
from oep_client.conformance_host_values import HostValueChecks
from oep_client.conformance_host_values_sample import decode

SPEC = '/explicit/spec-input'


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    def identity(path):
        assert path == SPEC
        return {'resolutions': {'rejected': 0, 'completed': 1},
                'outcomes': {'success': 0, 'failed': 1, 'partial': 2},
                'status': {'ok': 0, 'wait': 1, 'line': 2, 'fault': 3, 'timeout': 4, 'state': 5},
                'reject_reasons': {'unavailable' if i == 4 else str(i): i for i in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12)}}, {'commit': 'unit-fixture'}
    monkeypatch.setattr('oep_client.conformance_host_values.spec_identity', identity)


def test_selected_unknown_value_contracts_pass():
    report = HostValueChecks(decode, spec=SPEC).run()
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 7 and not report['full_conformance']


@pytest.mark.parametrize('defect,case,prefix', [
    ('resolution_success', 'RESOLUTION', 'Violation:'),
    ('outcome_success', 'OUTCOME', 'Violation:'),
    ('status_success', 'STATUS', 'Violation:'),
    ('change_count', 'STATUS', 'Violation:'),
    ('reason_success', 'REASON', 'Violation:'),
    ('cause_refusal', 'CAUSE', 'Violation:'),
    ('cause_fallback', 'CAUSE', 'Violation:'),
    ('kind_refusal', 'SCAN_KIND', 'Violation:'),
    ('kind_fallback', 'SCAN_KIND', 'Violation:'),
    ('flags_refusal', 'FLAGS', 'Violation:'),
    ('flags_change_limit', 'FLAGS', 'Violation:'),
])
def test_independent_reader_faults(defect, case, prefix):
    def broken(frame, shape):
        if defect == 'cause_refusal' and frame[:5] == b'\x02\x07\x00\x00\x04' and frame[-1] > 6:
            raise ValueError('unknown cause')
        if defect == 'kind_refusal' and shape == 'scan' and frame[7] > 2:
            raise ValueError('unknown kind')
        if defect == 'flags_refusal' and shape == 'confirm' and frame[10]:
            raise ValueError('reserved flags')
        out = decode(frame, shape)
        if defect == 'resolution_success' and frame[3] > 1: out['successful'] = True
        if defect == 'outcome_success' and frame[3] == 1 and frame[4] > 2: out['successful'] = True
        if defect == 'status_success' and out.get('status', 0) > 5: out['successful'] = True
        if defect == 'change_count' and out.get('status', 0) > 5: out['done'] = 0; out['values'] = []
        if defect == 'reason_success' and frame[3] == 0 and frame[4] > 12: out['successful'] = True
        if defect == 'cause_fallback' and out.get('cause', 0) > 6: out['cause'] = 6
        if defect == 'kind_fallback' and shape == 'scan' and out['entries'][0][0] > 2: out['entries'][0][0] = 1
        if defect == 'flags_change_limit' and shape == 'confirm' and frame[10]: out['maximum'] = 64
        return out
    report = HostValueChecks(broken, spec=SPEC).run()
    row = next(r for r in report['checks'] if r['id'] == 'HOST-VALUES-' + case)
    assert row['status'] == 'failed' and row['error'].startswith(prefix), row


def test_unknown_failure_can_be_reported_as_protocol_error():
    def refusing(frame, shape):
        if frame[3] > 1 or frame[3] == 1 and frame[4] > 2:
            raise ValueError('unusable unknown result')
        return decode(frame, shape)
    assert HostValueChecks(refusing, spec=SPEC).run()['status'] == 'passed'


@pytest.mark.parametrize('section,key', [
    ('resolutions', 'completed'), ('outcomes', 'success'),
    ('status', 'ok'), ('reject_reasons', 'unavailable'),
])
def test_changed_selected_meanings_block_before_decoder(monkeypatch, section, key):
    from oep_client import conformance_host_values as module
    from oep_client.conformance import Violation
    original = module.spec_identity
    def changed(path):
        registry, identity = original(path)
        registry[section][key] = 255
        return registry, identity
    monkeypatch.setattr(module, 'spec_identity', changed)
    calls = []
    with pytest.raises(Violation):
        HostValueChecks(lambda *args: calls.append(args), spec=SPEC)
    assert calls == []
