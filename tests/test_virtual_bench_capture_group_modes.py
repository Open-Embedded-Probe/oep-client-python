"""Capture §4.1/4.2: the initial trigger gates every repeat/streaming group track."""
from fractions import Fraction
import struct
import pytest
from oep_client import capture as c, core, message as m
from test_virtual_bench_capture import bench


def setup(mode, analog_source, pre=0, unreachable=False):
    ep, hst, logic, clock = bench()
    analog = c.AnalogCapture(hst)
    source, other = (analog, logic) if analog_source else (logic, analog)
    core.plan_apply(hst, [(logic.fn, 0, 20), (logic.fn, 1, 21), (analog.fn, 1, 16), (analog.fn, 3, 17)])
    trigger = (c.CROSS_UP, 3, 2500) if analog_source else (c.EDGE, 1, 0)
    for track in [source, other]:
        track.configure(rate=1000 if track is source else 2000, mode=mode,
                        samples=32 if mode == c.REPEAT else None,
                        segments=3 if mode == c.REPEAT else None,
                        trigger=trigger if track is source else None,
                        pretrigger=pre if track is source else None)
        track.subscribe()
    if unreachable:
        ep.captures[source.fn].value = lambda role, index: 0
    group = c.CaptureGroup(hst)
    group.bind([other, source], trigger=source)
    hst.subscribe(group.fn)
    group.start([other, source])
    return ep, group, source, other, clock


def events(frames, fn):
    return [f for f in frames if f[0] == m.ROLE_EVENT and struct.unpack_from('<H', f, 1)[0] == fn]


@pytest.mark.parametrize('mode', [c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('analog_source', [False, True])
def test_unreachable_group_trigger_keeps_every_track_waiting(mode, analog_source):
    ep, group, source, other, clock = setup(mode, analog_source, unreachable=True)
    for elapsed in [0, 20, 1000]:
        clock.t = elapsed
        status = group.status()
        assert status.state == c.STATE['waiting'] and status.trigger_ns is None and status.trigger_fn is None
        for track in [source, other]:
            status = track.status()
            assert status.state == c.STATE['waiting'] and status.write_pos == status.serial_done == 0
            assert track.segments() == []
        assert ep.pushes() == []
    group.stop()
    assert group.status().state == c.STATE['configured']
    frames = ep.pushes()
    for track in [source, other]:
        assert track.segments() == []
        assert [c.unpack_event(f).kind for f in events(frames, track.fn)] == ['stopped']


@pytest.mark.parametrize('mode', [c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('pre', [1, 10])
def test_group_force_opens_all_tracks_once_with_common_time(mode, analog_source, pre):
    ep, group, source, other, clock = setup(mode, analog_source, pre, unreachable=True)
    clock.t = 20
    assert group.status().state == c.STATE['waiting']
    assert ep.pushes() == []
    generation = group.generation
    group.force()
    status = group.status()
    assert status.state == c.STATE['capturing'] and status.trigger_ns == 20_000_000
    assert status.trigger_fn is None and status.generation == generation
    frames = ep.pushes()
    notification, = events(frames, group.fn)
    assert notification[5] == 3 and struct.unpack_from('<HQI', notification, 6) == (0, status.trigger_ns, generation)
    for track in [source, other]:
        triggered, = [c.unpack_event(f) for f in events(frames, track.fn)]
        assert triggered.kind == 'triggered' and triggered.trigger_ns == status.trigger_ns
        assert triggered.trigger_index == group.pretrigger_of(track, source)
    group.force()
    assert not any(f[0] == m.ROLE_EVENT for f in ep.pushes())
    clock.t = 60
    if mode == c.REPEAT:
        for track in [source, other]:
            segment = track.segments()[0]
            assert segment.samples == 32 and segment.trigger_index == group.pretrigger_of(track, source)
            assert not segment.flags & 2
    else:
        assert all(track.status().write_pos > 0 for track in [source, other])
        assert not any(f[5] == c.EVENT_SEGMENT for f in ep.pushes() if f[0] == m.ROLE_EVENT)


@pytest.mark.parametrize('mode', [c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('pre', [1, 10])
def test_natural_group_trigger_maps_history_at_each_actual_rate(mode, analog_source, pre):
    ep, group, source, other, clock = setup(mode, analog_source, pre)
    first = 5 if analog_source else 2
    expected_time = ep.captures[source.fn].start_offset_ns + int(Fraction(first * 1_000_000_000) / source.config.rate)
    clock.t = first + 1
    status = group.status()
    assert status.state == c.STATE['capturing'] and status.trigger_ns == expected_time and status.trigger_fn == source.fn
    frames = ep.pushes()
    notification, = events(frames, group.fn)
    assert notification[5] == 3 and struct.unpack_from('<HQI', notification, 6) == (source.fn, expected_time, group.generation)
    for track in [source, other]:
        origin = ep.captures[track.fn].t0_ns
        absolute = max(0, round(Fraction(expected_time - origin) * track.config.rate / 1_000_000_000))
        requested = group.pretrigger_of(track, source)
        available = min(requested, absolute)
        triggered, = [c.unpack_event(f) for f in events(frames, track.fn)]
        assert triggered.kind == 'triggered' and triggered.trigger_ns == expected_time
        assert triggered.trigger_index == available and triggered.generation == track.generation
        clock.t = 50
        if mode == c.REPEAT:
            segment = track.segments()[0]
            assert segment.trigger_index == available and segment.samples == 32 - requested + available
            assert bool(segment.flags & 2) == (available < requested)
            assert segment.start_ns == origin + int(Fraction((absolute - available) * 1_000_000_000) / track.config.rate)
            assert all(s.trigger_index is None for s in track.segments()[1:])
        else:
            assert track.status().write_pos > 0
    assert not events(ep.pushes(), group.fn)
