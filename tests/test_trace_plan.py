"""oep.probe.plan (oep-spec a09fa45 interfaces/oep-if-plan.ja.md): the rules tests/test_plan.py leaves to the trace -
a role whose pins a request's arguments choose is no plan role, and the session's end releases a plan that
plan_release itself refuses (a track bound in a capture-group)."""

import pytest

from oep_client import capture, core, fixture, virtual_bench, host as h, message as m

from test_plan import APPLY, ASSIGN, PLAN, assignment, bench, raw


def test_a_role_an_attach_names_its_pins_for_is_no_plan_role():
    """plan preamble: a role whose pins the request's arguments choose (a wire's attach pins and reset line) is not a
    plan role - plan_apply naming it is unsupported with the tag as received, and nothing is assigned."""
    ep, hst = bench()
    wire = core.find_all(hst, "oep.wire.swio")[0]
    swdio = ep.pairs[wire][0][0]
    reset = min(ep.reset_channels[wire])
    for role, channel in ((0, swdio), (1, swdio), (2, swdio), (3, reset)):
        r = raw(hst, APPLY, assignment(wire, role, channel))
        assert r.detail == m.UNSUPPORTED and r.payload[:1] == bytes([ASSIGN | m.TAG_CRITICAL]) and not ep.plan


def test_the_sessions_end_releases_a_plan_plan_release_would_refuse():
    """plan §2.2: plan_release refuses a track bound to another fn (cause 4), but the session's end releases its
    dependants first (core §9) - that refusal does not stop the cleanup: no plan and no binding are left."""
    ep, hst = bench(virtual_bench.p4_x035)
    logic, analog = capture.LogicCapture(hst), capture.AnalogCapture(hst)
    core.plan_apply(hst, [(logic.fn, 0, 20), (analog.fn, 0, 16)])
    logic.configure(rate=1_000_000, samples=100)
    analog.configure(rate=1000, samples=10)
    group = capture.CaptureGroup(hst)
    group.bind([logic, analog])
    with pytest.raises(h.Unavailable) as e:
        core.plan_release(hst, [logic.fn])
    assert e.value.cause == "bound_in_group"
    hst.end()
    assert not ep.plan and all(not g.tracks for g in ep.groups.values())


@pytest.mark.parametrize("check", ["test_one_refusal_changes_nothing_not_even_the_replaced_fns_plan",
                                   "test_a_channel_group_mismatch_and_a_pin_conflict"])
def test_a_bench_that_skips_the_role_and_channel_checks_fails_them(monkeypatch, check):
    """plan §2.1, §2.4 (mutant): a virtual bench whose plan_apply skips the role / channel checks accepts a role the
    interface lacks or a pin another fn holds - the rule's tests in tests/test_plan.py fail on it."""
    import test_plan
    from oep_client import endpoint
    monkeypatch.setattr(endpoint.Endpoint, "_check_plan", lambda self, got, held=True, tag_of=None: None)
    with pytest.raises(AssertionError):
        getattr(test_plan, check)()


def test_the_interface_row_lists_oep_probe_plan_at_revision_1():
    """plan preamble (the interface row): the probe lists oep.probe.plan by that name at revision 1."""
    ep, hst = bench()
    entries = [e for e in core.list_entries(hst) if e.name == "oep.probe.plan"]
    assert [(e.fn, e.revision) for e in entries] == [(11, 1)] and PLAN.revision == 1


@pytest.mark.parametrize("op, code", sorted(PLAN.op.items()))
def test_plan_apply_and_plan_release_need_the_lock(op, code):
    """plan §1 (the op table, lock column): plan_apply 0x10 and plan_release 0x11 both need the lock - with
    session_id 0 they are refused session_required and nothing is assigned or released."""
    from oep_client import endpoint
    assert (op, code) in (("plan_apply", 0x10), ("plan_release", 0x11)) and code not in PLAN.lock_free
    ep, hst = bench()
    core.plan_apply(hst, [(4, 1, 4)])
    payload = assignment(5, 1, 5) if op == "plan_apply" else b"\x00"
    r = m.Result.unpack(ep.handle(m.Request(hst.next_corr(), 11, code, payload, 0).pack(), 0))
    assert r.detail == m.SESSION_REQUIRED and ep.plan == {(4, 1, 4)}
    assert raw(hst, code, payload).succeeded                                 # the same with the lock


def test_a_target_plan_missing_a_role_or_holding_one_twice_is_unsupported():
    """plan §2.4: an assignment set that does not meet the number of roles the function defines (spi-target: cs, sck,
    mosi, miso, each once) is unsupported with the tag as received, nothing assigned; the full set is taken."""
    ep, hst = bench()
    spi, group = 8, [(1, 18), (2, 19), (3, 5), (4, 4)]
    short = b"".join(assignment(spi, role, ch) for role, ch in group[:3])                 # role 4 missing
    twice = b"".join(assignment(spi, role, ch) for role, ch in group) + assignment(spi, 1, 14)   # role 1 twice
    for payload in (short, twice):
        r = raw(hst, APPLY, payload)
        assert r.detail == m.UNSUPPORTED and r.payload[:1] == bytes([ASSIGN | m.TAG_CRITICAL]) and not ep.plan
    assert raw(hst, APPLY, b"".join(assignment(spi, role, ch) for role, ch in group)).succeeded
    assert ep.plan == {(spi, role, ch) for role, ch in group}


