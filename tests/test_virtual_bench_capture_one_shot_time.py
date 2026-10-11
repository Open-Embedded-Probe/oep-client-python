"""Capture §2.2/3.2/3.4/4: one-shot only publishes elapsed, completed samples."""
import struct
from fractions import Fraction
import pytest
from oep_client import capture as c, core, message as m
from test_virtual_bench_capture import bench


def setup(analog_source, grouped, trigger, pre=0):
    ep, hst, logic, clock = bench()
    analog = c.AnalogCapture(hst)
    source, other = (analog, logic) if analog_source else (logic, analog)
    tracks = [source, other] if grouped else [source]
    core.plan_apply(hst, [(logic.fn, 0, 20), (analog.fn, 0, 16)])
    for track in tracks:
        track.configure(rate=1000, samples=8, pretrigger=pre if trigger != 'none' and track is source else None,
                        trigger=((c.CROSS_UP, 0, 1500) if analog_source else (c.EDGE, 0, 0))
                        if trigger != 'none' and track is source else None)
        track.subscribe()
    for track in tracks:
        cap = ep.captures[track.fn]
        def value(role, index, cap=cap, track=track):
            assert cap.time_of(index) <= clock.t * 1_000_000, 'waveform read before its sample time'
            return (3000 if isinstance(track, c.AnalogCapture) else 1) if index >= 3 and trigger == 'natural' else 0
        cap.value = value
    group = c.CaptureGroup(hst) if grouped else None
    if group:
        group.bind(tracks, trigger=source if trigger != 'none' else None)
        hst.subscribe(group.fn);group.start(tracks)
    else:source.start()
    return ep, tracks, group, clock


def read_available(track):
    payload = track._call(track.READ, struct.pack('<IQI', track.generation, 0, 32), locked=False).payload
    position, flags, count = struct.unpack_from('<QBI', payload)
    assert position == 0 and count == len(payload) - 13 and not flags & 2
    return payload[13:]


@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('grouped', [False, True])
@pytest.mark.parametrize('trigger,pre', [('none', 0), ('natural', 0), ('natural', 4), ('force', 0), ('force', 4)])
def test_one_shot_does_not_complete_or_expose_future_data(analog_source, grouped, trigger, pre):
    ep, tracks, group, clock = setup(analog_source, grouped, trigger, pre)
    generation = {t.fn: t.generation for t in tracks}
    for t in tracks:
        assert t.segments() == [] and read_available(t) == b''
    clock.t = 4
    if trigger == 'force':(group or tracks[0]).force()
    status = (group or tracks[0]).status()
    assert status.state == c.STATE['capturing']
    for t in tracks:
        assert t.segments() == [] and read_available(t) == b''
    initial = ep.pushes()
    assert not any(f[0] == m.ROLE_DATA or f[0] == m.ROLE_EVENT and f[5] in (c.EVENT_SEGMENT, c.EVENT_STOPPED) for f in initial)
    clock.t = 20
    assert (group or tracks[0]).status().state == c.STATE['done']
    final = ep.pushes()
    for t in tracks:
        segment, = t.segments()
        assert t.generation == generation[t.fn] == segment.generation
        assert segment.samples > 0 and segment.start_ns + int((segment.samples - 1) * 1_000_000_000 / t.config.rate) <= 20_000_000
        assert t.read_segment(segment)
        kinds = [f[5] for f in final if f[0] == m.ROLE_EVENT and int.from_bytes(f[1:3], 'little') == t.fn]
        assert kinds == [c.EVENT_SEGMENT, c.EVENT_STOPPED]
    assert ep.pushes() == []


