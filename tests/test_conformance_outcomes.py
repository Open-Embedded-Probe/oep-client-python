import copy
import struct
import pytest
from oep_client.conformance import Checks
from oep_client.virtual_core import Completed
from oep_client.virtual_resource_model import ResourceModel
from test_conformance import REG


@pytest.mark.parametrize('outcome',[1,2])
def test_completed_failure_and_partial_are_cached_without_reexecution(outcome):
    now=[0];model=ResourceModel(lambda:now[0],boot_id=17);c=Checks(model.handle,copy.deepcopy(REG),'virtual-resource-1')
    calls=[]
    def dispatch(fn,op,payload):
        calls.append((fn,op,payload));return Completed(b'\x01\0',outcome)
    model.extension.dispatch=dispatch
    c.confirm();c.identity()
    with c.holding(3000) as sid:
        request=c.request(16,b'\x01',session=sid,fn=1)
        now[0]=100
        result=c.exchange(request)
        lock=c.success(c.request(c.core['lock_state']))
        assert struct.unpack_from('<BI',lock)==(1,3000)
        now[0]=200
        assert result[3:5]==bytes((1,outcome)) and result[5:]==b'\x01\0'
        assert c.exchange(request)==result and len(calls)==1
        lock=c.success(c.request(c.core['lock_state']))
        assert struct.unpack_from('<BI',lock)==(1,2900)
        assert dict(model.ep.current_core.cache)[int.from_bytes(request[1:3],'little')]==(request,result)

from oep_client.conformance_outcomes import OutcomeChecks, decode_debug_counted, debug_succeeded
from oep_client.conformance_outcome_sample import SampleOutcomeAdapter
from oep_client.virtual_outcome_model import OutcomeModel


def setup():
    reg=copy.deepcopy(REG);reg['status']={'ok':0,'wait':1,'line':2,'fault':3,'timeout':4,'state':5}
    model=OutcomeModel(lambda:0,boot_id=17);c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c,SampleOutcomeAdapter(model)


def test_outcome_contracts_pass():
    _,c,a=setup();report=OutcomeChecks(c,a).run()
    assert report['status']=='passed',[(r['id'],r.get('error')) for r in report['checks']]
    assert len(report['checks'])==19 and not report['full_conformance']
    assert all(r['level']=='interface' for r in report['checks'])


@pytest.mark.parametrize('defect',['wrong_outcome','tail_not_prefix','accepted_overflow','debug_rejected','wrong_done','missing_values','wrong_single_shape','reexecute'])
def test_outcome_mutants_fail(defect):
    model,c,a=setup();restart=model.restart_fixture
    def corrupt(position,serial):
        result=restart(position,serial);extension=model.extension;dispatch=extension.dispatch
        def broken(fn,op,payload):
            reply=dispatch(fn,op,payload)
            if fn==1 and op==20:
                if defect=='wrong_outcome' and getattr(reply,'outcome',0):return Completed(bytes(reply),0)
                if defect=='tail_not_prefix' and len(payload)>6:extension.tx[:]=payload[-4:]
                if defect=='accepted_overflow':return Completed(struct.pack('<H',100),getattr(reply,'outcome',0))
            if fn==2:
                if defect=='debug_rejected' and getattr(reply,'outcome',0):
                    from oep_client.virtual_core import Reject
                    raise Reject(4)
                if op==16 and defect=='wrong_done':return Completed(b'\x09\0'+reply[2:],getattr(reply,'outcome',0))
                if op==16 and defect=='missing_values' and int.from_bytes(reply[:2],'little'):return Completed(reply[:3],getattr(reply,'outcome',0))
                if op==17 and defect=='wrong_single_shape' and getattr(reply,'outcome',0):return Completed(b'\0'+reply,1)
            return reply
        extension.dispatch=broken
        if defect=='reexecute':
            core=model.ep.current_core;handle=core.handle
            def replay(request,route,**kwargs):
                corr=int.from_bytes(request[1:3],'little');fn=int.from_bytes(request[3:5],'little')
                if fn and corr in core.cache:core.cache.pop(corr);core.high=corr-1
                return handle(request,route,**kwargs)
            core.handle=replay
        return result
    model.restart_fixture=corrupt
    report=OutcomeChecks(c,a).run()
    assert report['status']=='failed'
    assert any(r['status']=='failed' for r in report['checks'][1:])


@pytest.mark.parametrize('status',[6,0x40,0x7f,0xff])
def test_unknown_status_never_becomes_success_or_changes_payload_shape(status):
    _,c,_=setup();frame=struct.pack('<BHBBHBII',2,1,1,0,2,status,11,22)
    decoded=decode_debug_counted(frame,2)
    assert decoded=={'done':2,'status':status,'values':[11,22]}
    assert not debug_succeeded(frame,decoded,2,c.reg)


@pytest.mark.parametrize('invalid',['component','geometry','scope','status'])
def test_invalid_adoption_or_registry_blocks_before_wire(invalid):
    _,c,a=setup();a.adoptions=copy.deepcopy(a.adoptions)
    if invalid=='component':a.adoptions['debug']['component']='other'
    elif invalid=='geometry':a.adoptions['tx_capacity']=5
    elif invalid=='scope':a.reset_scope='physical'
    else:c.reg['status']['ok']=7
    report=OutcomeChecks(c,a).run()
    assert report['checks'][0]['status']=='failed' and not c.trace
    assert all(r['status']=='blocked' for r in report['checks'][1:])


@pytest.mark.parametrize('detail',[3,0x40])
def test_unknown_completed_outcome_is_not_host_success(detail):
    _,c,_=setup();frame=struct.pack('<BHBBHBI',2,1,1,detail,1,0,11)
    decoded=decode_debug_counted(frame,1)
    assert not debug_succeeded(frame,decoded,1,c.reg)


def test_failed_write_is_completion_not_original_operation_rejection():
    model,c,a=setup();c.confirm();c.identity()
    with c.holding(3000) as sid:
        runner=OutcomeChecks(c,a);runner.write(sid,b'abcd',4,0)
        _,frame=runner.write(sid,b'x',0,1)
        assert frame[3]==c.reg['resolutions']['completed']
        assert frame[4]==c.reg['outcomes']['failed'] and model.extension.tx==b'abcd'
