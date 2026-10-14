"""oep.probe.plan (oep-spec a09fa45 interfaces/oep-if-plan.ja.md): the rules tests/test_plan.py leaves to the trace -
a role whose pins a request's arguments choose is no plan role, and the session's end releases a plan that
plan_release itself refuses (a track bound in a capture-group)."""

import pytest

from oep_client import capture, core, virtual_bench, host as h, message as m

from test_plan import APPLY, ASSIGN, assignment, bench, raw


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
