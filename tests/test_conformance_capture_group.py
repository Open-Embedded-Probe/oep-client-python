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
        assert checks.observed['capture_metadata']['4']['tracks']==[2,3,*range(6,13)]


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


@pytest.mark.parametrize('method,args',[('group_feed',([{}],)),('group_feed',([{7:[0]}],)),('group_advance',(-1,)),('group_fail',(2,2)),('group_seed',(True,0,0)),('group_invalidate',(13,)),('group_frame',(63,))])
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
 ('mutate_bind_refusal','BIND-MODE'),('wrong_refusal_fn','BIND-UNCONFIGURED'),('dup_accepted','BIND-DUP'),('unknown_no_fn','BIND-UNKNOWN'),
 ('ignore_frame','BIND-FRAME'),('same_list_refused','BIND-SAME'),('allow_trigger','BIND-TRIGGER'),('allow_active_track','BIND-ACTIVE'),
 ('steal_track','BIND-OTHER'),('other_group_cause6','BIND-OTHER'),('rate_cause6','BIND-RATE'),('critical_ignored','BIND-TLV'),
 ('change_active','UNBIND-ACTIVE'),('rebind_done_refused','REBIND-DONE'),
 ('allow_track_op','TRACK-LOCK'),('allow_plan_op','PLAN-LOCK'),('same_plan_refused','PLAN-LOCK'),('query_applies','QUERY'),
 ('reverse_reply','REVERSE'),('track_generation','GENERATION'),('generation_wrap','GEN-LAST'),('track_generation_wrap','GEN-LAST-TRACK'),
 ('start_while_active','START-ACTIVE'),('reset_bind_generation','REBIND-GEN'),('partial_start','START-ATOMIC'),
 ('stop_one_track','STOP'),('no_short','STOP'),('premature_done','START'),('legacy_start','START'),('legacy_status','EMPTY'),
 ('paused_group','REPEAT'),('release_peer','RELEASE'),('error_peer_runs','ERROR-ADC'),
 ('keep_binding','END'),('drive_pin','START'),
])
def test_semantic_group_faults_are_detected(fault,case):
    from oep_client.virtual_core import Reject,tlv
    from oep_client.virtual_capture_group_model import NO_TIME,LAST
    import struct
    model,checks=setup();install=model.install
    def faulty_install():
        install();e=model.extension;bind=e.bind;start=e.start;release=e.release;dispatch=e.dispatch_request;plan=e.plan;state=e.state;status=e.status;tags=e.request_tags
        def bound_states(states):
            saved={fn:t.state for fn,t in e.tracks.items()}
            for fn,t in e.tracks.items():
                if t.state in states:t.state=1
            return saved
        def broken_bind(payload,tags,gid=4):
            functions=list(struct.unpack('<'+'H'*payload[0],payload[1:]));g=e.groups[gid]
            if fault=='dup_accepted' and len(set(functions))!=len(functions):g.bound=functions;return b''
            if fault=='same_list_refused' and functions==g.bound and e.state(gid)==3:raise e.refusal(6)
            if fault=='change_active' and e.state(gid)==3 and functions!=g.bound:g.bound=functions;return b''
            if fault=='rebind_done_refused' and functions!=g.bound and any(e.tracks[fn].state==4 for fn in functions if fn in e.tracks):raise e.refusal(6)
            saved=frame=configs=peers=None
            if fault=='ignore_frame':frame=model.ep.probe.max_frame;model.ep.probe.max_frame=65535
            if fault=='allow_active_track':saved=bound_states((2,3,5))
            if fault=='allow_trigger':configs={fn:t.config for fn,t in e.tracks.items()};[setattr(t,'config',dict(t.config,trigger=(0,0,0))) for t in e.tracks.values() if t.config]
            if fault=='steal_track':peers={other:list(h.bound) for other,h in e.groups.items() if other!=gid};[setattr(h,'bound',[]) for other,h in e.groups.items() if other!=gid]
            try:result=bind(payload,tags,gid)
            except Reject as error:
                if fault=='mutate_bind_refusal':e.bound=[2]
                if fault=='wrong_refusal_fn':raise Reject(error.reason,tlv(1,b'\6')+tlv(5,b'\2\0'))
                if fault=='unknown_no_fn' and error.reason==11:raise Reject(11,b'\0')
                if fault=='other_group_cause6' and error.payload[:4]==tlv(1,b'\4'):raise Reject(4,tlv(1,b'\6')+error.payload[4:])
                if fault=='rate_cause6' and error.payload[:4]==tlv(1,b'\2'):raise Reject(4,tlv(1,b'\6')+error.payload[4:])
                raise
            finally:
                if frame is not None:model.ep.probe.max_frame=frame
                if saved is not None:
                    for fn,value in saved.items():e.tracks[fn].state=value
                if configs is not None:
                    for fn,value in configs.items():
                        if value is not None:e.tracks[fn].config=value
                if peers is not None:
                    for other,value in peers.items():e.groups[other].bound=value
            if fault=='reset_bind_generation':e.generation=0
            return result
        def broken_start(gid=4):
            g=e.groups[gid]
            if fault=='generation_wrap' and g.generation==LAST:g.generation=0
            if fault=='track_generation_wrap':
                for fn in g.bound:
                    if e.tracks[fn].generation==LAST:e.tracks[fn].generation=0
            if fault=='start_while_active':bound_states((2,3,5))
            try:result=start(gid)
            except Reject:
                if fault=='partial_start':e.tracks[2].generation+=1;e.tracks[2].clear(3)
                raise
            if fault=='reverse_reply':result=result[:13]+result[19:25]+result[13:19]
            if fault=='track_generation':e.tracks[2].generation-=1
            if fault=='drive_pin':e.pins[1]=(4,0)
            if fault=='legacy_start':result=b'\0'*4+result
            return result
        def broken_status(gid=4):
            if fault=='legacy_status':g=e.groups[gid];return struct.pack('<BQQHI',e.state(gid),g.start_ns,NO_TIME,0,g.generation)
            return status(gid)
        def broken_tags(fn,op,data):
            try:return tags(fn,op,data)
            except Reject as error:
                if fault=='critical_ignored' and fn in e.groups and error.reason==11:return {}
                raise
        def broken_release():
            before=list(e.bound);release()
            if fault=='keep_binding':e.bound=before
        def broken_plan(op,payload,tags):
            owned={gid:list(g.bound) for gid,g in e.groups.items()}
            if fault=='allow_plan_op':
                for g in e.groups.values():g.bound=[]
                try:return plan(op,payload,tags)
                finally:
                    for gid,value in owned.items():e.groups[gid].bound=value
            if fault=='same_plan_refused' and op==16 and any(struct.unpack('<H',value[:2])[0] in e.bound for _,value in tags):raise e.refusal(4,e.bound[0])
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
        def broken_state(gid=4):
            result=state(gid)
            if fault=='premature_done' and result==3:return 4
            if fault=='paused_group' and e.groups[gid].bound and all(e.tracks[fn].state==5 for fn in e.groups[gid].bound):return 5
            return result
        e.bind=broken_bind;e.start=broken_start;e.status=broken_status;e.request_tags=broken_tags;e.release=broken_release;e.plan=broken_plan;e.dispatch_request=broken_dispatch;e.state=broken_state
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
    row=next(r for r in result['checks'] if r['id']=='OEP-GROUP-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row.get('error')


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
