"""capture-group §4.4, the trigger source, through the virtual bench and the client: the expression's stages, terms and
conditions over a logic and an analog track, the event cutting every track's segment, force, status and the refusals."""
import pytest

from oep_client import capture as c, core, host as h, message as m
from oep_client import virtual_bench_capture as vbc
from test_virtual_bench_capture import bench

LINKED = (c.LINKED, 0, 0)
Cond, Stage, TriggerSource = c.Cond, c.Stage, c.TriggerSource


def setup(pre=4, logic_waves=None):
    ep, hst, logic, clock = bench()
    analog, group = c.AnalogCapture(hst), c.CaptureGroup(hst)
    core.plan_apply(hst, [(logic.fn, r, 30 + r) for r in range(6)] + [(analog.fn, 0, 16)])
    for role, wave in (logic_waves or {}).items():
        ep.captures[logic.fn].waves[role] = vbc.logic_wave(wave)
    logic.configure(rate=10_000, samples=32, trigger=LINKED, pretrigger=pre)
    analog.configure(rate=10_000, samples=32, trigger=LINKED, pretrigger=pre)
    for t in (logic, analog):
        t.subscribe()
    hst.subscribe(group.fn)
    return ep, hst, logic, analog, group, clock


def group_events(ep, fn):
    return [c.unpack_event(f, group=True) for f in ep.pushes() if f[0] == m.ROLE_EVENT and int.from_bytes(f[1:3], "little") == fn]


def test_one_edge_starts_every_track_at_its_nearest_sample():
    ep, hst, logic, analog, group, clock = setup()
    group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 5)]])]))
    group.start([logic, analog])
    assert group.status().state == c.STATE["waiting"] and group.status().trigger is None
    clock.t += 20                                            # channel 5 first rises at sample 32 (3.2 ms)
    st = group.status()
    fn, ns, unc = st.trigger
    assert fn == logic.fn and unc == 100_000                 # judged at the logic's samples, 10 kHz
    assert ns == ep.captures[logic.fn].time_of(32)
    clock.t += 20
    (ls,), (an,) = logic.segments(), analog.segments()
    assert ls.trigger_index == 4 and ls.start_ns + 4 * 100_000 == ns   # arm 1: the pretrigger is there
    assert an.trigger_index == 4 and abs(an.start_ns + 4 * 100_000 - ns) <= 100_000 + an.start_uncertainty_ns
    assert group.status().state == c.STATE["done"]


def test_two_stages_cross_track_and_the_group_event():
    ep, hst, logic, analog, group, clock = setup()
    # stage 0: the analog's square (channel 0, period 64) falls below 100 while logic 4 is high;
    # stage 1: logic 5 rises within 5 ms
    source = TriggerSource([Stage([[Cond.below(analog, 0, 100), Cond.level(logic, 4, 1, when=1)]]),
                            Stage([[Cond.edge(logic, 5)]], within_ns=5_000_000)])
    group.bind([logic, analog], source)
    group.start([logic, analog])
    clock.t += 30
    events = group_events(ep, group.fn)
    trig = [e for e in events if e.kind == "triggered"]
    assert len(trig) == 1 and trig[0].trigger_fn == logic.fn and trig[0].record is None
    assert trig[0].generation == group.generation
    assert trig[0].uncertainty_ns == 100_000                 # both 10 kHz: the slower's period


def test_should_come_stage_holds_when_its_edge_does_not_come():
    ep, hst, logic, analog, group, clock = setup(logic_waves={3: "quiet"})
    source = TriggerSource([Stage([[Cond.edge(logic, 5)]]),
                            Stage([[Cond.edge(logic, 3)]], within_ns=1_000_000, on_timeout=1)])   # role 3 never moves
    group.bind([logic, analog], source)
    group.start([logic, analog])
    clock.t += 20
    fn, ns, _ = group.status().trigger
    assert ns == ep.captures[logic.fn].time_of(32) + 1_000_000   # the ACK that did not come within 1 ms


def test_force_makes_the_event_now():
    ep, hst, logic, analog, group, clock = setup(logic_waves={5: "quiet"})
    group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 5)]])]))
    group.start([logic, analog])
    clock.t += 10
    group.force()
    fn, ns, unc = group.status().trigger
    assert (fn, unc) == (0, 0) and ns == clock.t * 1_000_000
    clock.t += 10
    assert logic.segments()[0].trigger_index == 4
    group.force()                                            # not waiting: nothing


def test_refusals():
    ep, hst, logic, analog, group, clock = setup()
    with pytest.raises(h.Unsupported):                       # two instants in one term (§4.4)
        group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 5), Cond.cross_up(analog, 0, 100)]])]))
    with pytest.raises(h.Rejected) as e:                     # on_timeout 1 without within_ns
        group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 5)]], on_timeout=1)]))
    assert e.value.result.detail == m.MALFORMED
    with pytest.raises(h.Unavailable):                       # a role not in the track's plan
        group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(logic, 9)]])]))
    with pytest.raises(h.Unsupported):                       # a logic condition on the analog
        group.bind([logic, analog], TriggerSource([Stage([[Cond.edge(analog, 0)]])]))
    with pytest.raises(h.Unavailable):                       # a linked track alone (§3.3)
        logic.start()
    logic.configure(rate=10_000, samples=32)                 # immediate: not with a trigger source
    with pytest.raises(h.Unavailable):
        group.bind([logic, analog], TriggerSource([Stage([[Cond.cross_up(analog, 0, 100)]])]))


@pytest.mark.parametrize("when", [0, 1])
def test_a_stage_entered_with_its_level_already_true_follows_when(when):
    """§4.4 段の入り口: role 4 is high all along. Stage 1's level(4, high) counts at entry only with when 1; with when 0
    it waits for false to true inside the stage - never here."""
    ep, hst, logic, analog, group, clock = setup(logic_waves={4: "quiet:1"})
    source = TriggerSource([Stage([[Cond.edge(logic, 5)]]), Stage([[Cond.level(logic, 4, 1, when=when)]])])
    group.bind([logic, analog], source)
    group.start([logic, analog])
    clock.t += 20
    trigger = group.status().trigger
    if when:
        assert trigger[1] == ep.captures[logic.fn].time_of(33)   # the sample after stage 0's (one stage a sample)
    else:
        assert trigger is None
