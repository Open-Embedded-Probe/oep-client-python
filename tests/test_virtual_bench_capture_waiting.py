"""Capture §3.2/3.3/4.1: unreachable threshold must wait, never fabricate a trigger."""
import struct
from oep_client.registry import FIXTURE_LOGIC as _LOGIC
STOPPED_ERROR = _LOGIC.enum['stopped_reason']['error']   # capture §3.4, by name
from fractions import Fraction
import pytest
from oep_client import capture as c,core,host
from test_virtual_bench_capture_requests import configured,snapshot
from test_virtual_bench_capture_notifications import events


def waiting():
    ep,hst,track,clock=configured()
    track.configure(rate=1000,samples=8,trigger=(c.CROSS_UP,0,0),pretrigger=4)
    track.subscribe();track.start()
    return ep,hst,track,clock


def test_unreachable_threshold_waits_without_segments_data_or_events():
    ep,_,track,clock=waiting();generation=track.generation;origin=clock.t
    for t in [0,1,20,1000]:
        clock.t=origin+t;status=track.status()
        assert status.state==c.STATE['waiting'] and status.generation==generation
        assert track.segments()==[] and events(ep,track.fn)==[]
        assert not ep.captures[track.fn].data


@pytest.mark.parametrize('operation',['start','configure'])
def test_waiting_rejects_restart_and_configuration_atomically(operation):
    ep,_,track,_=waiting();before=snapshot(ep,track.fn)
    with pytest.raises(host.Unavailable) as error:
        track.start() if operation=='start' else track.configure(rate=1000,samples=8)
    assert error.value.cause=='wrong_state' and snapshot(ep,track.fn)==before


def test_waiting_stop_notifies_host_reason_without_fabricated_segment():
    ep,_,track,clock=waiting();clock.t=20;track.stop()
    assert track.status().state==c.STATE['configured'] and track.segments()==[]
    rows=events(ep,track.fn);assert [r[0] for r in rows]==[c.EVENT_STOPPED]
    assert rows[0][1][0]==1 and rows[0][1][-1]==track.generation


@pytest.mark.parametrize('error',[1,2,3])
def test_waiting_error_has_matching_stopped_reason_and_no_segment(error):
    ep,_,track,_=waiting();ep.capture_overflow(track.fn,error)
    status=track.status();assert status.state==c.STATE['error'] and status.error==error
    assert track.segments()==[]
    rows=events(ep,track.fn);assert [r[0] for r in rows]==[c.EVENT_STOPPED]
    assert rows[0][1]==(STOPPED_ERROR,error,track.generation)


@pytest.mark.parametrize('elapsed,index,count',[(0,0,4),(2,2,6),(20,4,8)])
def test_force_uses_current_sample_and_available_pretrigger(elapsed,index,count):
    ep,_,track,clock=waiting();generation=track.generation;clock.t+=elapsed;forced_ns=clock.t*1000000;track.force();clock.t+=10
    segment,=track.segments();assert segment.trigger_index==index and segment.samples==count
    assert not segment.flags&2 and track.generation==generation   # bit1 is stop's (§2.2)
    rows=events(ep,track.fn);assert [r[0] for r in rows]==[c.EVENT_TRIGGERED,c.EVENT_SEGMENT,c.EVENT_STOPPED]
    expected=forced_ns
    assert rows[0][1][1]==index and rows[0][1][2]==expected
    assert rows[0][1][-1]==generation and track.status().state==c.STATE['done']
    assert abs(segment.start_ns+Fraction(index*1000000000)/track.config.rate-rows[0][1][2])<=Fraction(500000000)/track.config.rate
    track.force();assert events(ep,track.fn)==[] and track.segments()==[segment]


def grouped():
    ep,hst,analog,clock=configured();logic=c.LogicCapture(hst)
    core.plan_apply(hst,[(analog.fn,0,16),(analog.fn,1,17),(logic.fn,0,20),(logic.fn,1,21)])
    analog.configure(rate=1000,samples=8,trigger=(c.CROSS_UP,0,0),pretrigger=4)
    logic.configure(rate=1000,samples=8)
    group=c.CaptureGroup(hst);group.bind([analog,logic],trigger=analog)
    for track in [analog,logic]:track.subscribe()
    hst.subscribe(group.fn);group.start([analog,logic])
    return ep,group,analog,logic,clock


def test_waiting_release_does_not_trigger_or_discard_state():
    ep,_,track,_=waiting();before=snapshot(ep,track.fn)
    track.release(0)
    assert snapshot(ep,track.fn)==before and events(ep,track.fn)==[]
