"""Capture §3.2/3.3/4: an armed capture detects later input without rereading history."""
from fractions import Fraction
import struct
import pytest
from oep_client import capture as c, core, message as m
from test_virtual_bench_capture import bench


def setup(mode, analog_source, pre, grouped=False):
    ep, hst, logic, clock = bench()
    analog = c.AnalogCapture(hst)
    source, other = (analog, logic) if analog_source else (logic, analog)
    tracks = [source, other] if grouped else [source]
    core.plan_apply(hst, [(logic.fn, 0, 20), (logic.fn, 1, 21), (analog.fn, 1, 16), (analog.fn, 3, 17)])
    role = 3 if analog_source else 0
    trigger = (c.CROSS_UP, role, 2500) if analog_source else (c.EDGE, role, 0)
    for track in tracks:
        track.configure(rate=1000 if track is source else 2000, mode=mode,
                        samples=32 if mode != c.STREAMING else None,
                        segments=3 if mode == c.REPEAT else None,
                        trigger=trigger if track is source else None,
                        pretrigger=pre if track is source else None)
        track.subscribe()
    ep.captures[source.fn].value = lambda role, index: 0
    group = None
    if grouped:
        group = c.CaptureGroup(hst)
        group.bind([other, source], trigger=source)
        hst.subscribe(group.fn)
        group.start([other, source])
    else:
        source.start()
    return ep, group, source, other, clock, role


def notices(frames, fn):
    return [f for f in frames if f[0] == m.ROLE_EVENT and struct.unpack_from('<H', f, 1)[0] == fn]


def change(ep, source, role):
    # Timestamped stimulus: history remains low; the new signal starts at source sample 20.
    ep.captures[source.fn].value = lambda r, i: (3000 if isinstance(source, c.AnalogCapture) else 1) if r == role and i >= 20 else 0


