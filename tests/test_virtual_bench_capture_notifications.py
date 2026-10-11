"""Capture §3.4 notifications, with independent fixed-part wire decoding."""
import struct
from types import SimpleNamespace
import pytest
from oep_client import capture as c,message as m
from test_virtual_bench_capture_requests import configured


def events(ep,fn):
    result=[]
    for frame in ep.pushes():
        if frame[0]!=m.ROLE_EVENT or struct.unpack_from('<H',frame,1)[0]!=fn:continue
        kind=frame[5];body=frame[6:]
        if kind==c.EVENT_SEGMENT:
            fixed=struct.unpack_from('<IQIQIIBI',body);tail=body[37:];generation=fixed[-1]
        elif kind==c.EVENT_STOPPED:
            fixed=struct.unpack_from('<BBI',body);tail=body[6:];generation=fixed[-1]
        else:
            assert kind==c.EVENT_TRIGGERED
            fixed=struct.unpack_from('<IIQI',body);tail=body[20:];generation=fixed[-1]
        m.Tail.parse(tail)
        decoded=c.unpack_event(frame);assert decoded.generation==generation
        result.append((kind,fixed,frame))
    return result


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('subscribed',[False,True])
def test_one_shot_completion_matches_polling_and_generation(analog,subscribed):
    ep,_,track,clock=configured(analog)
    if subscribed:track.subscribe()
    track.start();clock.t+=1;rows=events(ep,track.fn)
    if not subscribed:assert rows==[];return
    segment,=track.segments();kinds=[row[0] for row in rows]
    assert kinds==[c.EVENT_SEGMENT,c.EVENT_STOPPED]
    assert rows[0][1]==(segment.serial,segment.position,segment.samples,segment.start_ns,segment.start_uncertainty_ns,0xffffffff,segment.flags,track.generation)
    assert rows[1][1][0]==0 and rows[1][1][-1]==track.generation


@pytest.mark.parametrize('analog',[False,True])
def test_short_stop_notifies_the_actual_short_segment_and_host_reason(analog):
    ep,_,track,clock=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=2);track.subscribe();track.start();clock.t+=3;track.stop()
    rows=events(ep,track.fn);assert [row[0] for row in rows]==[c.EVENT_SEGMENT,c.EVENT_STOPPED]
    assert rows[0][1][2]==3 and rows[0][1][6]&2 and rows[0][1][-1]==track.generation
    assert rows[1][1][0]==1 and rows[1][1][-1]==track.generation


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('error',[1,2,3])
def test_error_notification_matches_status_without_incomplete_segment(analog,error):
    ep,_,track,clock=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=2);track.subscribe();track.start();clock.t=9;track.status();events(ep,track.fn)
    ep.capture_overflow(track.fn,error);rows=events(ep,track.fn)
    assert [row[0] for row in rows]==[c.EVENT_STOPPED]
    status=track.status();assert rows[0][1]==(3,status.error,status.generation)


@pytest.mark.parametrize('analog',[False,True])
def test_full_ring_and_release_do_not_notify_reserved_stopped_reason(analog):
    ep,_,track,clock=configured(analog);track.configure(rate=1000,mode=c.REPEAT,samples=8,segments=1);track.subscribe();track.start();clock.t+=8
    assert track.status().state==c.STATE['paused'];rows=events(ep,track.fn)
    assert [row[0] for row in rows]==[c.EVENT_SEGMENT]
    track.release(0);assert events(ep,track.fn)==[]
    clock.t+=8;track.status();rows=events(ep,track.fn)
    assert [row[0] for row in rows]==[c.EVENT_SEGMENT] and rows[0][1][6]&1


@pytest.mark.parametrize('analog',[False,True])
def test_queued_old_events_keep_old_generation_and_host_discards_them(analog):
    ep,_,track,clock=configured(analog);track.subscribe();track.start();old=track.generation;clock.t+=1;track.status();track.start();current=track.generation;clock.t+=1;track.status()
    rows=events(ep,track.fn);assert [row[1][-1] for row in rows]==[old,old,current,current]
    link=SimpleNamespace(events=[row[2] for row in rows]);chosen=track.events(link)
    assert [event.generation for event in chosen]==[current,current] and track.stale_events==2 and link.events==[]


@pytest.mark.parametrize('analog',[False,True])
def test_reachable_trigger_event_matches_segment_time_and_generation(analog):
    ep,_,track,clock=configured(analog);trigger=(c.CROSS_UP,1,2500) if analog else (c.EDGE,0,0)
    track.configure(rate=1000,samples=64,trigger=trigger,pretrigger=10);track.subscribe();track.start()
    clock.t=100
    rows=events(ep,track.fn);assert [row[0] for row in rows]==[c.EVENT_TRIGGERED,c.EVENT_SEGMENT,c.EVENT_STOPPED]
    serial,index,ns,generation=rows[0][1];segment,=track.segments()
    assert serial==segment.serial and index==segment.trigger_index and generation==segment.generation==track.generation
    expected=segment.start_ns+int(index*1000000000/track.config.rate)
    assert ns==expected


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('fault',['segment_generation','stopped_generation','reserved_reason','segment_time','missing_stopped','extra_trigger'])
def test_notification_corruption_is_detected(monkeypatch,analog,fault):
    fixture=configured(analog);ep,_,track,_=fixture;original=ep._events
    def broken(fn,frames):
        changed=[]
        for frame in frames:
            data=bytearray(frame)
            if fn==track.fn:
                if fault=='missing_stopped' and frame[0]==c.EVENT_STOPPED:continue
                if (fault=='segment_generation' and frame[0]==c.EVENT_SEGMENT) or (fault=='stopped_generation' and frame[0]==c.EVENT_STOPPED):data[-1]^=1
                if fault=='reserved_reason' and frame[0]==c.EVENT_STOPPED:data[1]=2
                if fault=='segment_time' and frame[0]==c.EVENT_SEGMENT:struct.pack_into('<Q',data,17,struct.unpack_from('<Q',data,17)[0]+1)
                if fault=='extra_trigger' and frame[0]==c.EVENT_SEGMENT:changed.append(bytes((c.EVENT_TRIGGERED,))+struct.pack('<IIQI',0,0,0,track.generation))
            changed.append(bytes(data))
        original(fn,changed)
    ep._events=broken
    monkeypatch.setattr(__import__(__name__,fromlist=['configured']),'configured',lambda chosen:fixture)
    with pytest.raises(AssertionError):test_one_shot_completion_matches_polling_and_generation(analog,True)


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('unused_error',[1,255])
def test_error_field_outside_error_reason_is_not_constrained(monkeypatch,analog,unused_error):
    fixture=configured(analog);ep,_,track,_=fixture;original=ep._events
    def altered(fn,frames):
        changed=[]
        for frame in frames:
            data=bytearray(frame)
            if fn==track.fn and frame[0]==c.EVENT_STOPPED and frame[1]!=3:data[2]=unused_error
            changed.append(bytes(data))
        original(fn,changed)
    ep._events=altered
    monkeypatch.setattr(__import__(__name__,fromlist=['configured']),'configured',lambda chosen:fixture)
    test_one_shot_completion_matches_polling_and_generation(analog,True)
