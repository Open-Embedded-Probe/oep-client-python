import struct
import pytest
from oep_client.conformance_capture_timeline_lifecycle import CaptureTimelineLifecycleChecks
from oep_client.conformance_capture_timeline_sample import SampleCaptureTimelineAdapter
from oep_client.virtual_capture_timeline_model import Completed
from test_conformance_capture_timeline import setup,run as run_checker,failed as failed_row


def run(model,checks,scenario):return run_checker(model,checks,scenario,CaptureTimelineLifecycleChecks)
def failed(result):return failed_row(result,'OEP-TIMELINE-LIFE-0')


@pytest.mark.parametrize('scenario',CaptureTimelineLifecycleChecks.scenarios)
def test_timeline_lifecycle(scenario):
    model,checks=setup();result=run(model,checks,scenario)
    assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
    assert not result['full_conformance']


@pytest.mark.parametrize('scenario',CaptureTimelineLifecycleChecks.scenarios)
def test_lifecycle_wire_fault_is_detected(scenario):
    model,checks=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little') in (2,3,4):reply=reply[:4]+bytes(((reply[4]+1)&255,))+reply[5:]
        return reply
    checks.send=broken;assert failed(run(model,checks,scenario))


@pytest.mark.parametrize('fault,kind',[
    ('stop_peer_runs','STOP-ACTIVE'),('no_short','STOP-ACTIVE'),('discard_done','STOP-MIXED'),('old_generation','STOP-ACTIVE'),
    ('wrong_write_pos','STOP-ACTIVE'),('query_applies','QUERY-ACTIVE'),('keep_cache','END-ACTIVE'),('reset_generation','END-ACTIVE'),
    ('restart_keeps_data','STOP-ACTIVE'),('late_expiry','EXPIRY-ACTIVE'),('error_peer_runs','ERROR-FAST'),('publish_failed','ERROR-FAST'),
    ('wrong_error','ERROR-FAST'),('failed_position','ERROR-FAST'),('restart_origin','STOP-ACTIVE'),('error_discards_done','ERROR-MIXED'),
    ('error_resets_done_state','ERROR-MIXED'),
    # generation exhaustion
    ('wrap_group_generation','GEN-EXHAUST-GROUP'),('exhaust_side_effect','GEN-EXHAUST-GROUP'),('wrap_track_generation','GEN-EXHAUST-TRACK'),('exhaust_wrong_cause','GEN-EXHAUST-TRACK'),
    # start failure fixed form
    ('failure_success','START-FAIL-FIRST'),('failure_short_form','START-FAIL-SECOND'),('failure_state','START-FAIL-FIRST'),('failure_peer_runs','START-FAIL-FIRST'),
    ('failure_rollback','START-FAIL-SECOND'),('failure_success','START-FAIL-TRACK'),('failure_state','START-FAIL-TRACK'),
    # state table
    ('configure_active','STATE-TABLE'),('stop_done_clears','STATE-TABLE'),('release_no_generation','STATE-TABLE'),('start_from_error','STATE-TABLE'),
    ('stop_error_keeps','STATE-TABLE'),('configure_unplanned','STATE-TABLE'),('stop_unconfigured','STATE-TABLE'),
    # streaming error
    ('error_write_pos','STREAM-ERROR'),('error_publishes','STREAM-ERROR'),('error_wrong_reason','STREAM-ERROR'),('error_keeps_streaming','STREAM-ERROR')])
