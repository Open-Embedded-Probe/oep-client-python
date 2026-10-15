"""Capture §3.3/4.1: first observed trigger, retained prehistory and short initial segment."""
from fractions import Fraction
import struct
import pytest
from oep_client import capture as c,core
from test_virtual_bench_capture import bench
from test_virtual_bench_capture_notifications import events


@pytest.mark.parametrize('kind,role,value,first',[(c.EDGE,1,0,2),(c.EDGE,2,1,8),(c.LEVEL,3,1,8),(c.LEVEL,0,0,0)])
@pytest.mark.parametrize('pre',[0,3,10])
def test_logic_fires_at_first_observed_condition_and_keeps_available_history(kind,role,value,first,pre):
    ep,hst,track,clock=bench();core.plan_apply(hst,[(track.fn,r,20+r) for r in range(4)])
    track.configure(rate=1000000,samples=64,trigger=(kind,role,value),pretrigger=pre);track.subscribe();track.start()
    clock.t=1
    segment,=track.segments();available=min(pre,first);begin=first-available;count=64-pre+available
    assert (segment.trigger_index,segment.samples)==(available,count)
    assert not segment.flags&2 and segment.position==0   # bit1 is stop's (§2.2)
    assert segment.start_ns==begin*1000
    raw=track.read_segment(segment)
    expected=bytearray((count+1)//2)
    for i in range(count):expected[i//2]|=((begin+i)&15)<<((i%2)*4)
    # Last unused nibble is unconstrained.
    if count%2:raw=raw[:-1]+bytes((raw[-1]&15,))
    assert raw==bytes(expected)
    rows=events(ep,track.fn);assert [r[0] for r in rows]==[c.EVENT_TRIGGERED,c.EVENT_SEGMENT,c.EVENT_STOPPED]
    assert rows[0][1]==(0,available,first*1000,track.generation)
    assert track.status().state==c.STATE['done']


@pytest.mark.parametrize('kind,role,first',[(c.CROSS_UP,0,64),(c.CROSS_DOWN,0,32),(c.CROSS_UP,3,5)])
@pytest.mark.parametrize('pre',[0,10])
def test_analog_crossing_keeps_bounded_history_and_no_sample_before_start(kind,role,first,pre):
    ep,hst,_,clock=bench();track=c.AnalogCapture(hst);core.plan_apply(hst,[(track.fn,role,16)])
    track.configure(rate=1000,samples=40,trigger=(kind,role,2500),pretrigger=pre);track.subscribe();track.start()
    clock.t=first+40  # Include the post-trigger record and analog start offset.
    segment,=track.segments();available=min(pre,first);count=40-pre+available
    assert segment.trigger_index==available and segment.samples==count and not segment.flags&2
    origin=ep.captures[track.fn].start_offset_ns
    assert segment.start_ns==origin+int(Fraction((first-available)*1000000000)/track.config.rate)
    rows=events(ep,track.fn);assert rows[0][1][1]==available
    assert rows[0][1][2]==origin+int(Fraction(first*1000000000)/track.config.rate)
    values=track.values(track.read_segment(segment),role)
    if available:
        assert (values[available-1]<2500<=values[available]) if kind==c.CROSS_UP else (values[available-1]>2500>=values[available])
    if role==0:
        assert values==[4095 if (first-available+i)%64<32 else 0 for i in range(count)]
