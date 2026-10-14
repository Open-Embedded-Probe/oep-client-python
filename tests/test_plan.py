"""oep.probe.plan (oep-spec a09fa45 interfaces/oep-if-plan.ja.md) rule by rule on the virtual bench (esp32-v003: gpio
fn 4, uart fn 5, logic fn 6, spi-target fn 8, plan fn 11, plan_roles 32), through the client's core.plan_apply /
plan_release and raw requests where the client never sends the form."""

import struct

import pytest

from oep_client import capture, catalog, core, endpoint, fixture, host as h, message as m, registry as reg, virtual_bench

PLAN = reg.PROBE_PLAN
APPLY, RELEASE = PLAN.op["plan_apply"], PLAN.op["plan_release"]
ASSIGN = PLAN.tlv["plan_apply"]["role_assignment"]
GPIO, UART, LOGIC, SPI, PLAN_FN = 4, 5, 6, 8, 11
CAUSE = reg.CORE.tlv["unavailable_payload"]["cause"]


class Clock:
    t = 0

    def __call__(self):
        return self.t


def bench(profile=virtual_bench.esp32_v003):
    ep = endpoint.Endpoint(profile(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    return ep, hst


def raw(hst, op, payload):
    """A plan request as sent; the result whatever it is."""
    try:
        return hst.call(PLAN_FN, op, payload)
    except h.Rejected as e:
        return e.result


def assignment(fn, role, channel, critical=True):
    return m.tlv(ASSIGN, struct.pack("<HBH", fn, role, channel), critical=critical)


def cause(result):
    return dict(m.split_tlvs(result.payload)).get(CAUSE)


# ---- the interface's place (preamble) ---------------------------------------------------------------------------------

def test_a_probe_with_plan_roles_lists_exactly_one_plan_and_one_without_lists_none():
    """A probe listing an interface with plan roles lists oep.probe.plan, at most one; one with none lists none."""
    ep, hst = bench()
    assert core.find_all(hst, "oep.probe.plan") == [PLAN_FN]
    ep = endpoint.Endpoint(virtual_bench.core_v1(), Clock())               # the core alone: no plan roles
    hst = h.Host(lambda b: ep.handle(b, 0))
    assert core.find_all(hst, "oep.probe.plan") == []


def test_plan_is_a_session_resource():
    """The plan goes with the session (core §9); nothing re-applies it but the host."""
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    hst.end()
    assert not ep.plan
    hst.open(3000)
    assert not ep.plan


# ---- plan §1 operations and describe -----------------------------------------------------------------------------------------

def test_ops_and_plan_roles_are_declared():
    """§1: plan_apply 0x10 and plan_release 0x11, both requiring the lock; describe 0x40 plan_roles(u32)."""
    ep, hst = bench()
    assert core.ops(hst, PLAN_FN) >= {APPLY, RELEASE}
    decl = dict(core.describe(hst, PLAN_FN))
    assert struct.unpack("<I", decl[PLAN.tlv["describe"]["plan_roles"]])[0] == ep.plan_roles == 32
    r = m.Result.unpack(ep.handle(m.Request(1, PLAN_FN, APPLY, assignment(GPIO, 1, 4), 0).pack(), 0))
    assert r.detail == m.SESSION_REQUIRED


# ---- plan §2.1 plan_apply ----------------------------------------------------------------------------------------------------

def test_apply_answers_success_with_no_payload_and_an_assignment_is_fn_role_channel():
    """§2.1: role_assignment TLVs; an assignment is (fn, role, channel) - one role may hold several channels (gpio)."""
    ep, hst = bench()
    r = raw(hst, APPLY, assignment(GPIO, 1, 4) + assignment(GPIO, 1, 5))
    assert r.succeeded and r.payload == b""
    assert {(GPIO, 1, 4), (GPIO, 1, 5)} <= ep.plan


def test_apply_replaces_only_the_fns_it_names():
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4), (LOGIC, 0, 13)])
    core.plan_apply(hst, [(GPIO, 1, 5)])
    assert ep.plan == {(GPIO, 1, 5), (LOGIC, 0, 13)}                        # gpio replaced, logic kept


