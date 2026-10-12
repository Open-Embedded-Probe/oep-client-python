import copy
from oep_client.conformance import Checks
from oep_client.conformance_retention import RetentionChecks
from oep_client.conformance_lease_sample import SampleLeaseAdapter
from oep_client.virtual_pipeline_model import PipelineModel
from test_conformance import REG


def test_unconfigured_retention_blocks_session_work():
    model = PipelineModel(lambda: 0, boot_id=17)
    adapter = SampleLeaseAdapter(model)
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1')
    report = RetentionChecks(checks, adapter).run()
    assert report['status'] == 'failed'
    assert model.ep.current_core.last is None

from oep_client.virtual_retention_model import RetentionModel
from oep_client.conformance_retention_sample import SampleRetentionAdapter
import pytest
import struct


def build():
    now = [0]
    model = RetentionModel(lambda: now[0], boot_id=17)
    adapter = SampleRetentionAdapter(model, clock_ms=lambda: now[0], wait_ms=lambda ms: now.__setitem__(0,now[0]+ms))
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-retention-1')
    return now, model, adapter, RetentionChecks(checks, adapter)


def test_retention_passes():
    now, model, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'],x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 12
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('defect', ['reexecute', 'boundary', 'too_few', 'high_reset', 'same_open', 'end_clear', 'lost_before_identity'])
def test_retention_mutants_fail(defect):
    now, model, adapter, runner = build()
    core = model.ep.current_core
    handle = core.handle
    def altered(data, transport, *, admission_reason=None, defer_send=False):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', data)
        if defect == 'lost_before_identity' and sid == core.last and corr in core.cache and core.cache[corr][1] is None:
            return struct.pack('<BHBB', 2, corr, 0, 12)
        if defect == 'reexecute' and sid == core.last and corr in core.cache and None in core.cache[corr]:
            core.cache.pop(corr); core.high = corr - 1
        if defect == 'same_open' and fn == 0 and op == 16 and sid == core.holder:
            core.cache.clear(); core.high = 0
        result = handle(data, transport, admission_reason=admission_reason, defer_send=defer_send)
        if defect == 'boundary' and corr in core.cache:
            request, response = core.cache[corr]
            core.cache[corr] = (None if request and len(request)==16 else request,
                                None if response and len(response)==16 else response)
        if defect == 'too_few':
            while len(core.cache) > 2: core.cache.popitem(last=False)
        if defect == 'high_reset' and len(core.cache) == 8: core.high = 0
        if defect == 'end_clear' and fn == 0 and op == 17:
            core.cache.clear(); core.high = 0
        return result
    core.handle = altered
    report = runner.run()
    assert report['status'] == 'failed'
    assert any(x['id'].startswith('CORE-RETENTION-') and x['id']!='CORE-RETENTION-IDENTITY' and
               x['status']=='failed' for x in report['checks'])


@pytest.mark.parametrize('invalid', ['capability', 'geometry', 'allocator', 'unit', 'fn'])
def test_preconditions_block_session_work(invalid):
    now, model, adapter, runner = build()
    if invalid == 'capability': adapter.retention_state = None
    elif invalid == 'geometry': model.ep.remember_max = 15
    elif invalid == 'allocator':
        state = adapter.state
        def missing():
            value = state(); value.pop('next_resource_id'); return value
        adapter.state = missing
    elif invalid == 'unit': runner.c.unit = 'other-unit'
    else: adapter.functions = (0,)
    report = runner.run()
    assert report['status'] == 'failed'
    assert model.ep.current_core.last is None
    assert model.extension.next_id == 1


def test_normal_pipeline_retention_stays_unchanged():
    assert PipelineModel(lambda: 0, boot_id=17).ep.remember_max == 72
    assert RetentionModel(lambda: 0, boot_id=17).ep.remember_max == 16


def test_repeated_session_zero_executes_after_clock_advance():
    now,model,adapter,runner=build()
    runner.c.confirm();runner.c.identity();runner.peer.confirm();runner.peer.identity()
    runner.c.check('CORE-RETENTION-SESSION-ZERO','core §5.2',runner.session_zero)
    assert runner.c.results[-1]['status']=='passed',runner.c.results
    evidence=runner.c.observed['retention_session_zero']
    first=bytes.fromhex(evidence['first_clock_hex']);second=bytes.fromhex(evidence['second_clock_hex'])
    assert int.from_bytes(second[4:12],'little')-int.from_bytes(first[4:12],'little')>=5000000


def test_private_session_zero_reply_cache_is_detected():
    now,model,adapter,runner=build();core=model.ep.current_core;handle=core.handle;cache={}
    def cached(data,transport,**kwargs):
        _,corr,fn,op,sid=struct.unpack_from('<BHHBI',data)
        if sid==0 and fn==0 and op==4 and corr==65535:
            if data not in cache:cache[data]=handle(data,transport,**kwargs)
            return cache[data]
        return handle(data,transport,**kwargs)
    core.handle=cached
    runner.c.confirm();runner.c.identity();runner.peer.confirm();runner.peer.identity()
    runner.c.check('CORE-RETENTION-SESSION-ZERO','core §5.2',runner.session_zero)
    assert runner.c.results[-1]['status']=='failed',runner.c.results
    assert 'deduplicated' in runner.c.results[-1]['error']
