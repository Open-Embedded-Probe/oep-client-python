import copy
import pytest
from oep_client import registry_legacy as registry
from oep_client.conformance import Checks
from oep_client.conformance_capture_group import CaptureGroupChecks
from oep_client.conformance_capture_group_sample import SampleCaptureGroupAdapter
from oep_client.virtual_capture_group_model import CaptureGroupModel
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,checks=registered_setup();reg=checks.reg
    reg['core']['tlv']['unavailable_payload']={'cause':1,'fn':5}
    specs={'oep.probe.plan':registry.PROBE_PLAN,'oep.fixture.analog':registry.FIXTURE_ANALOG,
           'oep.fixture.logic':registry.FIXTURE_LOGIC,'oep.fixture.capture-group':registry.FIXTURE_CAPTURE_GROUP}
    for spec in reg['interface']:
        if spec['name'] in specs:
            spec['tlv']=copy.deepcopy(specs[spec['name']].tlv)
            spec['enum']=copy.deepcopy(specs[spec['name']].enum)
    model=CaptureGroupModel(boot_id=17)
    return model,Checks(model.handle,reg,'virtual-resource-1')


def test_group_operations():
    model,checks=setup();result=CaptureGroupChecks(checks,SampleCaptureGroupAdapter(model)).run()
    assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
    assert len([r for r in result['checks'] if r['id'].startswith('OEP-GROUP-')])==len(CaptureGroupChecks.cases)+1
    assert not result['full_conformance']


