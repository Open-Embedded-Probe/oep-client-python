import copy
from oep_client.conformance import Checks
from oep_client.conformance_retention_lifecycle import RetentionLifecycleChecks
from oep_client.conformance_retention_sample import SampleRetentionAdapter
from oep_client.virtual_retention_model import RetentionModel
from test_conformance import REG


def test_restart_requires_explicit_logical_fixture():
    model = RetentionModel(lambda: 0, boot_id=17)
    adapter = SampleRetentionAdapter(model)
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-retention-1')
    report = RetentionLifecycleChecks(checks, adapter).run()
    assert report['status'] == 'failed'
    assert model.ep.current_core.last is None

from oep_client.conformance_retention_lifecycle_sample import SampleRetentionLifecycleAdapter
import pytest
import struct


def build():
    now = [0]
    model = RetentionModel(lambda: now[0], boot_id=17)
    adapter = SampleRetentionLifecycleAdapter(model, clock_ms=lambda: now[0],
                    wait_ms=lambda ms: now.__setitem__(0, now[0] + ms))
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-retention-1')
    return now, model, adapter, RetentionLifecycleChecks(checks, adapter)


def test_lifecycle_passes():
    now, model, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 19
    assert not report['full_conformance']
    assert model.ep.boot_id == 16 and model.ep.current_core.holder is None


@pytest.mark.parametrize('defect', ['expiry_cache', 'expiry_allocator', 'force_cache', 'force_resources',
                                    'reboot_cache', 'same_boot', 'old_output', 'old_requests'])
def test_lifecycle_mutants_fail(defect):
    now, model, adapter, runner = build()
    runner.c.confirm(); runner.peer.confirm()
    core = model.ep.current_core
    release, handle, restart = core.release, core.handle, model.restart
    forcing = [False]
    if defect.startswith('expiry') or defect == 'force_resources':
        def altered():
            expired = core.holder is not None and model.ep.now() >= core.deadline
            live = model.extension.live.copy()
            release()
            if defect == 'expiry_cache' and expired: core.cache.clear()
            if defect == 'expiry_allocator' and expired: model.extension.next_id = 1
            if defect == 'force_resources' and forcing[0]: model.extension.live.update(live)
        core.release = altered
    if defect.startswith('force'):
        def altered(data, transport, *, admission_reason=None, defer_send=False):
            old = core.cache.copy()
            forcing[0] = data[3:6] == b'\0\0\x10' and len(data)>=15 and data[14] == 1
            try: result = handle(data, transport, admission_reason=admission_reason, defer_send=defer_send)
            finally: forcing[0] = False
            if defect == 'force_cache' and len(data)>=15 and data[14] == 1 and old:
                for key, item in old.items(): core.cache.setdefault(key, item)
            return result
        core.handle = altered
    if defect in ('reboot_cache', 'same_boot', 'old_output', 'old_requests'):
        def altered(boot_id):
            old = core.cache.copy(), core.last, core.high
            requests = model.requests.copy()
            result = restart(boot_id)
            if defect == 'reboot_cache':
                model.ep.current_core.cache, model.ep.current_core.last, model.ep.current_core.high = old
            elif defect == 'same_boot': model.ep.boot_id = 17
            elif defect == 'old_requests': model.requests = requests
            else: model.pipes['primary']['out'].append(b'\x02\x01\0\x01\0')
            return result
        model.restart = altered
    case = (lambda: runner.expiry('request')) if defect.startswith('expiry') else (
           (lambda: runner.force('response')) if defect.startswith('force') else runner.reboot)
    runner.c.check('CORE-LIFECYCLE-MUTANT', 'core §5.2/6/9', case)
    assert runner.c.results[-1]['status'] == 'failed'


@pytest.mark.parametrize('invalid', ['scope', 'restart', 'pending', 'boot'])
def test_controls_block_session_work(invalid):
    now, model, adapter, runner = build()
    if invalid == 'scope': adapter.reset_scope = 'physical'
    elif invalid == 'restart': adapter.restart = None
    elif invalid == 'pending': adapter.pending = None
    else:
        state = adapter.state
        def missing():
            value = state(); value.pop('boot_id'); return value
        adapter.state = missing
    report = runner.run()
    assert report['status'] == 'failed'
    assert model.ep.current_core.last is None and model.extension.next_id == 1
    assert not any(x['method']=='restart' for x in adapter.trace)


def test_restart_rejects_same_or_invalid_boot():
    now, model, adapter, runner = build()
    for value in (17, -1, 0x100000000, True):
        with pytest.raises(ValueError): model.restart(value)
    assert model.ep.boot_id == 17


def test_boot_zero_and_uptime_reset():
    now, model, adapter, runner = build()
    now[0] = 999
    model.restart(0)
    assert model.ep.boot_id == 0 and model.ep.now() == 0
    now[0] += 7
    assert model.ep.now() == 7