@pytest.mark.parametrize('mode', [c.ONE_SHOT, c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('pre', [0, 10])
def test_standalone_detects_later_crossing_and_retains_actual_input(mode, analog_source, pre):
    ep, _, source, _, clock, role = setup(mode, analog_source, pre)
    clock.t = 10
    assert source.status().state == c.STATE['waiting'] and ep.pushes() == []
    generation = source.generation
    change(ep, source, role)
    clock.t = 19
    assert source.status().state == c.STATE['waiting'] and ep.pushes() == []
    clock.t = 25
    status = source.status()
    assert status.state == c.STATE['capturing']
    assert status.generation == generation
    frames = ep.pushes()
    triggered = [c.unpack_event(f) for f in notices(frames, source.fn) if f[5] == c.EVENT_TRIGGERED]
    event, = triggered
    origin = ep.captures[source.fn].t0_ns
    assert event.trigger_ns == origin + int(Fraction(20 * 1_000_000_000) / source.config.rate)
    assert event.trigger_index == pre and event.generation == generation
    clock.t = 60
    if mode != c.STREAMING:
        segment = source.segments()[0]
        assert segment.samples == 32 and segment.trigger_index == pre and not segment.flags & 2
        raw = source.read_segment(segment)
        values = source.values(raw, role) if analog_source else source.channel(raw, role, segment.samples)
        assert values == [0] * pre + [3000 if analog_source else 1] * (32 - pre)
    else:
        frames += ep.pushes()
        raw = b''.join(c.unpack_push(f)[3] for f in frames if f[0] == m.ROLE_DATA and struct.unpack_from('<H', f, 1)[0] == source.fn)
        values = source.values(raw, role) if analog_source else source.channel(raw, role)
        assert values[:pre] == [0] * pre and values[pre:] and all(v == (3000 if analog_source else 1) for v in values[pre:])
        assert not any(f[5] == c.EVENT_SEGMENT for f in notices(frames, source.fn))
    assert not any(f[5] == c.EVENT_TRIGGERED for f in notices(ep.pushes(), source.fn))


@pytest.mark.parametrize('mode', [c.ONE_SHOT, c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('pre', [0, 10])
@pytest.mark.parametrize('poll', ['group', 'track'])
def test_group_later_trigger_starts_every_track_without_new_generation(mode, analog_source, pre, poll):
    ep, group, source, other, clock, role = setup(mode, analog_source, pre, grouped=True)
    clock.t = 10
    assert group.status().state == c.STATE['waiting'] and ep.pushes() == []
    generations = [group.generation, source.generation, other.generation]
    change(ep, source, role)
    clock.t = 25
    group.status() if poll == 'group' else other.status()
    status = group.status()
    expected = ep.captures[source.fn].t0_ns + int(Fraction(20 * 1_000_000_000) / source.config.rate)
    assert status.trigger_ns == expected and status.trigger_fn == source.fn
    assert status.state == c.STATE['capturing']
    assert [group.generation, source.generation, other.generation] == generations
    frames = ep.pushes()
    triggered = [f for f in notices(frames, group.fn) if f[5] == 3]
    notification, = triggered
    assert struct.unpack_from('<HQI', notification, 6) == (source.fn, expected, group.generation)
    for track in [source, other]:
        notification, = [c.unpack_event(f) for f in notices(frames, track.fn) if f[5] == c.EVENT_TRIGGERED]
        assert notification.trigger_ns == expected and notification.generation == track.generation
        assert notification.trigger_index == group.pretrigger_of(track, source)
    clock.t = 60
    if mode != c.STREAMING:
        for track in [source, other]:
            segment = track.segments()[0]
            assert segment.trigger_index == group.pretrigger_of(track, source) and segment.samples == 32
    else:
        assert all(track.status().write_pos > 0 for track in [source, other])
    later = ep.pushes()
    assert not any(f[5] == 3 for f in notices(later, group.fn))
    if mode == c.ONE_SHOT:
        assert group.status().state == c.STATE['done']
        assert [f[5] for f in notices(frames + later, group.fn)] == [3, 2]
        assert [f[5] for f in notices(later, group.fn)] == [2]
        assert notices(ep.pushes(), group.fn) == []


@pytest.mark.parametrize('analog_source', [False, True])
def test_waiting_scan_reads_only_new_elapsed_samples_and_resets_on_restart(analog_source):
    ep, _, source, _, clock, _ = setup(c.REPEAT, analog_source, 0)
    calls = []
    first_unobserved = ep.captures[source.fn].trigger_scan
    ep.captures[source.fn].value = lambda r, i: calls.append(i) or 0
    for elapsed in [5, 5, 10]:
        clock.t = elapsed
        before = len(calls)
        assert source.status().state == c.STATE['waiting']
        last = int((Fraction(elapsed * 1_000_000) - ep.captures[source.fn].t0_ns) * source.config.rate // 1_000_000_000)
        assert calls == list(range(first_unobserved, last + 1))
        if elapsed == 5 and before: assert len(calls) == before
    source.stop()
    clock.t = 100
    calls.clear()
    source.start()
    clock.t = 105
    assert source.status().state == c.STATE['waiting']
    assert calls and calls[0] == 0 and calls[-1] <= 5


@pytest.mark.parametrize('mode', [c.ONE_SHOT, c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('operation', ['force', 'stop'])
def test_group_operation_observes_natural_trigger_before_host_action(mode, analog_source, operation):
    ep, group, source, _, clock, role = setup(mode, analog_source, 0, grouped=True)
    clock.t = 10
    assert group.status().state == c.STATE['waiting'] and ep.pushes() == []
    change(ep, source, role)
    clock.t = 25
    getattr(group, operation)()
    frames = ep.pushes()
    notification, = [f for f in notices(frames, group.fn) if f[5] == 3]
    expected = ep.captures[source.fn].t0_ns + int(Fraction(20 * 1_000_000_000) / source.config.rate)
    assert struct.unpack_from('<HQI', notification, 6) == (source.fn, expected, group.generation)
