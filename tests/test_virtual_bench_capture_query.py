"""Capture §3.3/4.1: query with a plan is lockless and independent of acquisition/binding state."""
import copy
import struct
import pytest
from oep_client import capture as c,core,host,message as m
from test_virtual_bench_capture import bench,_configure_body


def fixture(analog,state):
    ep,hst,logic,clock=bench();track=c.AnalogCapture(hst) if analog else logic
    core.plan_apply(hst,[(track.fn,r,(16 if analog else 20)+r) for r in range(2)])
    if state==0:return ep,hst,track,clock
    track.configure(rate=1000,samples=8,mode=c.REPEAT if state in (3,5,6) else c.ONE_SHOT,segments=1 if state==5 else None)
    if state==2:
        # An explicit constant-low source never produces the configured edge/crossing.
        ep.captures[track.fn].value=lambda role,index:0
        track.configure(rate=1000,samples=8,trigger=(c.CROSS_UP,0,1) if analog else (c.EDGE,0,0))
    if state in (2,3,4,5,6):track.start()
    if state in (4,5,6):clock.t=9;track.status()
    if state==6:ep.capture_overflow(track.fn,2)
    assert track.status().state==state
    return ep,hst,track,clock


def snapshot(ep):
    return copy.deepcopy((ep.captures,ep.groups,ep.plan,ep.gpio_modes,ep.outbox))


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('state',range(7))
def test_query_from_unopened_reader_preserves_all_acquisition_states(analog,state):
    ep,_,track,_=fixture(analog,state);before=snapshot(ep)
    reader=host.Host(lambda frame:ep.handle(frame,0))
    reply=reader.request(track.fn,track.QUERY_OP,_configure_body(rate=2000,samples=5),locked=False)
    cfg=c._config(reply.payload,analog)
    assert cfg.samples==5 and cfg.rate>1000
    assert snapshot(ep)==before


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('state',[2,3,5])
def test_query_invalid_request_does_not_change_active_acquisition(analog,state):
    ep,_,track,_=fixture(analog,state);before=snapshot(ep)
    reader=host.Host(lambda frame:ep.handle(frame,0))
    bad=b''.join(m.tlv(tag,value) for tag,value in m.split_tlvs(_configure_body(rate=1000,samples=8)) if tag!=c.RATE)+m.tlv(c.RATE,b'\0'*4)
    with pytest.raises(host.Rejected) as error:reader.request(track.fn,track.QUERY_OP,bad,locked=False)
    assert error.value.result.detail==m.MALFORMED and snapshot(ep)==before


@pytest.mark.parametrize('state',[1,3,4,5,6])
@pytest.mark.parametrize('analog',[False,True])
def test_group_bound_query_does_not_reconfigure_or_unbind_any_track(state,analog):
    ep,hst,logic,clock=bench();adc=c.AnalogCapture(hst);group=c.CaptureGroup(hst)
    core.plan_apply(hst,[(logic.fn,0,20),(adc.fn,0,16)])
    for track in [logic,adc]:track.configure(rate=1000,samples=8,mode=c.REPEAT if state in (3,5,6) else c.ONE_SHOT,segments=1 if state==5 else None)
    group.bind([logic,adc])
    if state!=1:group.start([logic,adc])
    if state in (4,5,6):clock.t=9;group.status()
    if state==6:ep.capture_overflow(adc.fn,2)
    # Group status reports 3 while member tracks are paused (capture §4.1).
    assert group.status().state==(3 if state==5 else state)
    if state==5:assert logic.status().state==adc.status().state==5
    track=adc if analog else logic;before=snapshot(ep)
    reader=host.Host(lambda frame:ep.handle(frame,0))
    reply=reader.request(track.fn,track.QUERY_OP,_configure_body(rate=2000,samples=5),locked=False)
    assert c._config(reply.payload,analog).samples==5 and snapshot(ep)==before


@pytest.mark.parametrize('analog',[False,True])
def test_query_without_plan_remains_atomic_cause6(analog):
    ep,_,logic,_=bench();track=c.AnalogCapture(logic.host) if analog else logic
    reader=host.Host(lambda frame:ep.handle(frame,0));before=snapshot(ep)
    with pytest.raises(host.Unavailable) as error:reader.request(track.fn,track.QUERY_OP,_configure_body(rate=1000,samples=8),locked=False)
    assert error.value.cause=='wrong_state' and snapshot(ep)==before


@pytest.mark.parametrize('analog',[False,True])
def test_query_does_not_suspend_elapsed_time_or_apply_candidate_settings(analog):
    ep,_,track,clock=fixture(analog,3);current=copy.deepcopy(ep.captures[track.fn]);clock.t=9
    reader=host.Host(lambda frame:ep.handle(frame,0))
    candidate=c._config(reader.request(track.fn,track.QUERY_OP,_configure_body(rate=2000,samples=5),locked=False).payload,analog)
    cap=ep.captures[track.fn]
    assert candidate.samples==5 and candidate.rate>current.rate
    assert cap.mode==current.mode and cap.samples==8 and cap.rate==current.rate and cap.generation==current.generation
    assert len(cap.segs)==1 and cap.segs[0].samples==8 and len(cap.data)>0
    assert cap.state==c.STATE['capturing']
