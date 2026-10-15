"""Capture §2.1/3.2/3.3: trigger is an initial gate for repeat and streaming too."""
from fractions import Fraction
import struct
import pytest
from oep_client import capture as c,core,message as m
from test_virtual_bench_capture import bench


def setup(analog,mode,pre=0,unreachable=True,internal_samples=None):
    ep,hst,logic,clock=bench();track=c.AnalogCapture(hst) if analog else logic
    roles=(1,3) if analog else (0,1)
    core.plan_apply(hst,[(track.fn,r,(16 if analog else 20)+i) for i,r in enumerate(roles)])
    if unreachable:ep.captures[track.fn].value=lambda role,index:0
    trigger=(c.CROSS_UP,3,2500) if analog else (c.EDGE,0,0)
    track.configure(rate=1000,mode=mode,samples=32 if mode==c.REPEAT else None,segments=3 if mode==c.REPEAT else None,trigger=trigger,pretrigger=pre)
    if internal_samples is not None:ep.captures[track.fn].samples=internal_samples  # explicit prototype-owned stream geometry
    track.subscribe();track.start()
    return ep,track,clock


def frames(ep,track):
    return [f for f in ep.pushes() if struct.unpack_from('<H',f,1)[0]==track.fn]


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('mode',[c.REPEAT,c.STREAMING])
def test_unreachable_condition_remains_waiting_without_data_or_events(analog,mode):
    ep,track,clock=setup(analog,mode)
    for t in [0,20,1000]:
        clock.t=t;status=track.status()
        assert status.state==c.STATE['waiting'] and status.write_pos==0 and status.serial_done==0
        assert frames(ep,track)==[] and track.segments()==[]


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('mode',[c.REPEAT,c.STREAMING])
@pytest.mark.parametrize('pre',[0,4])
def test_force_opens_gate_once_and_retains_pretrigger_for_initial_record(analog,mode,pre):
    ep,track,clock=setup(analog,mode,pre);clock.t=20
    assert track.status().state==c.STATE['waiting'];generation=track.generation
    track.force();initial=frames(ep,track);notices=[f for f in initial if f[0]==m.ROLE_EVENT]
    assert [f[5] for f in notices]==[c.EVENT_TRIGGERED]
    event=c.unpack_event(notices[0]);assert event.trigger_index==pre and event.generation==generation
    assert track.status().state==c.STATE['capturing']
    if mode==c.REPEAT:
        clock.t=60;segment=track.segments()[0]
        assert segment.trigger_index==pre and segment.samples==32 and not segment.flags&2
        assert abs(segment.start_ns+Fraction(pre*1000000000)/track.config.rate-event.trigger_ns)<=Fraction(500000000)/track.config.rate
    else:
        assert len(b''.join(c.unpack_push(f)[3] for f in initial if f[0]==m.ROLE_DATA))==(pre*4 if analog else pre//4)
    track.force();assert not [f for f in frames(ep,track) if f[0]==m.ROLE_EVENT and f[5]==c.EVENT_TRIGGERED]


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('mode',[c.REPEAT,c.STREAMING])
@pytest.mark.parametrize('pre',[0,10])
def test_known_waveform_has_one_initial_trigger_and_correct_retained_history(analog,mode,pre):
    ep,track,clock=setup(analog,mode,pre,False);first=5 if analog else 1;available=min(first,pre)
    clock.t=first+1
    initial=frames(ep,track);events=[f for f in initial if f[0]==m.ROLE_EVENT]
    assert [f[5] for f in events]==[c.EVENT_TRIGGERED]
    trigger=c.unpack_event(events[0]);assert trigger.trigger_index==available
    origin=ep.captures[track.fn].t0_ns
    assert trigger.trigger_ns==origin+int(Fraction(first*1000000000)/track.config.rate)+(track.config.skew_ns[3] if analog else 0)
    clock.t=70;later=frames(ep,track)
    assert not [f for f in later if f[0]==m.ROLE_EVENT and f[5]==c.EVENT_TRIGGERED]
    all_frames=initial+later
    if mode==c.REPEAT:
        segments=track.segments();segment=segments[0]
        assert segment.samples==32-pre+available and segment.trigger_index==available
        assert not segment.flags&2   # bit1 is stop's (§2.2)
        assert all(s.trigger_index is None for s in segments[1:])
        assert segment.start_ns==origin+int(Fraction((first-available)*1000000000)/track.config.rate)
    else:
        assert not [f for f in all_frames if f[0]==m.ROLE_EVENT and f[5]==c.EVENT_SEGMENT]
        data=b''.join(c.unpack_push(f)[3] for f in all_frames if f[0]==m.ROLE_DATA)
        assert data
        if analog:assert track.values(data,3)[0]==(2048 if available else 2545)
        else:assert track.channel(data,0)[0]==((first-available)&1)


@pytest.mark.parametrize('pre,serial_done,committed',[(0,0,0),(10,1,44)])
def test_stream_error_rolls_back_to_real_completed_boundary_after_trigger(pre,serial_done,committed):
    ep,track,clock=setup(True,c.STREAMING,pre,False,internal_samples=16)
    clock.t=20;sent=frames(ep,track);assert [f for f in sent if f[0]==m.ROLE_DATA]
    assert track.status().serial_done==serial_done
    ep.capture_overflow(track.fn,2);status=track.status()
    assert status.state==c.STATE['error'] and status.error==2 and status.write_pos==committed
    stopped=[c.unpack_event(f) for f in frames(ep,track) if f[0]==m.ROLE_EVENT]
    assert len(stopped)==1 and stopped[0].kind=='stopped' and stopped[0].reason==3


@pytest.mark.parametrize('pre',[0,10])
def test_repeat_stop_preserves_initial_trigger_index_in_short_partial_segment(pre):
    ep,track,clock=setup(False,c.REPEAT,pre,False);frames(ep,track);clock.t=3;track.stop()
    segment,=track.segments();index=min(pre,1);begin=1-index
    assert segment.samples==3-begin and segment.trigger_index==index and segment.flags&2
