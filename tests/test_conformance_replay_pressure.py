import copy
from oep_client.conformance import Checks
from oep_client.conformance_replay_pressure import ReplayPressureChecks
from oep_client.conformance_pipeline_sample import SamplePipelineAdapter
from oep_client.virtual_pipeline_model import PipelineModel
from test_conformance import REG


def build():
    model = PipelineModel(lambda: 0, boot_id=17)
    adapter = SamplePipelineAdapter(model)
    c = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1')
    return model, adapter, ReplayPressureChecks(c, adapter)


def test_pressure_replay():
    model, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 15
    assert model.ep.current_core.holder is None
    assert not report['full_conformance']

import struct
import pytest


@pytest.mark.parametrize('defect', ['pre_core', 'uncached', 'reexecute', 'renew', 'peer_cache', 'takeover'])
def test_replay_pressure_mutants_fail(defect):
    model, adapter, runner = build()
    core = model.ep.current_core
    handle = core.handle
    def altered(data, transport, *, admission_reason=None):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', data)
        if defect == 'pre_core' and admission_reason is not None:
            return struct.pack('<BHBB', 2, corr, 0, admission_reason)
        cached = sid != 0 and sid == core.last and corr in core.cache
        if defect == 'reexecute' and cached and fn and op == 16:
            core.cache.pop(corr); core.high = corr - 1
        old_cache = core.cache.copy()
        value = handle(data, transport, admission_reason=admission_reason)
        if defect == 'uncached' and admission_reason == 6 and not cached:
            core.cache.pop(corr, None)
        if defect == 'renew' and cached:
            core.deadline += 1
        if defect == 'peer_cache' and sid == 0 and op == 4 and core.holder is not None:
            core.cache.clear()
        if defect == 'takeover' and op == 16 and sid != 0 and data[-1:] == b'\x01' and old_cache:
            for key, item in old_cache.items(): core.cache.setdefault(key, item)
        return value
    core.handle = altered
    report = runner.run()
    failed = [x for x in report['checks'] if x['status'] == 'failed']
    assert any(x['id'].startswith('CORE-PRESSURE-') and x['id'] != 'CORE-PRESSURE-INSTRUMENTATION'
               for x in failed), failed


@pytest.mark.parametrize('invalid', ['state_capability', 'state_fields', 'fn'])
def test_instrumentation_preflight_precedes_session_work(invalid):
    model, adapter, runner = build()
    if invalid == 'state_capability': adapter.state = None
    elif invalid == 'state_fields':
        state = adapter.state
        def incomplete():
            value = state(); value.pop('deadline'); return value
        adapter.state = incomplete
    else: adapter.functions = (0,)
    report = runner.run()
    assert report['status'] == 'failed'
    assert model.ep.current_core.last is None
    assert model.extension.next_id == 1


def test_pressure_does_not_move_session_to_peer():
    model, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed'
    peer_requests = [bytes.fromhex(x['request_hex']) for x in report['peer_exchanges']]
    nonzero = [struct.unpack_from('<I', req, 6)[0] for req in peer_requests
               if struct.unpack_from('<I', req, 6)[0]]
    assert nonzero and len(set(nonzero)) == 1  # only replacement T's open/end
    assert not model.pending('primary') and not model.pending('peer')
