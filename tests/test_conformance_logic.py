import pytest
from oep_client.conformance import Checks
from oep_client.conformance_logic import LogicChecks
from oep_client.conformance_logic_sample import SampleLogicAdapter
from oep_client.virtual_logic_model import LogicModel
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,c=registered_setup();reg=c.reg
    for spec in reg['interface']:
        if spec['name']=='oep.probe.plan':spec['tlv']={'plan_apply':{'role_assignment':16},'describe':{'plan_roles':64}}
        if spec['name']=='oep.fixture.logic':spec['tlv']={'configure':{'mode':64,'rate':66,'samples':67,'segments':68,'trigger':69,'pretrigger':70},'configure_answer':{'actual_rate':80,'layout':81,'actual_samples':82,'actual_segments':83,'blocking_ms':86},'describe':{'mode':64,'rate_range':65,'channels':68,'trigger':69,'multirate':96}}
    model=LogicModel(boot_id=17);return model,Checks(model.handle,reg,'virtual-resource-1')


def test_logic_contract():
    model,c=setup();r=LogicChecks(c,SampleLogicAdapter(model)).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==33 and not r['full_conformance']


def test_generic_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


def test_metadata():
    from oep_client.conformance_capture_metadata import CaptureMetadataChecks
    _,c=setup();r=CaptureMetadataChecks(c,{2:'oep.fixture.logic'}).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('kind',LogicChecks.cases)
def test_wire_faults_detected(kind):
    model,c=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little'):
            if reply[3]==1:reply=reply[:4]+b'\1'+reply[5:]
            else:reply=reply[:4]+b'\3'
        return reply
    c.send=broken;checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=(kind,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-'+kind)['status']=='failed'


@pytest.mark.parametrize('fault,case',[
 ('query_applies','QUERY'),('wrong_pack','ONE-SHOT'),('generation_zero','GEN-WRAP'),
 ('keep_old_data','GENERATION'),('no_resume','RELEASE'),('no_gap','REPEAT'),
 ('no_short','SHORT-STOP'),('wrong_stop_state','SHORT-STOP'),('discard_committed','ERROR'),
 ('error_retains_partial','ERROR'),('pin_output','ONE-SHOT'),('reset_generation','PLAN-RELEASE'),
])
def test_side_effect_faults_detected(fault,case):
    model,c=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;finish=e.finish;release=e.release_plans
        def broken_dispatch(fn,op,payload,tags):
            old_data=bytes(e.data);old_state=e.state;result=dispatch(fn,op,payload,tags)
            if fn==2:
                if fault=='query_applies' and op==24:e.config=e.settings(tags);e.state=1
                if fault=='generation_zero' and op==17:e.generation=0;result=result[:4]+b'\0'*4
                if fault=='keep_old_data' and op==17:e.data[:]=old_data
                if fault=='no_resume' and op==23:e.state=5
                if fault=='no_gap' and op==23:e.gap=False
                if fault=='wrong_stop_state' and op==18 and old_state==3:e.state=4
            return result
        def broken_finish(short=False):
            finish(short)
            if fault=='wrong_pack' and e.data:e.data[0]^=1
            if fault=='no_short' and e.segments:
                row=e.segments[-1];e.segments[-1]=row[:6]+(0,)+row[7:]
            if fault=='pin_output':e.pins[1]=(4,0)
        def broken_release(functions):
            release(functions)
            if fault=='reset_generation':e.generation=0
        e.dispatch_request=broken_dispatch;e.finish=broken_finish;e.release_plans=broken_release
    model.install=faulty_install
    if fault in ('discard_committed','error_retains_partial'):
        original=model.logic_fail
        def broken_fail(error):
            original(error)
            if fault=='discard_committed':model.extension.data.clear();model.extension.segments.clear()
            else:model.extension.data.append(255)
        model.logic_fail=broken_fail
    checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-'+case)['status']=='failed'


@pytest.mark.parametrize('case',['REPLAY-START','REPLAY-RELEASE'])
def test_replay_cannot_repeat_side_effects(case):
    model,c=setup();original=model.handle;seen=set();operation=17 if case=='REPLAY-START' else 23
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==operation:
            if req in seen:
                if case=='REPLAY-START':model.extension.clear(3);model.extension.generation+=1
                else:model.extension.segments.clear();model.extension.data.clear()
            seen.add(req)
        return reply
    c.send=broken;checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-'+case)['status']=='failed'


@pytest.mark.parametrize('method,args',[('logic_feed',([[True]],)),('logic_advance',(-1,)),('logic_fail',(2,)),('logic_seed',(True,))])
def test_invalid_controls_do_not_mutate(method,args):
    model,_=setup();before=model.logic_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.logic_state()==before


def test_unused_layout_bits_are_not_required_zero():
    model,c=setup();r=LogicChecks(c,SampleLogicAdapter(model)).run();assert r['status']=='passed',r['checks']
    # The model always sets the unassigned bit2 for two-channel samples.
    checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=('ONE-SHOT',);r=checker.run();assert r['status']=='passed'


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_preflight_blocks(invalid):
    model,c=setup();a=SampleLogicAdapter(model)
    if invalid=='scope':a.reset_scope='physical'
    else:a.selection={2:'oep.fixture.logic'}
    r=LogicChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:]);assert not a.trace


def test_future_empty_read_can_keep_requested_position():
    model,c=setup();original=model.handle
    def requested_position(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==21 and reply[3:5]==b'\1\0':
            asked=int.from_bytes(req[14:22],'little')
            if asked==999:reply=reply[:5]+asked.to_bytes(8,'little')+reply[13:]
        return reply
    c.send=requested_position;r=LogicChecks(c,SampleLogicAdapter(model)).run();assert r['status']=='passed',r['checks']