def test_one_refusal_changes_nothing_not_even_the_replaced_fns_plan():
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4), (UART, 1, 5)])
    before = set(ep.plan)
    r = raw(hst, APPLY, assignment(GPIO, 1, 13) + assignment(UART, 9, 14))   # uart has no role 9
    assert r.detail == m.UNSUPPORTED and ep.plan == before


def test_an_assignment_that_stays_keeps_its_pin_and_the_same_set_does_nothing():
    """§2.1: a kept (fn, role, channel) is neither released nor taken again: the pin's level stays; the same set for
    an fn does nothing."""
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    fixture.Gpio(hst, GPIO).set([(4, fixture.Gpio.OUTPUT_HIGH)])
    state = ep.pin_state(4)
    core.plan_apply(hst, [(GPIO, 1, 4), (GPIO, 1, 5)])                      # 4 stays, 5 added
    assert ep.pin_state(4) == state and state.startswith("gpio")
    core.plan_apply(hst, [(GPIO, 1, 4), (GPIO, 1, 5)])                      # the same set
    assert ep.pin_state(4) == state


def test_a_running_function_refuses_a_change_with_cause_6():
    """§2.1, §2.4: the changed fn's function is running (a configured UART) - unavailable cause 6, nothing changes."""
    ep, hst = bench()
    core.plan_apply(hst, [(UART, 1, 5)])
    fixture.FixtureUart(hst, UART).configure(9600)
    before = set(ep.plan)
    r = raw(hst, APPLY, assignment(UART, 1, 13))
    assert r.detail == m.UNAVAILABLE and cause(r) == b"\x06" and ep.plan == before
    assert raw(hst, APPLY, assignment(UART, 1, 5)).succeeded                # the same set: nothing to refuse


def test_a_resource_bound_to_another_fn_refuses_with_cause_4():
    """§2.1, §2.4: a track bound in a capture-group refuses a plan change (cause 4)."""
    ep, hst = bench(virtual_bench.p4_x035)
    logic, analog = capture.LogicCapture(hst), capture.AnalogCapture(hst)
    core.plan_apply(hst, [(logic.fn, 0, 20), (analog.fn, 0, 16)])
    logic.configure(rate=1_000_000, samples=100)
    analog.configure(rate=1000, samples=10)
    capture.CaptureGroup(hst).bind([logic, analog])
    before = set(ep.plan)
    with pytest.raises(h.Unavailable) as e:
        core.plan_apply(hst, [(logic.fn, 0, 21)])
    assert e.value.cause == "bound_in_group" and ep.plan == before


def test_past_plan_roles_is_cause_2_and_changes_nothing():
    """§2.1: the total after the replacement over plan_roles - unavailable cause 2, not malformed."""
    ep, hst = bench()
    ep.plan_roles = 2
    core.plan_apply(hst, [(GPIO, 1, 4)])
    before = set(ep.plan)
    r = raw(hst, APPLY, assignment(LOGIC, 0, 13) + assignment(LOGIC, 1, 14))
    assert r.detail == m.UNAVAILABLE and cause(r) == b"\x02" and ep.plan == before
    assert raw(hst, APPLY, assignment(GPIO, 1, 5) + assignment(LOGIC, 0, 13)).succeeded   # replacing gpio: 2 in all


# ---- plan §2.2 plan_release --------------------------------------------------------------------------------------------------

def test_release_names_fns_n_0_is_every_fn_and_an_fn_without_a_plan_is_ignored():
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4), (LOGIC, 0, 13), (UART, 1, 5)])
    r = raw(hst, RELEASE, struct.pack("<BHH", 2, GPIO, SPI))                # spi has no plan: ignored
    assert r.succeeded and r.payload == b"" and ep.plan == {(LOGIC, 0, 13), (UART, 1, 5)}
    assert raw(hst, RELEASE, b"\x00").succeeded and not ep.plan


