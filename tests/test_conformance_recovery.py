import struct
import pytest
from oep_client.conformance_recovery import RecoveryChecks
from oep_client.conformance_recovery_sample import SampleRecoveryAdapter, SampleRecoveryHost
from test_conformance_retention_lifecycle import build


def setup():
    now, model, _, runner = build()
    adapter = SampleRecoveryAdapter(model, clock_ms=lambda: now[0], wait_ms=lambda ms: now.__setitem__(0, now[0]+ms))
    runner.c.send = lambda req: adapter.exchange(adapter.primary, req)
    return now, model, adapter, RecoveryChecks(runner.c, adapter)


def test_recovery_passes():
    _, model, _, runner = setup()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'],x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 22
    assert len(report['recovery_decisions']) == 10
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('defect', ['retry_new_corr', 'infer_success', 'infer_failure', 'bind_ambiguous', 'ignore_boot', 'ignore_session'])
def test_recovery_host_mutants_fail(defect):
    _, _, _, runner = setup()
    recover = runner.host.recover
    def altered(sid, boot, before, **kwargs):
        if defect == 'retry_new_corr' and kwargs['action'] == 'create':
            runner.c.success(runner.create(sid))
        result = recover(sid, boot, before, **kwargs)
        if defect == 'infer_success' and result['status'] == 'observed-added': result['original_outcome'] = 'success'
        if defect == 'infer_failure' and result['status'] == 'unchanged-state': result['original_outcome'] = 'rejected'
        if defect == 'bind_ambiguous' and result['status'] == 'ambiguous-state': result['status'] = 'observed-added'
        if defect == 'ignore_boot' and result['status'] == 'boot-changed': result.update(status='unchanged-state',inventory={})
        if defect == 'ignore_session' and result['status'] == 'session-unavailable': result.update(status='observed-absent',inventory={})
        return result
    runner.host.recover = altered
    report = runner.run()
    assert report['status'] == 'failed'
    assert any(x['id'].startswith('CORE-RECOVERY-') and x['status']=='failed' for x in report['checks'])


@pytest.mark.parametrize('invalid', ['physical', 'loss', 'restart', 'delivered'])
def test_loss_controls_rejected(invalid):
    _, model, adapter, runner = setup()
    if invalid == 'physical': adapter.reset_scope = 'physical'
    elif invalid == 'loss': adapter.discard_reply = None
    elif invalid == 'restart': adapter.restart = None
    else:
        adapter.discard_reply = lambda req: adapter.exchange(adapter.primary, req)
    report = runner.run()
    assert report['status']=='failed'
    if invalid != 'delivered': assert model.ep.current_core.last is None


def test_uncontrolled_single_addition_stays_ambiguous():
    _, _, _, runner = setup()
    runner.c.confirm(); runner.c.identity()
    with runner.c.holding(60000) as sid:
        boot, before = runner.c.boot, runner.host.inventory()
        request = runner.create(sid,size=17); runner.lose(request)
        runner.replay_unchanged(request,reason='result_lost')
        result = runner.recover(sid,boot,before,'ambiguous-state',single_writer=False)
        assert result['candidate_resource_id'] is None


def test_loss_waits_before_retry_and_never_returns_original_result():
    now, _, adapter, runner = setup()
    runner.c.confirm(); runner.c.identity()
    with runner.c.holding(60000) as sid:
        request = runner.create(sid, size=17)
        before = now[0]
        assert runner.lose(request) is None
        assert now[0] - before >= 1050
        losses = [x for x in adapter.trace if x['method'] == 'discard_reply']
        assert losses[-1]['delivered_to_host'] is False
        assert bytes.fromhex(losses[-1]['discarded_response_hex'])[3:5] == b'\x01\0'
        assert not any(x['request_hex'] == request.hex() for x in runner.c.trace)


def test_wait_that_does_not_advance_is_rejected():
    _, _, adapter, runner = setup()
    adapter.wait_ms = lambda ms: None
    report = runner.run()
    assert report['status'] == 'failed'


def test_boot_change_during_inventory_discards_readback():
    _, _, adapter, runner = setup()
    runner.c.confirm(); runner.c.identity()
    with runner.c.holding(60000) as sid:
        boot, before = runner.c.boot, runner.host.inventory()
        runner.lose(runner.create(sid, size=17))
        inventory = runner.host.inventory
        def raced():
            value = inventory()
            adapter.restart(boot ^ 1); runner.c.session = None
            return value
        runner.host.inventory = raced
        result = runner.host.recover(sid,boot,before,action='create',slot=(1,1),single_writer=True)
        assert result == {'status':'boot-changed','original_outcome':'unknown','inventory':None}


def test_expiry_during_inventory_discards_readback():
    now, _, _, runner = setup()
    runner.c.confirm(); runner.c.identity()
    with runner.c.holding(1000) as sid:
        runner.created_one(runner.create(sid))
        boot, before = runner.c.boot, runner.host.inventory()
        inventory = runner.host.inventory
        def raced():
            value = inventory()
            now[0] += 1050; runner.c.session = None
            return value
        runner.host.inventory = raced
        result = runner.host.recover(sid,boot,before,action='create',slot=(1,1),single_writer=True)
        assert result == {'status':'session-unavailable','original_outcome':'unknown','inventory':None}


def test_reboot_reusing_resource_number_does_not_bind_old_operation():
    from oep_client.conformance import Checks
    _, model, adapter, runner = setup()
    runner.c.confirm(); runner.c.identity()
    with runner.c.holding(60000) as sid:
        boot, before = runner.c.boot, runner.host.inventory()
        runner.lose(runner.create(sid, size=17))
        assert set(model.state()['resources']) == {1}
        adapter.restart(boot ^ 1); runner.c.session = None
        other = Checks(lambda req: adapter.exchange(adapter.primary, req), runner.c.reg, 'virtual-retention-1')
        other.confirm(); other.identity()
        replacement = sid ^ 0xffffffff or 1
        other.success(other.request(other.core['open'], struct.pack('<IB',60000,0),session=replacement))
        try:
            payload = other.success(other.request(16,b'\x01\x07',fn=1,session=replacement))
            assert struct.unpack('<H',payload)[0] == 1
            result = runner.host.recover(sid,boot,before,action='create',slot=(1,1),single_writer=True)
            assert result == {'status':'boot-changed','original_outcome':'unknown','inventory':None}
            assert set(model.state()['resources']) == {1}
        finally:
            other.success(other.request(other.core['end'],session=replacement))