def test_lifecycle_semantic_faults_are_detected(fault,kind):
    model,checks=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;release=e.release;start=e.start;failure=e.failure
        def broken_dispatch(fn,op,payload,tags):
            if fault=='stop_peer_runs' and fn==4 and op==18:return e.tracks[2].dispatch(18,payload,{})
            if fault=='configure_active' and fn in e.tracks and op==16 and e.tracks[fn].state==3:return e.tracks[fn].answer(e.tracks[fn].config)
            if fault=='release_no_generation' and fn in e.tracks and op==23:return b''
            if fault=='start_from_error' and fn in e.tracks and op==17 and e.tracks[fn].state==6:raise_unavailable()
            if fault=='configure_unplanned' and fn in e.tracks and op==24 and not e.tracks[fn].roles:return b''
            if fault=='stop_unconfigured' and fn in e.tracks and op==18 and e.tracks[fn].state==0:e.tracks[fn].flags|=2
            if fault=='exhaust_side_effect' and fn==4 and op==17 and e.generation==0xffffffff:
                for t in e.tracks.values():t.data.extend(b'x')
            if fault=='exhaust_wrong_cause' and fn in e.tracks and op==17 and e.tracks[fn].generation==0xffffffff:raise_unavailable()
            result=dispatch(fn,op,payload,tags)
            if fault=='discard_done' and fn==4 and op==18:e.tracks[2].data.clear();e.tracks[2].segments.clear()
            if fault=='query_applies' and fn==2 and op==24:e.tracks[2].config['samples']=5
            if fault=='stop_done_clears' and fn in e.tracks and op==18 and e.tracks[fn].state==4:e.tracks[fn].segments.clear()
            if fault=='stop_error_keeps' and fn in e.tracks and op==18 and e.tracks[fn].state==1 and e.tracks[fn].error:e.tracks[fn].state=6
            if op==20 and fn in e.tracks and ((fn==3 and fault=='wrong_write_pos') or (fault=='failed_position' and e.tracks[fn].state==6)):
                result=result[:5]+(int.from_bytes(result[5:13],'little')+1).to_bytes(8,'little')+result[13:]
            return result
        def raise_unavailable():
            from oep_client.virtual_capture_timeline_model import refusal
            raise refusal(6)
        def broken_release():
            kept={fn:(list(t.current),t.config) for fn,t in e.tracks.items()};release()
            if fault=='keep_cache':
                for fn,t in e.tracks.items():t.current[:],t.config=kept[fn]
            if fault=='reset_generation':e.generation=0
        def broken_start():
            old={fn:(bytes(t.data),list(t.segments)) for fn,t in e.tracks.items()}
            if fault=='wrap_group_generation' and e.generation==0xffffffff:e.generation=0
            if fault=='wrap_track_generation':
                for t in e.tracks.values():
                    if t.generation==0xffffffff:t.generation=0
            if fault=='failure_rollback':generations={fn:e.tracks[fn].generation for fn in e.bound}
            result=start()
            if fault=='restart_keeps_data' and any(old[fn][1] for fn in old):
                for fn,t in e.tracks.items():t.data[:],t.segments[:]=old[fn]
            if fault=='restart_origin':
                for t in e.tracks.values():t.origin-=1000000
            if isinstance(result,Completed):
                if fault=='failure_success':result=bytes(result)
                if fault=='failure_short_form':result=Completed(bytes(result)[:12],1)
                if fault=='failure_state':
                    for t in e.tracks.values():
                        if t.state==6:t.state=1
                if fault=='failure_peer_runs':
                    for fn in e.bound:
                        if e.tracks[fn].state==1:e.tracks[fn].state=3;e.tracks[fn].origin=e.start_ns
                if fault=='failure_rollback':
                    for fn,g in generations.items():e.tracks[fn].generation=g
            return result
        def broken_failure(fn,error):
            if fault=='publish_failed':e.tracks[fn].finish(True)
            if fault=='error_publishes':e.tracks[fn].finish(True)
            failure(fn,error)
            if fault=='error_peer_runs':e.tracks[5-fn].state=3
            if fault=='wrong_error':e.tracks[fn].error=3
            if fault=='error_discards_done':e.tracks[5-fn].data.clear();e.tracks[5-fn].segments.clear()
            if fault=='error_resets_done_state':e.tracks[5-fn].state=1
            if fault=='error_write_pos':e.tracks[fn].position+=1
            if fault=='error_keeps_streaming':e.tracks[5-fn].state=3
        e.dispatch_request=broken_dispatch;e.release=broken_release;e.start=broken_start;e.failure=broken_failure
        for fn,track in e.tracks.items():
            finish,event,fail,dispatch_track=track.finish,track.event,track.fail,track.dispatch
            def broken_finish(short=False,track=track,finish=finish):
                finish(False if fault=='no_short' else short)
                if fault=='old_generation' and track.segments and track.fn==3:row=track.segments[-1];track.segments[-1]=row[:7]+(0,)
            def broken_event(kind,fixed,event=event):
                if fault=='error_wrong_reason' and kind==2 and fixed[0]==3:fixed=b'\1\0'+fixed[2:]
                event(kind,fixed)
            def broken_track_dispatch(op,payload,tags,track=track,dispatch_track=dispatch_track):
                if fault in ('failure_success','failure_state') and op==17 and track.fail_start is not None:
                    result=dispatch_track(op,payload,tags)
                    if fault=='failure_success':return bytes(result)
                    track.state=3;return result
                return dispatch_track(op,payload,tags)
            track.finish=broken_finish;track.event=broken_event;track.dispatch=broken_track_dispatch
    model.install=faulty_install
    if fault=='late_expiry':
        advance=model.timeline_advance_to
        def broken_advance(ns):
            core=model.ep.current_core;old=core.deadline
            if core.holder is not None and ns>=old*1000000-model.ep._origin_ns:core.deadline+=1
            try:return advance(ns)
            finally:core.deadline=old
        model.timeline_advance_to=broken_advance
    result=run(model,checks,(2,kind));assert failed(result),result['checks']


@pytest.mark.parametrize('kind,op',[('REPLAY-START',17),('REPLAY-STOP',18)])
def test_lifecycle_replayed_effects_are_detected(kind,op):
    model,checks=setup();original=model.handle;seen=set()
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==4 and req[5]==op:
            if req in seen:
                model.extension.generation+=1
                model.extension.tracks[2].current.clear()
            seen.add(req)
        return reply
    checks.send=broken;assert failed(run(model,checks,(2,kind)))


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_lifecycle_preflight_blocks(invalid):
    model,checks=setup();adapter=SampleCaptureTimelineAdapter(model)
    if invalid=='scope':adapter.reset_scope='physical'
    else:adapter.selection={4:'oep.fixture.capture-group'}
    result=CaptureTimelineLifecycleChecks(checks,adapter).run()
    assert result['checks'][0]['status']=='failed' and all(r['status']=='blocked' for r in result['checks'][1:])
    assert not adapter.trace
