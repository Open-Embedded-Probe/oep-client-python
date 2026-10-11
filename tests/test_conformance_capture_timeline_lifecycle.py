import pytest
from oep_client.conformance_capture_timeline_lifecycle import CaptureTimelineLifecycleChecks
from oep_client.conformance_capture_timeline_sample import SampleCaptureTimelineAdapter
from test_conformance_capture_timeline import setup


@pytest.mark.parametrize('scenario',CaptureTimelineLifecycleChecks.scenarios)
def test_timeline_lifecycle(scenario):
    model,checks=setup();checker=CaptureTimelineLifecycleChecks(checks,SampleCaptureTimelineAdapter(model));checker.scenarios=(scenario,)
    result=checker.run()
    assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
    assert not result['full_conformance']


@pytest.mark.parametrize('scenario',CaptureTimelineLifecycleChecks.scenarios)
def test_lifecycle_wire_fault_is_detected(scenario):
    model,checks=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==4:reply=reply[:4]+bytes(((reply[4]+1)&255,))+reply[5:]
        return reply
    checks.send=broken;checker=CaptureTimelineLifecycleChecks(checks,SampleCaptureTimelineAdapter(model));checker.scenarios=(scenario,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-TIMELINE-LIFE-0')['status']=='failed'


@pytest.mark.parametrize('fault,kind',[
    ('stop_peer_runs','STOP-ACTIVE'),('no_short','STOP-ACTIVE'),
    ('discard_done','STOP-MIXED'),('old_generation','STOP-ACTIVE'),
    ('wrong_write_pos','STOP-ACTIVE'),('query_applies','QUERY-ACTIVE'),
    ('keep_cache','END-ACTIVE'),('reset_generation','END-ACTIVE'),
    ('restart_keeps_grid','STOP-ACTIVE'),('late_expiry','EXPIRY-WAIT'),
    ('error_peer_runs','ERROR-SOURCE'),('publish_failed','ERROR-SOURCE'),
    ('wrong_error','ERROR-SOURCE'),('failed_position','ERROR-SOURCE'),
    ('restart_origin','STOP-ACTIVE')])
def test_lifecycle_semantic_faults_are_detected(fault,kind):
    model,checks=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;release=e.release_plans;start=e.start
        def broken_dispatch(fn,op,payload,tags):
            if fault=='stop_peer_runs' and fn==4 and op==18:return e.tracks[2].dispatch_request(2,18,payload,{})
            result=dispatch(fn,op,payload,tags)
            if fault=='discard_done' and fn==4 and op==18:e.tracks[2].data.clear();e.tracks[2].segments.clear()
            if fault=='query_applies' and fn==2 and op==24:e.tracks[2].config['samples']=5
            if op==20 and ((fn==3 and fault=='wrong_write_pos') or (fn==2 and fault=='failed_position' and e.tracks[2].state==6)):
                result=result[:5]+(int.from_bytes(result[5:13],'little')+1).to_bytes(8,'little')+result[13:]
            return result
        def broken_release(functions):
            before=dict(e.grids);release(functions)
            if fault=='keep_cache':e.grids=before
            if fault=='reset_generation':e.generation=0
        def broken_start():
            old=dict(e.grids);result=start()
            if fault=='restart_keeps_grid' and old:e.grids=old
            if fault=='restart_origin' and old:
                for g in e.grids.values():g['origin']-=1000000
            return result
        e.dispatch_request=broken_dispatch;e.release_plans=broken_release;e.start=broken_start
        track=e.tracks[3];finish=track.finish
        def broken_finish(short=False):
            finish(False if fault=='no_short' else short)
            if fault=='old_generation' and track.segments:
                row=track.segments[-1];track.segments[-1]=row[:7]+(0,)
        track.finish=broken_finish
    model.install=faulty_install
    if fault=='late_expiry':
        advance=model.timeline_advance_to
        def broken_advance(ns):
            core=model.ep.current_core;old=core.deadline
            if core.holder is not None and ns>=old*1000000-model.ep._origin_ns:core.deadline+=1
            try:return advance(ns)
            finally:core.deadline=old
        model.timeline_advance_to=broken_advance
    if fault in ('error_peer_runs','publish_failed','wrong_error'):
        fail=model.group_fail
        def broken_fail(fn,error):
            if fault=='publish_failed':model.extension.tracks[fn].finish(True)
            fail(fn,error)
            if fault=='error_peer_runs':model.extension.tracks[5-fn].state=3
            if fault=='wrong_error':model.extension.tracks[fn].error=3
        model.group_fail=broken_fail
    checker=CaptureTimelineLifecycleChecks(checks,SampleCaptureTimelineAdapter(model));checker.scenarios=((2,kind),)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-TIMELINE-LIFE-0')['status']=='failed',result['checks']


@pytest.mark.parametrize('kind,op',[('REPLAY-START-WAIT',17),('REPLAY-START-ACTIVE',17),('REPLAY-STOP',18)])
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
    checks.send=broken;checker=CaptureTimelineLifecycleChecks(checks,SampleCaptureTimelineAdapter(model));checker.scenarios=((2,kind),)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-TIMELINE-LIFE-0')['status']=='failed'


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_lifecycle_preflight_blocks(invalid):
    model,checks=setup();adapter=SampleCaptureTimelineAdapter(model)
    if invalid=='scope':adapter.reset_scope='physical'
    else:adapter.selection={4:'oep.fixture.capture-group'}
    result=CaptureTimelineLifecycleChecks(checks,adapter).run()
    assert result['checks'][0]['status']=='failed' and all(r['status']=='blocked' for r in result['checks'][1:])
    assert not adapter.trace
