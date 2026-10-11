"""Capture §2.2/3.2 generation, release and error lifetime in the virtual endpoint."""
import copy
import struct
import pytest
from oep_client import capture as c,host
from test_virtual_bench_capture_requests import configured,snapshot


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('seed',[0,0xfffffffe,0xffffffff])
def test_restart_generation_wrap_and_old_read_release_are_atomic(analog,seed):
    ep,hst,track,_=configured(analog);cap=ep.captures[track.fn];cap.generation=seed
    track.start();first=track.generation;track.wait();track.start();current=track.generation
    assert first==((seed+1)&0xffffffff or 1) and current==((first+1)&0xffffffff or 1)
    before=snapshot(ep,track.fn)
    for op,body in [(track.READ,struct.pack('<IQI',first,0,8)),(track.RELEASE,struct.pack('<II',first,0))]:
        with pytest.raises(host.Unavailable) as err:hst.request(track.fn,op,body)
        assert err.value.cause=='wrong_state' and snapshot(ep,track.fn)==before
    seg,=track.segments();assert seg.generation==current and seg.serial==0 and seg.position==0


@pytest.mark.parametrize('analog',[False,True])
def test_one_shot_release_is_a_noop(analog):
    ep,_,track,_=configured(analog);before=copy.deepcopy(ep.captures[track.fn]);track.release(0)
    assert ep.captures[track.fn]==before


@pytest.mark.parametrize('analog',[False,True])
def test_release_that_frees_no_segment_keeps_paused_state(analog):
    ep,_,track,clock=configured(analog);track.configure(rate=10000 if analog else 1000000,mode=c.REPEAT,samples=8,segments=1);track.start();clock.t=50
    assert track.status().state==c.STATE['paused'];before=copy.deepcopy(ep.captures[track.fn])
    track.release(0xffffffff)
    assert ep.captures[track.fn]==before,'release older than first segment resumed a full ring'


@pytest.mark.parametrize('analog',[False,True])
def test_release_frees_inclusive_data_then_resumes_with_gap(analog):
    ep,_,track,clock=configured(analog);track.configure(rate=10000 if analog else 1000000,mode=c.REPEAT,samples=8,segments=1);track.start();clock.t=50
    assert track.status().state==c.STATE['paused'];first,=track.segments();old_end=first.position+len(track.read_segment(first))
    track.release(first.serial);assert track.status().state==c.STATE['capturing']
    reply=ep.captures[track.fn].read(track.generation,0,8,128);position,flags,count=struct.unpack_from('<QBI',reply)
    assert position==old_end and flags&2 and count==0
    clock.t=52;next_segment,=track.segments(first.serial+1)
    assert next_segment.flags&1 and next_segment.generation==track.generation and next_segment.position==old_end
    assert next_segment.start_ns==50000000+(5000 if analog else 0)


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('error',[1,2,3])
def test_error_preserves_committed_data_and_restart_clears_flags(analog,error):
    ep,_,track,clock=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=8);track.start()
    cap=ep.captures[track.fn];clock.t=9
    track.status();assert cap.segs;before=bytes(cap.data);segs=copy.deepcopy(cap.segs)
    ep.capture_overflow(track.fn,error);status=track.status()
    assert status.state==c.STATE['error'] and status.error==error and status.flags&1
    assert bytes(cap.data)==before and cap.segs==segs and status.write_pos==len(before)
    old_generation=track.generation;track.start();assert track.generation==old_generation+1
    assert cap.flags==0 and cap.error==0 and not cap.data and not cap.segs


@pytest.mark.parametrize('analog',[False,True])
def test_ring_pauses_as_soon_as_last_available_segment_finishes(analog):
    ep,_,track,clock=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=1);track.start();clock.t=8
    assert track.status().state==c.STATE['paused']


@pytest.mark.parametrize('analog',[False,True])
def test_stop_commits_a_short_partial_segment(analog):
    ep,_,track,clock=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=2);track.start();clock.t=3;track.stop()
    assert track.status().state==c.STATE['configured'];segment,=track.segments()
    assert segment.samples==3 and segment.flags&2 and segment.generation==track.generation
    assert len(track.read_segment(segment))==(12 if analog else 1)


@pytest.mark.parametrize('analog',[False,True])
def test_stop_before_first_sample_does_not_create_an_empty_segment(analog):
    ep,_,track,_=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=2);track.start();track.stop()
    assert track.status().state==c.STATE['configured'] and track.segments()==[]


@pytest.mark.parametrize('analog',[False,True])
def test_stop_after_error_returns_to_configured_without_discarding_data(analog):
    ep,_,track,clock=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=2);track.start();clock.t=9;track.status();ep.capture_overflow(track.fn)
    cap=ep.captures[track.fn];before=(bytes(cap.data),copy.deepcopy(cap.segs),cap.generation)
    track.stop();assert track.status().state==c.STATE['configured']
    assert (bytes(cap.data),cap.segs,cap.generation)==before


def test_group_stop_commits_short_segments_on_both_tracks():
    from test_virtual_bench_capture import bench
    from oep_client import core
    ep,hst,logic,clock=bench();analog=c.AnalogCapture(hst)
    core.plan_apply(hst,[(logic.fn,0,20),(logic.fn,1,21),(analog.fn,0,16),(analog.fn,1,17)])
    for track in (logic,analog):track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=2)
    group=c.CaptureGroup(hst);group.bind([logic,analog]);group.start();clock.t=3;group.stop()
    for track in (logic,analog):
        assert track.status().state==c.STATE['configured'];segment,=track.segments()
        assert segment.samples==3 and segment.flags&2 and segment.generation==track.generation