# ---- second review additions ---------------------------------------------------------------------------------------

def test_every_profile_lists_one_plan_exactly_when_an_interface_has_plan_roles():
    """plan preamble: a probe listing an interface with plan roles lists oep.probe.plan, at most one; a probe with
    none lists none - every virtual bench profile, and one made without its gpio and uart (the last plan roles)."""
    from oep_client import endpoint
    from test_plan import Clock
    no_roles = virtual_bench.without(virtual_bench.without(virtual_bench.rp2350_pins(), "oep.fixture.gpio"),
                                     "oep.fixture.uart")
    probes = [p() for p in (virtual_bench.p4_x035, virtual_bench.esp32_v003, virtual_bench.esp32_v003_64,
                            virtual_bench.p4_bench, virtual_bench.rp2350_pins, virtual_bench.core_v1)] + [no_roles]
    for probe in probes:
        ep = endpoint.Endpoint(probe, Clock())
        hst = h.Host(lambda b, ep=ep: ep.handle(b, 0))
        names = [e.name for e in core.list_entries(hst)]
        roles = bool(virtual_bench.PLAN_ROLE_INTERFACES & set(names))
        assert names.count("oep.probe.plan") == (1 if roles else 0), (probe.label, names)
    assert "oep.probe.plan" not in [o.name for o in no_roles.offered]


def test_the_replaced_fns_own_assignments_do_not_conflict_with_its_new_ones():
    """plan §2.1: each interface checks the request as if the replaced fns' current assignments were gone (core §8.1's
    conflict check included) - moving the uart's channel 5 from RX to TX, and handing gpio's channel 4 to the uart while
    gpio moves to 5 in the same request, are accepted; the same move onto a channel a fn not named holds is cause 1."""
    from test_plan import GPIO, UART, cause
    ep, hst = bench()
    core.plan_apply(hst, [(UART, 1, 5)])
    assert raw(hst, APPLY, assignment(UART, 2, 5)).succeeded and ep.plan == {(UART, 2, 5)}
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    assert raw(hst, APPLY, assignment(GPIO, 1, 5) + assignment(UART, 1, 4)).succeeded
    assert ep.plan == {(GPIO, 1, 5), (UART, 1, 4)}
    r = raw(hst, APPLY, assignment(UART, 1, 5))                              # 5 is gpio's, gpio not named
    assert r.detail == m.UNAVAILABLE and cause(r) == b"\x01" and ep.plan == {(GPIO, 1, 5), (UART, 1, 4)}


def test_an_unchanged_set_keeps_a_running_function_as_it_is():
    """plan §2.1: an fn whose set of assignments does not change is left alone - a configured (running) fixture UART
    named again with the same set stays configured at its baud with its received bytes, and its pins are not touched."""
    from test_plan import UART
    ep, hst = bench()
    core.plan_apply(hst, [(UART, 1, 5), (UART, 2, 13)])
    uart = fixture.FixtureUart(hst, UART)
    uart.configure(9600)
    ep.uart_rx(UART, b"kept")
    before = (dict(ep.uart_baud), ep.pin_state(5), ep.pin_state(13))
    assert raw(hst, APPLY, assignment(UART, 2, 13) + assignment(UART, 1, 5)).succeeded   # the same set, other order
    assert (dict(ep.uart_baud), ep.pin_state(5), ep.pin_state(13)) == before
    assert uart.status().configured and uart.read(uart.FROM_OLDEST).data == b"kept"


def test_plan_release_checks_every_fn_before_it_releases_any():
    """plan §2.2: every fn named is checked before anything is released - a releasable gpio named before a track bound
    in a capture-group is kept when the request is refused cause 4, and n = 0 (every fn) is refused the same way."""
    ep, hst = bench(virtual_bench.p4_x035)
    logic, analog = capture.LogicCapture(hst), capture.AnalogCapture(hst)
    gpio = core.find_all(hst, "oep.fixture.gpio")[0]
    core.plan_apply(hst, [(gpio, 1, 4), (logic.fn, 0, 20), (analog.fn, 0, 16)])
    logic.configure(rate=1_000_000, samples=100)
    analog.configure(rate=1000, samples=10)
    capture.CaptureGroup(hst).bind([logic, analog])
    before = set(ep.plan)
    with pytest.raises(h.Unavailable) as e:
        core.plan_release(hst, [gpio, logic.fn])
    assert e.value.cause == "bound_in_group" and ep.plan == before
    plan_fn = core.find_all(hst, "oep.probe.plan")[0]
    try:
        r = hst.call(plan_fn, PLAN.op["plan_release"], b"\x00")
    except h.Rejected as err:
        r = err.result
    assert r.detail == m.UNAVAILABLE and ep.plan == before
