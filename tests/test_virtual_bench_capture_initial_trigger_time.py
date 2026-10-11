"""Capture §3.2/3.3/4: start never searches an unelapsed trigger waveform."""
import pytest
from oep_client import capture as c, core, message as m
from test_virtual_bench_capture import bench


@pytest.mark.parametrize('mode', [c.ONE_SHOT, c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('analog_source', [False, True])
@pytest.mark.parametrize('grouped', [False, True])
def test_start_observes_only_elapsed_samples_before_initial_trigger(mode, analog_source, grouped):
    ep, hst, logic, clock = bench()
    analog = c.AnalogCapture(hst)
    source, other = (analog, logic) if analog_source else (logic, analog)
    tracks = [source, other] if grouped else [source]
    core.plan_apply(hst, [(logic.fn, 0, 20), (analog.fn, 0, 16)])
    for track in tracks:
        track.configure(rate=1000, samples=8 if mode != c.STREAMING else None,
                        segments=2 if mode == c.REPEAT else None, mode=mode,
                        trigger=(c.CROSS_UP, 0, 1500) if analog_source and track is source
                        else (c.EDGE, 0, 0) if track is source else None)
        track.subscribe()
    calls = []
    def value(role, index):
        calls.append((clock.t, index))
        return (3000 if analog_source else 1) if index >= 3 else 0
    ep.captures[source.fn].value = value
    group = c.CaptureGroup(hst) if grouped else None
    if grouped:
        group.bind([other, source], trigger=source);hst.subscribe(group.fn)
        group.start([other, source])
        status = lambda: group.status()
    else:
        source.start();status = source.status
    assert status().state == c.STATE['waiting']
    assert all(index <= 0 for _, index in calls)
    assert ep.pushes() == []
    generation = source.generation
    clock.t = 2
    assert status().state == c.STATE['waiting']
    assert all(index <= 2 for _, index in calls)
    assert ep.pushes() == []
    clock.t = 4
    assert status().state != c.STATE['waiting']
    assert source.generation == generation
    events = [f for f in ep.pushes() if f[0] == m.ROLE_EVENT and
              int.from_bytes(f[1:3], 'little') == source.fn and f[5] == c.EVENT_TRIGGERED]
    trigger, = [c.unpack_event(f) for f in events]
    assert trigger.trigger_ns == ep.captures[source.fn].time_of(3)