@pytest.mark.parametrize('kind',['declarations','metadata'])
def test_group_declarations(kind):
    from oep_client.conformance_interfaces import InterfaceChecks
    from oep_client.conformance_capture_metadata import CaptureMetadataChecks
    model,checks=setup()
    result=(InterfaceChecks(checks) if kind=='declarations' else CaptureMetadataChecks(checks,{2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'})).run()
    assert result['status']=='passed',result['checks']
    if kind=='metadata':
        assert checks.observed['capture_metadata']['2']['channels']==2
        assert checks.observed['capture_metadata']['3']['channels']==2
        assert checks.observed['capture_metadata']['4']['tracks']==[2,3]


@pytest.mark.parametrize('case',CaptureGroupChecks.cases)
def test_bad_group_wire_result_is_detected(case):
    model,checks=setup();original=model.handle
    def broken(req):
        result=original(req)
        if int.from_bytes(req[3:5],'little')==4:result=result[:4]+bytes(((result[4]+1)&255,))+result[5:]
        return result
    checks.send=broken;checker=CaptureGroupChecks(checks,SampleCaptureGroupAdapter(model));checker.cases=(case,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-GROUP-'+case)['status']=='failed'


@pytest.mark.parametrize('method,args',[('group_feed',([{}],)),('group_feed',([{7:[0]}],)),('group_advance',(-1,)),('group_fail',(2,2)),('group_seed',(True,0,0)),('group_invalidate',(7,))])
def test_invalid_controls_do_not_change_group(method,args):
    model,_=setup();before=model.group_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.group_state()==before


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_preflight_blocks_group(invalid):
    model,checks=setup();adapter=SampleCaptureGroupAdapter(model)
    if invalid=='scope':adapter.reset_scope='physical'
    else:adapter.selection={4:'oep.fixture.capture-group'}
    result=CaptureGroupChecks(checks,adapter).run()
    assert result['checks'][0]['status']=='failed'
    assert all(r['status']=='blocked' for r in result['checks'][1:]) and not adapter.trace


@pytest.mark.parametrize('fault,case',[
 ('mutate_bind_refusal','BIND-MODE'),('wrong_refusal_fn','BIND-UNCONFIGURED'),
 ('allow_track_op','TRACK-LOCK'),('allow_plan_op','PLAN-LOCK'),('query_applies','QUERY'),
 ('reverse_reply','REVERSE'),('generation_zero','GEN-WRAP'),('track_generation','GENERATION'),
 ('reset_bind_generation','REBIND-GEN'),('partial_start','START-ATOMIC'),
 ('stop_one_track','STOP'),('no_short','STOP'),('premature_done','START'),
 ('paused_group','REPEAT'),('release_peer','RELEASE'),('wrong_index','TRIGGER'),
 ('shift_trigger','TRIGGER'),('force_source','FORCE'),('error_peer_runs','ERROR-ADC'),
 ('keep_binding','END'),('drive_pin','START'),
])
def test_semantic_group_faults_are_detected(fault,case):
    from oep_client.virtual_core import Reject,tlv
    import struct
    model,checks=setup();install=model.install
    def faulty_install():
        install();e=model.extension;bind=e.bind;start=e.start;fire=e.fire;release=e.release;dispatch=e.dispatch_request;plan=e.plan;state=e.state
        def broken_bind(payload,tags):
            try:result=bind(payload,tags)
            except Reject as error:
                if fault=='mutate_bind_refusal':e.bound=[2]
                if fault=='wrong_refusal_fn':raise Reject(error.reason,tlv(1,b'\6')+tlv(5,b'\2\0'))
                raise
            if fault=='reset_bind_generation':e.generation=0
            return result
        def broken_start():
            try:result=start()
            except Reject:
                if fault=='partial_start':e.tracks[2].generation+=1;e.tracks[2].clear(3)
                raise
            if fault=='reverse_reply':result=result[:17]+result[23:29]+result[17:23]
            if fault=='generation_zero':e.generation=0;result=result[:12]+b'\0'*4+result[16:]
            if fault=='track_generation':e.tracks[2].generation-=1
            if fault=='drive_pin':e.pins[1]=(4,0)
            return result
        def broken_fire(forced=False):
            fire(forced)
            if fault=='wrong_index':e.tracks[3].trigger_index+=1
            if fault=='shift_trigger':e.trigger_ns+=1000
            if fault=='force_source' and forced:e.trigger_fn=e.source
        def broken_release():
            before=list(e.bound);release()
            if fault=='keep_binding':e.bound=before
        def broken_plan(op,payload,tags):
            if fault=='allow_plan_op':
                before=e.bound;e.bound=[]
                try:return plan(op,payload,tags)
                finally:e.bound=before
            return plan(op,payload,tags)
        def broken_dispatch(fn,op,payload,tags):
            if fault=='stop_one_track' and fn==4 and op==18:return e.tracks[2].dispatch_request(2,18,payload,{})
            if fault=='allow_track_op' and fn in e.bound and op in (16,17,18,19):
                before=e.bound;e.bound=[]
                try:return dispatch(fn,op,payload,tags)
                finally:e.bound=before
            result=dispatch(fn,op,payload,tags)
            if fault=='query_applies' and fn==2 and op==24:e.tracks[2].config=e.tracks[2].settings(tags);e.tracks[2].clear(1)
            if fault=='release_peer' and fn==2 and op==23:e.tracks[3].segments.clear();e.tracks[3].data.clear()
            return result
        def broken_state():
            result=state()
            if fault=='premature_done' and result==3:return 4
            if fault=='paused_group' and e.bound and all(e.tracks[fn].state==5 for fn in e.bound):return 5
            return result
        e.bind=broken_bind;e.start=broken_start;e.fire=broken_fire;e.release=broken_release;e.plan=broken_plan;e.dispatch_request=broken_dispatch;e.state=broken_state
        if fault=='no_short':
            track=e.tracks[3];finish=track.finish
            def broken_finish(short=False):
                finish(short)
                if track.segments:
                    row=track.segments[-1];track.segments[-1]=row[:6]+(row[6]&~2,)+row[7:]
            track.finish=broken_finish
    model.install=faulty_install
    if fault=='error_peer_runs':
        fail=model.group_fail
        def broken_fail(fn,error):
            fail(fn,error);model.extension.tracks[3].state=3
        model.group_fail=broken_fail
    checker=CaptureGroupChecks(checks,SampleCaptureGroupAdapter(model));checker.cases=(case,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-GROUP-'+case)['status']=='failed'


@pytest.mark.parametrize('case,op',[('REPLAY-START',17),('REPLAY-BIND',16),('REPLAY-STOP',18)])
def test_replay_group_side_effects_are_detected(case,op):
    model,checks=setup();original=model.handle;seen=set()
    def broken(req):
        result=original(req)
        if int.from_bytes(req[3:5],'little')==4 and req[5]==op:
            if req in seen:
                if op==17:model.extension.generation+=1;model.extension.tracks[2].data.clear()
                elif op==16:model.extension.bound=[]
                else:model.extension.tracks[2].data.clear()
            seen.add(req)
        return result
    checks.send=broken;checker=CaptureGroupChecks(checks,SampleCaptureGroupAdapter(model));checker.cases=(case,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-GROUP-'+case)['status']=='failed'
