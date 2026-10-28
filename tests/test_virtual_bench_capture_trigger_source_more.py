"""capture-group §4.4 beyond the profiles' conditions (virtual_bench.with_capture_options): pulse, analog_pulse,
pattern, serial (UART), the external input trig_in, trigger_out, and rearm 1 with holdoff."""
import struct

import pytest

from oep_client import capture as c, core, host as h, message as m, virtual_bench as vb
from oep_client import virtual_bench_capture as vbc
from test_virtual_bench_capture import bench

LINKED = (c.LINKED, 0, 0)
Cond, Stage, TriggerSource = c.Cond, c.Stage, c.TriggerSource
TRIG_IN, TRIG_OUT = c.PIN_ROLE["trig_in"], c.PIN_ROLE["trig_out"]
IN_PIN, OUT_PIN = 44, 45


def setup(pre=4, mode=c.ONE_SHOT, logic_waves=None, profile=None, group_roles=(TRIG_IN, TRIG_OUT), **more):
    ep, hst, logic, clock = bench(profile or (lambda: vb.with_capture_options(vb.p4_x035())))
    analog, group = c.AnalogCapture(hst), c.CaptureGroup(hst)
    plan = [(logic.fn, r, 30 + r) for r in range(6)] + [(analog.fn, 0, 16)]
    plan += [(group.fn, r, {TRIG_IN: IN_PIN, TRIG_OUT: OUT_PIN}[r]) for r in group_roles]
    core.plan_apply(hst, plan)
    for role, wave in (logic_waves or {}).items():
        ep.captures[logic.fn].waves[role] = vbc.logic_wave(wave)
    samples = None if mode == c.STREAMING else 8 if mode == c.REPEAT else 32
    for t in (logic, analog):
        t.configure(rate=10_000, mode=mode, samples=samples, trigger=LINKED, pretrigger=pre, **more)
        t.subscribe()
    hst.subscribe(group.fn)
    return ep, hst, logic, analog, group, clock


def judged(clock, ep, group, logic, analog, source, ms=100):
    group.bind([logic, analog], source)
    group.start([logic, analog])
    clock.t += ms
    return group.status().trigger


def group_events(ep, fn):
    return [c.unpack_event(f, group=True) for f in ep.pushes()
            if f[0] == m.ROLE_EVENT and int.from_bytes(f[1:3], "little") == fn]


def period(ep, fn):
    return 1_000_000_000 / ep.captures[fn].rate


@pytest.mark.parametrize("role, cond, lo, hi, sample", [
    (5, 0, 2_000_000, 0, 52),            # role 5 high from sample 32: 2 ms (20 samples) later
    (0, 1, 0, 150_000, 6),               # role 0's 1-sample runs: the first whole one after arming ends at 6
    (2, 2, 350_000, 450_000, 16),        # role 2's 4-sample runs (400 us): 4 - 7 began before the judging (from 5),
                                         # so 12 - 15, ending at 16
])
def test_a_pulse_by_its_width(role, cond, lo, hi, sample):
    ep, hst, logic, analog, group, clock = setup()
    fn, ns, unc = judged(clock, ep, group, logic, analog, TriggerSource([Stage([[Cond.pulse(logic, role, 1, cond, lo, hi)]])]))
    assert fn == logic.fn and ns == ep.captures[logic.fn].time_of(sample)


def test_an_analog_pulse_below_a_level_for_long_enough():
    ep, hst, logic, analog, group, clock = setup()
    fn, ns, unc = judged(clock, ep, group, logic, analog,
                         TriggerSource([Stage([[Cond.analog_pulse(analog, 0, 100, side=0, cond=0, min_ns=1_000_000)]])]))
    late = ns - ep.captures[analog.fn].time_of(32) - 1_000_000   # the square low from sample 32
    assert fn == analog.fn and 0 <= late <= period(ep, analog.fn) + 1


def test_a_pattern_of_the_tracks_roles():
    ep, hst, logic, analog, group, clock = setup()
    fn, ns, unc = judged(clock, ep, group, logic, analog, TriggerSource([Stage([[Cond.pattern(logic, {0: 1, 1: 1, 2: 0})]])]))
    assert ns == ep.captures[logic.fn].time_of(11)          # i & 7 == 3, after a false one since arming (4)
    with pytest.raises(h.Unavailable):                      # a role not in the track's plan
        group.bind([logic, analog], TriggerSource([Stage([[Cond.pattern(logic, {9: 1})]])]))


