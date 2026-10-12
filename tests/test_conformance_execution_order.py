import copy
from collections import deque
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_execution_order import ExecutionOrderChecks
from oep_client.conformance_execution_order_sample import SampleExecutionOrderAdapter
from oep_client.virtual_pipeline_model import PipelineModel
from test_conformance import REG


def build():
    model=PipelineModel(lambda:0,boot_id=17);adapter=SampleExecutionOrderAdapter(model)
    checks=Checks(lambda req:adapter.exchange(adapter.primary,req),copy.deepcopy(REG),'virtual-routes-1')
    return model,ExecutionOrderChecks(checks,adapter)


def test_dependent_effects_pass_for_every_writer_state():
    model,runner=build();report=runner.run()
    assert report['status']=='passed',report['checks']
    assert len(report['checks'])==19  # 7 contract checks plus 2 declarations per case.
    assert len(report['observations'])==6 and not report['full_conformance']
    assert model.ep.current_core.holder is None and not model.extension.live


@pytest.mark.parametrize('fault,phase',[('reverse','CLOSE-USE-CREATE'),('fake_effects','CLOSE-USE-CREATE'),
                                      ('wrong_results','CLOSE-USE-CREATE'),('deferred_create','CREATE-CLOSE-USE')])
def test_order_faults_fail_even_with_ordered_reply_headers(fault,phase):
    model,runner=build();reset=model.restart_fixture
    def faulty_reset():
        value=reset();service=model.service;dispatch=model.extension.dispatch;delayed=[];active=[False]
        def broken_dispatch(fn,op,payload):
            result=dispatch(fn,op,payload)
            if fault=='deferred_create' and active[0] and op==16:
                rid=int.from_bytes(result,'little');delayed.append((rid,model.extension.live.pop(rid)))
            return result
        model.extension.dispatch=broken_dispatch
        def broken_service():
            batch=len(model.requests)==3;before=copy.deepcopy(model.extension.live)
            if batch and fault=='reverse':model.requests=deque(reversed(model.requests))
            active[0]=batch
            try:service()
            finally:active[0]=False
            for rid,resource in delayed:model.extension.live[rid]=resource
            delayed.clear()
            if batch:
                pipe=model.pipes['primary']
                if fault=='fake_effects':model.extension.live=before
                if fault=='reverse':
                    pairs=sorted(zip(pipe['result'],model.completions['primary']),key=lambda pair:int.from_bytes(pair[0][1:3],'little'))
                    pipe['result']=deque(pair[0] for pair in pairs);model.completions['primary']=deque(pair[1] for pair in pairs)
                if fault=='wrong_results':pipe['result'].reverse();model.completions['primary'].reverse()
        model.service=broken_service;return value
    model.restart_fixture=faulty_reset
    runner.c.check('CORE-EXECUTION-ORDER-FAULT','core §4.4',lambda:runner.case(phase,0))
    row=runner.c.results[-1];assert row['status']=='failed',runner.c.results
    assert ('result/corr/outcome' if fault=='wrong_results' else 'side effect resources') in row['error'],runner.c.results


@pytest.mark.parametrize('missing',['restart_fixture','execution_state'])
def test_missing_order_capabilities_block_mutation(missing):
    model,runner=build();setattr(runner.a,missing,None);report=runner.run()
    assert report['checks'][0]['status']=='failed' and all(row['status']=='blocked' for row in report['checks'][1:])
    assert model.ep.current_core.last is None
