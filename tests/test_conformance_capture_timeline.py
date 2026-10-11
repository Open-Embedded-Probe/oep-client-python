import pytest
from oep_client.conformance import Checks
from oep_client.conformance_capture_timeline import CaptureTimelineChecks
from oep_client.conformance_capture_timeline_sample import SampleCaptureTimelineAdapter
from oep_client.virtual_capture_timeline_model import CaptureTimelineModel
from test_conformance_capture_group import setup as group_setup


def setup():
    _,old=group_setup();model=CaptureTimelineModel(boot_id=17)
    return model,Checks(model.handle,old.reg,'virtual-resource-1')


@pytest.mark.parametrize('scenario',CaptureTimelineChecks.scenarios)
def test_cross_rate_acquisition(scenario):
    model,checks=setup();checker=CaptureTimelineChecks(checks,SampleCaptureTimelineAdapter(model));checker.scenarios=(scenario,)
    result=checker.run()
    assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
    assert not result['full_conformance']


@pytest.mark.parametrize('kind',['declarations','metadata'])
def test_timeline_declarations(kind):
    from oep_client.conformance_interfaces import InterfaceChecks
    from oep_client.conformance_capture_metadata import CaptureMetadataChecks
    model,checks=setup()
    result=(InterfaceChecks(checks) if kind=='declarations' else CaptureMetadataChecks(checks,{2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'})).run()
    assert result['status']=='passed',result['checks']


@pytest.mark.parametrize('method,args',[('timeline_program',(2,True)),('timeline_program',(7,1)),('timeline_advance_to',(-1,)),('timeline_advance_to',(True,)),('group_feed',([],))])
def test_bad_scheduler_controls_do_not_mutate(method,args):
    model,_=setup();before=model.group_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.group_state()==before


@pytest.mark.parametrize('scenario',CaptureTimelineChecks.scenarios)
def test_wire_failure_is_detected(scenario):
    model,checks=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==4:reply=reply[:4]+bytes(((reply[4]+1)&255,))+reply[5:]
        return reply
    checks.send=broken;checker=CaptureTimelineChecks(checks,SampleCaptureTimelineAdapter(model));checker.scenarios=(scenario,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-TIMELINE-0')['status']=='failed'


@pytest.mark.parametrize('fault',['future','missing_history','wrong_rate','adc_data','logic_data','floor_nearest','no_short','old_generation','keep_cache','skip_sample'])
def test_scheduler_and_data_faults_are_detected(fault):
    model,checks=setup();install=model.install
    def faulty_install():
        install();e=model.extension;arrival=e.arrival;sample=e.sample;fire=e.fire;release=e.release_plans
        def broken_arrival(fn):
            arrival(fn)
            if fault=='missing_history' and e.state()==2 and fn==3:e.grids[fn]['values'].clear()
            if fault=='skip_sample' and fn==3:e.grids[fn]['next']+=1
        def broken_sample(fn,index):
            result=sample(fn,index)
            if fault=='adc_data' and fn==2:result[0]^=1
            if fault=='logic_data' and fn==3:result[1]^=1
            return result
        def broken_fire(forced=False):
            fire(forced)
            if fault=='floor_nearest':e.tracks[3].trigger_index=max(0,e.tracks[3].trigger_index-1)
        def broken_release(functions):
            before=dict(e.grids);release(functions)
            if fault=='keep_cache':e.grids=before
        e.arrival=broken_arrival;e.sample=broken_sample;e.fire=broken_fire;e.release_plans=broken_release
        for fn in (2,3):
            track=e.tracks[fn];finish=track.finish
            def broken_finish(short=False,track=track,finish=finish):
                finish(False if fault=='no_short' else short)
                if fault=='old_generation' and track.segments:
                    row=track.segments[-1];track.segments[-1]=row[:7]+(0,)
            track.finish=broken_finish
    model.install=faulty_install
    if fault=='future':
        original=model.timeline_advance_to
        model.timeline_advance_to=lambda ns:original(ns+10000000)
    if fault=='wrong_rate':
        program=model.timeline_program
        def broken_program(fn,rise):
            program(fn,rise)
            if fn==3:model.extension.tracks[3].config['rate']=1000
        model.timeline_program=broken_program
    event='force0' if fault=='no_short' else 'force2500000' if fault=='floor_nearest' else 'rise3'
    checker=CaptureTimelineChecks(checks,SampleCaptureTimelineAdapter(model));checker.scenarios=((2,(1000,333),2,event),)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-TIMELINE-0')['status']=='failed',result['checks']


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_timeline_preflight_blocks(invalid):
    model,checks=setup();adapter=SampleCaptureTimelineAdapter(model)
    if invalid=='scope':adapter.reset_scope='physical'
    else:adapter.selection={4:'oep.fixture.capture-group'}
    result=CaptureTimelineChecks(checks,adapter).run()
    assert result['checks'][0]['status']=='failed' and all(r['status']=='blocked' for r in result['checks'][1:])
    assert not adapter.trace