def test_release_of_an_unknown_fn_or_a_bound_one_changes_nothing():
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    r = raw(hst, RELEASE, struct.pack("<BHH", 2, GPIO, 99))
    assert r.detail == m.UNKNOWN_FUNCTION and ep.plan == {(GPIO, 1, 4)}
    ep, hst = bench(virtual_bench.p4_x035)
    logic, analog = capture.LogicCapture(hst), capture.AnalogCapture(hst)
    core.plan_apply(hst, [(logic.fn, 0, 20), (analog.fn, 0, 16)])
    logic.configure(rate=1_000_000, samples=100)
    analog.configure(rate=1000, samples=10)
    capture.CaptureGroup(hst).bind([logic, analog])
    before = set(ep.plan)
    with pytest.raises(h.Unavailable) as e:
        core.plan_release(hst, [logic.fn])
    assert e.value.cause == "bound_in_group" and ep.plan == before


def test_release_whose_count_does_not_match_is_malformed():
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    assert raw(hst, RELEASE, struct.pack("<BH", 2, GPIO)).detail == m.MALFORMED and ep.plan == {(GPIO, 1, 4)}


# ---- plan §2.3 pins ----------------------------------------------------------------------------------------------------------

def test_a_released_assignment_frees_its_pin_and_a_kept_one_is_not_released():
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4), (GPIO, 1, 5)])
    gpio = fixture.Gpio(hst, GPIO)
    gpio.set([(4, fixture.Gpio.OUTPUT_HIGH), (5, fixture.Gpio.OUTPUT_LOW)])
    kept = ep.pin_state(4)
    core.plan_apply(hst, [(GPIO, 1, 4)])                                    # 5 removed, 4 kept
    assert ep.pin_state(4) == kept and not ep.pin_state(5).startswith("gpio")


# ---- plan §2.4 refusals ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("payload, detail", [
    (assignment(GPIO, 1, 4) + assignment(GPIO, 1, 4), m.MALFORMED),        # the same (fn, role, channel) twice
    (assignment(0, 1, 4), m.MALFORMED),                                      # fn 0
    (m.tlv(ASSIGN, struct.pack("<HB", GPIO, 1), critical=True), m.MALFORMED),   # a short assignment
    (assignment(99, 1, 4), m.UNKNOWN_FUNCTION),                             # no such fn
    (assignment(GPIO, 7, 4), m.UNSUPPORTED),                                # not a plan role of gpio
    (assignment(GPIO, 1, 2), m.UNSUPPORTED),                                # not among role_channels' candidates
])
def test_apply_refusals(payload, detail):
    ep, hst = bench()
    r = raw(hst, APPLY, payload)
    assert r.detail == detail and not ep.plan
    if detail == m.UNSUPPORTED:
        assert r.payload[:1] == bytes([ASSIGN | m.TAG_CRITICAL])            # the tag as received (core §2.3)


def test_a_channel_group_mismatch_and_a_pin_conflict():
    """§2.4: channels matching no channel_group - unsupported; a pin another fn holds - unavailable cause 1."""
    ep, hst = bench()
    r = raw(hst, APPLY, assignment(SPI, 1, 18) + assignment(SPI, 2, 13) + assignment(SPI, 3, 5) + assignment(SPI, 4, 4))
    assert r.detail == m.UNSUPPORTED and not ep.plan
    core.plan_apply(hst, [(GPIO, 1, 4)])
    r = raw(hst, APPLY, assignment(LOGIC, 0, 4) + assignment(UART, 1, 4))
    assert r.detail == m.UNAVAILABLE and cause(r) == b"\x01" and ep.plan == {(GPIO, 1, 4)}


def test_an_output_idle_channel_is_refused_by_an_interface_that_does_not_take_it():
    """§2.4: an interface refusing an output idle - unavailable cause 5 (logic only listens: it takes it)."""
    from oep_client import config
    ep, hst = bench()
    ep.load_config([config.item(config.Idle(channel=5, mode="output-high"))], saved=False)
    r = raw(hst, APPLY, assignment(UART, 2, 5))
    assert r.detail == m.UNAVAILABLE and cause(r) == b"\x05" and not ep.plan
    assert raw(hst, APPLY, assignment(LOGIC, 0, 5)).succeeded
