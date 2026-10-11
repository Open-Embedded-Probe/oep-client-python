import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_allocator import AllocatorChecks
from oep_client.conformance_allocator_sample import SampleAllocatorAdapter
from oep_client.virtual_allocator_model import AllocatorModel
from oep_client.virtual_core import Reject,tlv
from test_conformance import REG


def setup():
    model=AllocatorModel();c=Checks(model.handle,copy.deepcopy(REG),'virtual-resource-1');a=SampleAllocatorAdapter(c,model)
    return model,c,a,AllocatorChecks(c,a)


def test_allocator_contracts_pass():
    model,_,_,runner=setup();r=runner.run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==7 and all(x['level']=='core' for x in r['checks']) and not r['full_conformance']
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('fault,case',[('wrap','BOUNDARY'),('zero','BOUNDARY'),('cause','BOUNDARY'),
    ('damage','BOUNDARY'),('close_reset','BOUNDARY'),('end_reset','END'),('expiry_reset','EXPIRY'),('force_reset','FORCE'),
    ('replay_execution','REPLAY'),('reboot_retains','REBOOT')])
def test_allocator_faults_are_detected(fault,case):
    model,c,a,runner=setup();reset=model.restart_fixture
    def restart():
        value=reset();e=model.extension;dispatch=e.dispatch;release=e.release
        def corrupt(fn,op,payload):
            if op==16 and e.next_id>65535:
                if fault=='wrap':e.next_id=1
                elif fault=='zero':e.next_id=0
                elif fault=='cause':raise Reject(4,tlv(1,b'\x06'))
                elif fault=='damage':e.live.clear()
            value=dispatch(fn,op,payload)
            if fault=='close_reset' and op==17:e.next_id=1
            return value
        e.dispatch=corrupt
        if fault in ('end_reset','expiry_reset','force_reset'):
            def bad_release():
                had_live=bool(e.live);release()
                if had_live:e.next_id=1
            e.release=bad_release
        if fault=='replay_execution':
            handle=model.ep.current_core.handle
            def bad_handle(request,*args,**kwargs):
                replay=int.from_bytes(request[1:3],'little') in model.ep.current_core.cache
                value=handle(request,*args,**kwargs)
                if replay and request[3:6]==bytes((1,0,16)):e.next_id+=1
                return value
            model.ep.current_core.handle=bad_handle
        return value
    model.restart_fixture=restart
    if fault=='reboot_retains':
        reboot=model.reboot
        def retained(boot):
            next_id=model.extension.next_id;reboot(boot);model.extension.next_id=next_id
        model.reboot=retained
    method={'BOUNDARY':runner.boundary,'END':runner.ended_boundary,'EXPIRY':lambda:runner.ended_boundary(True),'FORCE':runner.force_boundary,
            'REPLAY':runner.replay_boundary,'REBOOT':runner.reboot_boundary}[case]
    c.check('CORE-ALLOCATOR-'+case,'core §9',method)
    assert c.results[-1]['status']=='failed',c.results


@pytest.mark.parametrize('invalid',['scope','reference','capability'])
def test_allocator_preflight_blocks_before_wire(invalid):
    _,c,a,runner=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='reference':a.reference=''
    else:a.seed=None
    r=runner.run();assert r['checks'][0]['status']=='failed' and not c.trace and not a.trace
    assert all(x['status']=='blocked' for x in r['checks'][1:])


@pytest.mark.parametrize('first,last,status',[(2,6,'passed'),(6,2,'failed')])
def test_allocator_cause_uses_first_duplicate_tlv(first,last,status):
    model,c,a,runner=setup();reset=model.restart_fixture
    def restart():
        value=reset();e=model.extension;dispatch=e.dispatch
        def repeated(fn,op,payload):
            if op==16 and e.next_id>65535:raise Reject(4,tlv(1,bytes((first,)))+tlv(1,bytes((last,))))
            return dispatch(fn,op,payload)
        e.dispatch=repeated
        return value
    model.restart_fixture=restart
    c.check('CORE-ALLOCATOR-REPLAY','core §2.3/9',runner.replay_boundary)
    assert c.results[-1]['status']==status,c.results
