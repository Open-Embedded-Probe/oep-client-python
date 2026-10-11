"""Capture §3.2/3.8: retained segments survive resume; calibration describes past measurements."""
from fractions import Fraction
import copy
import pytest
from oep_client import capture as c, core
from test_virtual_bench_capture import bench
from test_virtual_bench_capture_channel_time import cap


@pytest.mark.parametrize('mode', [c.ONE_SHOT, c.REPEAT, c.STREAMING])
@pytest.mark.parametrize('grouped', [False, True])
@pytest.mark.parametrize('waiting', [False, True])
def test_calibration_measurement_is_not_in_the_future_and_tracks_last_start(mode, grouped, waiting):
    ep, hst, logic, clock = bench()
    adc, group = c.AnalogCapture(hst), c.CaptureGroup(hst)
    core.plan_apply(hst, [(logic.fn, 0, 20), (adc.fn, 0, 16), (adc.fn, 3, 19)])
    assert adc.calibration().vrefint is None
    for track in [logic, adc]:
        track.configure(rate=1000, samples=None if mode == c.STREAMING else 8, mode=mode)
        if mode == c.STREAMING: track.subscribe()
    if waiting:
        ep.captures[adc.fn].value = lambda role, index: 0
        adc.configure(rate=1000, samples=None if mode == c.STREAMING else 8, mode=mode,
                      trigger=(c.CROSS_UP, 3, 1500))
    if grouped: group.bind([logic, adc], trigger=adc if waiting else None)
    factory = adc.calibration().factory
    for start_ms in [7, 13]:
        clock.t = start_ms
        if grouped: group.start([logic, adc])
        else: adc.start()
        before = copy.deepcopy(ep.captures[adc.fn])
        cal = adc.calibration()
        assert cal.factory == factory
        raw, ns = cal.vrefint
        assert raw == 1365 and cal.vrefint_nominal_mv == 1100
        assert start_ms * 1000000 <= ns <= clock.t * 1000000, 'future calibration measurement'
        assert ep.captures[adc.fn] == before
        clock.t += 1
        assert adc.calibration().vrefint == (raw, ns)
        if grouped: group.stop()
        else: adc.stop()


@pytest.mark.parametrize('analog', [False, True])
@pytest.mark.parametrize('rate', [Fraction(1000, 3), Fraction(1001, 3), Fraction(83333, 8)])
@pytest.mark.parametrize('drop', [1, 2])
def test_repeat_resume_preserves_retained_rows_bytes_and_trigger_time(analog, rate, drop):
    track = cap(c.REPEAT, analog, rate=rate, trigger=True)
    clock_ns = Fraction(7000000)
    def value(role, index):
        when = track.t0_ns + Fraction(index * 1000000000) / rate + track.channel_skew(role)
        assert when <= clock_ns, 'future channel sample after resume'
        return index & 1
    # A force resolves the trigger without reading ahead in the known waveform.
    track.value = lambda role, index: 0
    track.start(7)
    track.force(7)
    track.value = value
    trigger_ns, generation = track.trigger_time_ns, track.generation
    for round_number in range(3):
        clock_ns += 100000000
        track.tick(int(clock_ns // 1000000))
        assert track.state == c.STATE['paused'] and len(track.segs) == 3
        before = copy.deepcopy(track)
        clock_ns += 100000000
        assert track.tick(int(clock_ns // 1000000)) == []
        assert track == before
        kept = [s.pack() for s in track.segs[drop:]]
        kept_bytes = bytes(track.data[track.segs[drop].position - track.base:])
        freed = track.segs[drop - 1].serial
        next_serial = track.serial_done
        track.release(generation, freed, int(clock_ns // 1000000))
        assert track.state == c.STATE['capturing']
        assert [s.pack() for s in track.segs] == kept and bytes(track.data) == kept_bytes
        assert track.trigger_time_ns == trigger_ns and track.generation == generation
        after_release = copy.deepcopy(track)
        track.release(generation, freed, int(clock_ns // 1000000))
        assert track == after_release
        clock_ns += 100000000
        track.tick(int(clock_ns // 1000000))
        assert [s.pack() for s in track.segs[:len(kept)]] == kept
        assert bytes(track.data[:len(kept_bytes)]) == kept_bytes
        fresh = track.segs[len(kept):]
        assert fresh[0].serial == next_serial and fresh[0].flags & 1
        assert all(not s.flags & 1 for s in fresh[1:])
        assert fresh[0].start_ns >= clock_ns - 100000000
        assert fresh[0].start_ns > track.segs[len(kept) - 1].start_ns
        assert track.trigger_time_ns == trigger_ns and track.generation == generation