@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('grouped', [False, True])
def test_stop_commits_only_elapsed_one_shot_samples(analog_source, grouped):
    ep, tracks, group, clock = setup(analog_source, grouped, 'none')
    clock.t = 3;(group or tracks[0]).stop()
    for t in tracks:
        assert t.status().state == c.STATE['configured']
        segment, = t.segments()
        assert 0 < segment.samples < 8 and segment.flags & 2
        assert segment.start_ns + int((segment.samples - 1) * 1_000_000_000 / t.config.rate) <= 3_000_000
        assert t.read_segment(segment)
    frames = ep.pushes()
    for t in tracks:
        notices = [c.unpack_event(f) for f in frames if f[0] == m.ROLE_EVENT and int.from_bytes(f[1:3], 'little') == t.fn]
        assert [e.kind for e in notices] == ['segment', 'stopped'] and notices[-1].reason == 1


@pytest.mark.parametrize('analog', [False, True])
@pytest.mark.parametrize('channels', [1, 2])
@pytest.mark.parametrize('rate', [Fraction(1000, 3), Fraction(1001, 3), Fraction(20000, 7)])
def test_last_sample_boundary_uses_realized_fractional_rate_and_analog_skew(analog, channels, rate):
    from oep_client.virtual_bench_capture import VirtualCapture
    cap = VirtualCapture({c.ONE_SHOT}, {8}, 1, 1_000_000,
                         frontends={0: (0, 3300, 0)} if analog else {})
    cap.mode, cap.state, cap.rate = c.ONE_SHOT, c.STATE['configured'], rate
    cap.samples, cap.channels, cap.segments_max = 4, channels, 1
    cap.width = channels * 2 if analog else 8
    cap.analog_roles = tuple(range(channels)) if analog else ()
    channel_step = int(Fraction(1_000_000_000) / (rate * channels)) if analog else 0
    clock_ns = Fraction(0)
    def value(role, index):
        when = cap.t0_ns + Fraction(index * 1_000_000_000) / rate + role * channel_step
        assert when <= clock_ns
        return index & 1
    cap.value = value
    cap.start(0)
    last = cap.t0_ns + Fraction(3_000_000_000) / rate + (channels - 1) * channel_step
    clock_ns = last - 1
    assert cap.tick(clock_ns / 1_000_000) == []
    assert cap.state == c.STATE['capturing'] and not cap.data and not cap.segs
    clock_ns = last
    events = cap.tick(clock_ns / 1_000_000)
    assert cap.state == c.STATE['done'] and [row[0] for row in events] == [c.EVENT_SEGMENT, c.EVENT_STOPPED]
    assert cap.segs[0].samples == 4 and cap.produced == 4
    assert cap.tick(clock_ns / 1_000_000) == []


@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('grouped', [False, True])
@pytest.mark.parametrize('error', [1, 2, 3])
def test_one_shot_error_never_commits_incomplete_samples(analog_source, grouped, error):
    ep, tracks, group, clock = setup(analog_source, grouped, 'none')
    clock.t = 3
    ep.capture_overflow(tracks[0].fn, error)
    status = tracks[0].status()
    assert status.state == c.STATE['error'] and status.error == error
    assert tracks[0].segments() == [] and read_available(tracks[0]) == b''
    frames = ep.pushes()
    notices = [c.unpack_event(f) for f in frames if f[0] == m.ROLE_EVENT and int.from_bytes(f[1:3], 'little') == tracks[0].fn]
    notice, = notices
    assert notice.kind == 'stopped' and notice.reason == 3 and notice.error == error
    assert notice.generation == tracks[0].generation
    clock.t = 20
    assert tracks[0].segments() == [] and read_available(tracks[0]) == b''


@pytest.mark.parametrize('analog', [False, True])
def test_one_sample_can_complete_as_soon_as_its_actual_sample_time(analog):
    ep, hst, logic, clock = bench()
    track = c.AnalogCapture(hst) if analog else logic
    core.plan_apply(hst, [(track.fn, 0, 16 if analog else 20)])
    track.configure(rate=1000, samples=1);track.subscribe();track.start()
    assert track.status().state == c.STATE['capturing' if analog else 'done']
    if analog:
        assert track.segments() == [] and read_available(track) == b''
        clock.t = 1
    segment, = track.segments()
    assert segment.samples == 1 and track.status().state == c.STATE['done']