def test_a_uart_match_and_a_protocol_it_does_not_declare():
    ep, hst, logic, analog, group, clock = setup(logic_waves={3: "uart:1000:OEP\n"})
    fn, ns, unc = judged(clock, ep, group, logic, analog, TriggerSource([Stage([[Cond.uart(logic, 3, 1000, b"EP")]])]), ms=200)
    # 10 samples a bit, a character every 20 bits: "P" starts at sample 400, its stop bit's middle at 495; "O" began
    # before arming and is not read
    assert ns == ep.captures[logic.fn].time_of(495)
    spi = Cond(logic.fn, 3, c.COND["serial"], struct.pack("<BBBBIBBBB", c.SERIAL_PROTOCOL["spi"], 0, 4, 5, 1000, 8, 0, 0, 1)
               + bytes([0xFF, 0x00]))
    with pytest.raises(h.Unsupported):
        group.bind([logic, analog], TriggerSource([Stage([[spi]])]))


def test_the_external_input_is_judged_at_its_own_rate():
    ep, hst, logic, analog, group, clock = setup()
    ep.groups[group.fn].inputs[TRIG_IN] = lambda ns: int(ns >= 2_500_500)   # rises 2.5005 ms after the start
    fn, ns, unc = judged(clock, ep, group, logic, analog, TriggerSource([Stage([[Cond.external("edge")]])]))
    assert (fn, unc) == (0, 1000) and ns == 2_501_000         # the first microsecond tick past the rise


def test_trig_in_and_trig_out_need_the_groups_plan():
    ep, hst, logic, analog, group, clock = setup(group_roles=())
    for source in (TriggerSource([Stage([[Cond.external("level")]])]),
                   TriggerSource([Stage([[Cond.edge(logic, 5)]])], trigger_out=(5000, 1))):
        with pytest.raises(h.Unavailable):
            group.bind([logic, analog], source)


def test_trigger_out_gives_a_pulse_after_the_event():
    ep, hst, logic, analog, group, clock = setup()
    fn, ns, unc = judged(clock, ep, group, logic, analog, TriggerSource([Stage([[Cond.edge(logic, 5)]])], trigger_out=(5000, 1)))
    assert ep.groups[group.fn].pulses == [(TRIG_OUT, ns + 1000, 5000, 1)]   # trigger_out_latency_ns 1000
    ep, hst, logic, analog, group, clock = setup(profile=vb.p4_x035, group_roles=())
    with pytest.raises(h.Unsupported):                      # the profile's group declares no trigger_out
        group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 5)]])], trigger_out=(5000, 1)))


def test_rearm_1_cuts_a_segment_at_every_event_after_the_holdoff():
    ep, hst, logic, analog, group, clock = setup(mode=c.REPEAT, segments=3)
    group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 5)]])], rearm=1, holdoff_ns=10_000_000))
    group.start([logic, analog])
    clock.t += 100
    events = [e for e in group_events(ep, group.fn) if e.kind == "triggered"]
    assert [e.record for e in events] == [0, 1, 2]
    lc = ep.captures[logic.fn]
    # role 5 rises at 32, 96, 160, ...: 10 ms (100 samples) after 32 the next is 160, then 288
    assert [e.trigger_ns for e in events] == [lc.time_of(i) for i in (32, 160, 288)]
    segs = logic.segments()
    assert len(segs) == 3 and all(s.trigger_index == 4 for s in segs)
    assert len(analog.segments()) == 3


def test_rearm_1_in_streaming_makes_a_record_a_track_without_cutting():
    ep, hst, logic, analog, group, clock = setup(mode=c.STREAMING)
    group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 5)]])], rearm=1))
    group.start([logic, analog])
    clock.t += 30
    ep.pushes()
    assert [r.sample for r in logic.triggers()] == [32, 96, 160, 224, 288]   # 30 ms: 300 samples
    assert len(analog.triggers()) == 5


def test_an_abort_term_sends_the_stage_back_to_stage_0():
    # stage 0: role 4 rises (16, 48, ...); stage 1: role 5 rises (32, 96, ...); role 3 rises every 16 samples at 8 mod 16,
    # always between the two: with it as stage 1's abort the event never comes, without it at 32
    ep, hst, logic, analog, group, clock = setup()
    fn, ns, _ = judged(clock, ep, group, logic, analog,
                       TriggerSource([Stage([[Cond.edge(logic, 4)]]), Stage([[Cond.edge(logic, 5)]])]))
    assert ns == ep.captures[logic.fn].time_of(32)
    ep, hst, logic, analog, group, clock = setup()
    group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 4)]]),
                                               Stage([[Cond.edge(logic, 5)]], aborts=[[Cond.edge(logic, 3)]])]))
    group.start([logic, analog])
    clock.t += 100
    assert group.status().state == c.STATE["waiting"] and group.status().trigger is None
