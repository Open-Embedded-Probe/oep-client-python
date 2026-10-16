"""Capture §1.2/3.3/3.4: channel skew gates observation; force keeps its instant."""
from fractions import Fraction
import struct
import pytest
from oep_client import capture as c, core, message as m
from oep_client.virtual_bench_capture import VirtualCapture, NO_TIME
from test_virtual_bench_capture import bench


def cap(mode, analog, channels=2, rate=Fraction(1000), trigger=False):
    track = VirtualCapture({mode}, {8}, 1, 1000000, frontends={0:(0,3300)} if analog else {})
    track.mode, track.state, track.rate = mode, c.STATE['configured'], rate
    track.samples, track.channels, track.segments_max = 8, channels, 3
    track.width, track.analog_roles = (channels*2 if analog else 8), (tuple([1,3][:channels]) if analog and channels<=2 else tuple(range(channels)) if analog else ())
    if trigger:track.trigger = (c.CROSS_UP, 3, 1500) if analog else (c.EDGE, 0, 0)
    return track


@pytest.mark.parametrize('mode',[c.ONE_SHOT,c.REPEAT,c.STREAMING])
@pytest.mark.parametrize('rate',[Fraction(1000),Fraction(1001,3)])
@pytest.mark.parametrize('channels',[2,4])
def test_analog_trigger_waits_for_sparse_role_channel_time(mode,rate,channels):
    source=cap(mode,True,channels,rate=rate,trigger=True)
    clock_ns=Fraction(0);calls=[]
    step=source.analog_roles.index(3)*int(Fraction(1000000000)/(rate*channels));origin=source.start_offset_ns
    def value(role,index):
        assert role==3
        when=origin+Fraction(index*1000000000)/rate+step
        assert when<=clock_ns,'trigger read before this role has sampled'
        calls.append(index)
        return 3000 if index>=1 else 0
    source.value=value
    source.start(0)
    # Base sample 1 exists, but its second channel still has not sampled.
    clock_ns=origin+Fraction(1000000000)/rate
    source.tick(clock_ns/1000000)
    assert source.state==c.STATE['waiting'] and calls==[0]
    before=len(calls)
    source.tick(clock_ns/1000000)
    assert len(calls)==before
    clock_ns+=step
    events=source.tick(clock_ns/1000000)
    triggered,=[e for e in events if e[0]==c.EVENT_TRIGGERED]
    assert struct.unpack_from('<IIIQQII',triggered,1)[4]==int(clock_ns)
    assert calls==[0,1] and source.state==c.STATE['capturing']
    assert source.capture_begin+source.first_trigger_index==1


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('mode',[c.ONE_SHOT,c.REPEAT,c.STREAMING])
@pytest.mark.parametrize('elapsed',[0,2,20])
@pytest.mark.parametrize('rate',[Fraction(1000),Fraction(1001,3)])
def test_force_reports_request_instant_without_offset_or_sample_rounding(analog,mode,elapsed,rate):
    track=cap(mode,analog,rate=rate,trigger=True)
    track.value=lambda role,index:0
    track.start(0)
    events=track.force(elapsed)
    event,=[e for e in events if e[0]==c.EVENT_TRIGGERED]
    assert struct.unpack_from('<IIIQQII',event,1)[4]==elapsed*1000000
    sample=track.capture_begin+track.first_trigger_index
    when=track.t0_ns+Fraction(sample*1000000000)/track.rate
    if elapsed or not analog:assert abs(when-elapsed*1000000)<=Fraction(500000000)/track.rate
    else:assert sample==0  # Before the first sample, the nearest acquisition index is 0.


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('mode',[c.REPEAT,c.STREAMING])
@pytest.mark.parametrize('channels',[1,2])
@pytest.mark.parametrize('rate',[Fraction(1000,3),Fraction(1001,3),Fraction(83333,8)])
def test_repeat_and_streaming_never_read_a_future_channel(mode,analog,channels,rate):
    track=cap(mode,analog,channels,rate);clock_ns=Fraction(0);calls=[]
    step=int(Fraction(1000000000)/(rate*channels)) if analog else 0
    def value(role,index):
        ordinal=track.analog_roles.index(role) if analog else 0
        when=track.t0_ns+Fraction(index*1000000000)/rate+ordinal*step
        assert when<=clock_ns,'future channel sample'
        calls.append(index);return index&1
    track.value=value;track.start(0)
    for elapsed in [0,1,2,3,4,10,20,50,100]:
        clock_ns=elapsed*1000000;track.tick(elapsed)
    assert calls and track.produced>0
    if mode==c.REPEAT:assert track.segs and track.state==c.STATE['paused']
    else:assert track.data and track.state==c.STATE['capturing']


@pytest.mark.parametrize('mode',[c.ONE_SHOT,c.REPEAT,c.STREAMING])
def test_wire_endpoint_waits_for_channel_skew(mode):
    ep,hst,logic,clock=bench();analog=c.AnalogCapture(hst)
    core.plan_apply(hst,[(analog.fn,1,16),(analog.fn,3,17),(logic.fn,0,20)])
    analog.configure(rate=1000,samples=8 if mode!=c.STREAMING else None,
                     segments=3 if mode==c.REPEAT else None,mode=mode,trigger=(c.CROSS_UP,3,1500))
    analog.subscribe()
    actual=ep.captures[analog.fn]
    def value(role,index):
        when=actual.t0_ns+Fraction(index*1000000000)/analog.config.rate+analog.config.skew_ns.get(role,0)
        assert when<=clock.t*1000000
        return 3000 if role==3 and index>=2 else 0
    actual.value=value
    analog.start()
    generation=analog.generation
    clock.t=2
    assert analog.status().state==c.STATE['waiting'] and ep.pushes()==[]
    clock.t=3
    assert analog.status().state==c.STATE['capturing']
    frames=ep.pushes()
    notice,=[c.unpack_event(f) for f in frames if f[0]==m.ROLE_EVENT and int.from_bytes(f[1:3],'little')==analog.fn and f[5]==c.EVENT_TRIGGERED]
    expected=actual.t0_ns+int(Fraction(2000000000)/analog.config.rate)+analog.config.skew_ns[3]
    assert notice.trigger_ns==expected and notice.generation==analog.generation==generation


@pytest.mark.parametrize('analog',[False,True])
@pytest.mark.parametrize('mode',[c.REPEAT,c.STREAMING])
@pytest.mark.parametrize('channels',[1,2])
def test_future_data_mutant_is_caught_by_channel_time_oracle(monkeypatch,analog,mode,channels):
    original=VirtualCapture.tick
    def future(self,now_ms):
        return original(self,Fraction(now_ms)+Fraction(24000)/self.rate)
    monkeypatch.setattr(VirtualCapture,'tick',future)
    with pytest.raises(AssertionError,match='future channel sample'):
        test_repeat_and_streaming_never_read_a_future_channel(mode,analog,channels,Fraction(1001,3))
