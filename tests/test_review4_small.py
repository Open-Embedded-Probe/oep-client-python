"""Review 4 (plan, restart, link, probe-config against oep-spec a09fa45): code divergences found and fixed, each test
failing on the code before the fix."""

import struct

from oep_client import message as m, registry as reg
from test_plan import GPIO, PLAN_FN, RELEASE, bench, raw


# ---- plan §2.2: "存在しないfnはunknown_function" / "存在するfnにplanが無ければ無視する" ----------------------------------

def test_plan_release_naming_fn_0_is_ignored_like_any_existing_fn_without_a_plan():
    """fn 0 exists (core §1: the core is fn 0) and never has a plan: plan_release ignores it - only an fn the probe
    lacks is unknown_function. The bench refused fn 0 as unknown_function."""
    from oep_client import core
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    assert hst.call(PLAN_FN, RELEASE, struct.pack("<BHH", 2, 0, GPIO)).payload == b"" and ep.plan == set()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    assert hst.call(PLAN_FN, RELEASE, struct.pack("<BH", 1, 0)).payload == b"" and ep.plan == {(GPIO, 1, 4)}
    assert raw(hst, RELEASE, struct.pack("<BHH", 2, 0, 99)).detail == m.UNKNOWN_FUNCTION and ep.plan == {(GPIO, 1, 4)}
    assert PLAN_FN in ep.names


# ---- link §3: "決めた状態で、元の所有sessionが残る間に正常な要求frameを受けた場合だけidle復帰の保留を取り消し" -------------

def _idle_pending(owner_stays=True):
    from oep_client import core, endpoint, host as h, virtual_bench
    from test_port_speed import COMMIT, PS, TRY

    class Clock:
        t = 0

        def __call__(self):
            return self.t
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)                   # transport 0: its UART bridge
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(60000)
    hst.call(ep.link_fn, PS, struct.pack("<IBH", 500000, TRY, 2000))
    ep.after_answer()
    hst.call(ep.link_fn, PS, struct.pack("<IBH", 500000, COMMIT, 0))
    ep.speed_frame_busy = True                                                  # a frame still on the raised line
    clock.t += 3000                                                             # port_speed_idle_ms: the return is due
    ep.tick()
    assert ep.speed_phase(0) == 3 and ep.speed_fallback == "idle"
    return ep, hst, clock, core


def test_any_good_request_frame_cancels_an_idle_return_while_the_owner_session_remains():
    """A request frame without a session id (port_state, source: lock-free) on the raised port, while the session that
    raised it still holds the lock, cancels the pending idle return (phase 2 again, the idle timer counted anew) - the
    timer is not the lease, which only the owner's requests renew (link §3 return condition 2). The bench and the
    checker's model cancelled only on the owner's own frame."""
    for op in ("port_state", "source"):
        ep, hst, clock, core = _idle_pending()
        payload = struct.pack("<I", 4) if op == "source" else b""
        r = h_call(hst, ep.link_fn, reg.PROBE_LINK.op[op], payload)
        if op == "port_state":
            assert struct.unpack_from("<IB", r) == (500000, 2)
        assert ep.speed_phase(0) == 2 and ep.speed_fallback is None and ep.port_baud(0) == 500000
        ep.speed_frame_busy = False
        clock.t += 2999
        ep.tick()
        assert ep.speed_phase(0) == 2                                           # the timer counts from that frame
        clock.t += 1
        ep.tick()
        ep.speed_after_answer()
        assert ep.speed_phase(0) == 0 and ep.port_baud(0) == 115200


def h_call(hst, fn, op, payload):
    """A lock-free request sent with session id 0 -> the answer's payload."""
    sid, hst.session = hst.session, None
    try:
        return hst.call(fn, op, payload).payload
    finally:
        hst.session = sid


# ---- probe-config §1: label = "text（1〜32byte、coreのtext）"; core §2.1: text is UTF-8, the host replaces C0 / 0x7F ----

def test_the_config_model_takes_a_label_with_control_characters():
    """A label is core text: UTF-8 bytes, control characters included (core §2.1 has the host replace them when it shows
    one). The checker's config model refused a label with a C0 byte or 0x7F as malformed; invalid UTF-8 stays malformed."""
    from oep_client.virtual_config_model import ConfigModel, LABEL
    from oep_client.virtual_core import Reject
    e = ConfigModel().extension
    assert e.form(LABEL, b"\x04\x00a\x01b\x7f") == 4
    try:
        e.form(LABEL, b"\x04\x00\xff")
    except Reject as r:
        assert r.reason == m.MALFORMED
    else:
        raise AssertionError("invalid UTF-8 taken")


# ---- the checkers' models: plan_release §2.2 (unknown fn), plan_apply §2.4 (no malformed for a role / pin set) -----------

def _models():
    from oep_client.virtual_analog_model import AnalogModel
    from oep_client.virtual_capture_group_model import CaptureGroupModel
    from oep_client.virtual_capture_timeline_model import CaptureTimelineModel
    from oep_client.virtual_gpio_model import GpioModel
    from oep_client.virtual_i2c_model import I2cModel
    from oep_client.virtual_logic_model import LogicModel
    from oep_client.virtual_spi_model import SpiModel
    from oep_client.virtual_uart_model import UartModel
    return {"gpio": lambda: GpioModel(lambda: 0, boot_id=17), "uart": lambda: UartModel(boot_id=17),
            "spi": lambda: SpiModel(boot_id=17), "i2c": lambda: I2cModel(boot_id=17),
            "logic": lambda: LogicModel(boot_id=17), "analog": lambda: AnalogModel(boot_id=17),
            "capture-group": lambda: CaptureGroupModel(boot_id=17),
            "capture-timeline": lambda: CaptureTimelineModel(boot_id=17)}


def _plan(model, op, payload, tags=()):
    from oep_client.virtual_core import Reject
    e = model.extension
    try:
        (e.plan if hasattr(e, "plan") else lambda o, p, t: e.dispatch_request(1, o, p, t))(op, payload, list(tags))
        return None
    except Reject as r:
        return r.reason


import pytest  # noqa: E402


@pytest.mark.parametrize("name", sorted(_models()))
def test_every_model_refuses_a_plan_release_naming_an_fn_it_lacks_and_ignores_fn_0(name):
    """plan §2.2: "存在しないfnはunknown_function ... 要求全体を何も変えずに断る"; fn 0 exists without a plan: ignored.
    The models released the fns they knew and ignored the rest."""
    model = _models()[name]()
    before = {k: list(v) for k, v in model.extension.plans.items()}
    assert _plan(model, 17, struct.pack("<BHH", 2, 2, 99)) == m.UNKNOWN_FUNCTION
    assert {k: list(v) for k, v in model.extension.plans.items()} == before
    assert _plan(model, 17, struct.pack("<BH", 1, 0)) is None


@pytest.mark.parametrize("name,rows", [
    ("logic", [(2, 0, 0), (2, 0, 1)]), ("logic", [(2, 0, 0), (2, 1, 0)]),
    ("analog", [(2, 1, 0), (2, 1, 1)]), ("analog", [(2, 1, 0), (2, 3, 0)]),
    ("capture-group", [(3, 0, 2), (3, 0, 3)]), ("capture-timeline", [(3, 0, 2), (3, 0, 3)])])
def test_a_capture_role_twice_or_two_roles_on_one_pin_is_unsupported_not_malformed(name, rows):
    """plan §2.4: malformed is only a form error, the same (fn, role, channel) twice or fn 0; "個別機能が定める役割の個数・
    ピンの組を満たさない" is unsupported with the tag as received. The capture models said malformed."""
    model = _models()[name]()
    tags = [(0x90, struct.pack("<HBH", *r)) for r in rows]
    assert _plan(model, 16, b"", tags) == m.UNSUPPORTED


@pytest.mark.parametrize("name,rows", [("logic", [(2, 0, 0), (2, 1, 1)]), ("analog", [(2, 1, 0), (2, 3, 1)])])
def test_a_capture_model_keeps_its_configuration_on_the_same_set_and_refuses_a_change_while_running(name, rows):
    """plan §2.1 "割り当ての集合が変わらないfnは何もしない" / "変更するfnの機能が動作中なら ... unavailable（cause 6）";
    capture §3.2. The logic and analog models dropped the configuration for the same set and changed a running one."""
    model = _models()[name]()
    e = model.extension
    tags = [(0x90, struct.pack("<HBH", *r)) for r in rows]
    assert _plan(model, 16, b"", tags) is None
    e.config, e.state = {"mode": 2}, 3                                        # configured and capturing
    assert _plan(model, 16, b"", tags) is None and e.config == {"mode": 2} and e.state == 3
    swapped = [(0x90, struct.pack("<HBH", rows[0][0], rows[0][1], rows[1][2])),
               (0x90, struct.pack("<HBH", rows[1][0], rows[1][1], rows[0][2]))]
    assert _plan(model, 16, b"", swapped) == m.UNAVAILABLE and e.config == {"mode": 2} and e.state == 3


# ---- link §3: "試し中のcommit要求がverify期限までに届かなければ、unavailable cause 6で断り、応答後のframe境界で戻す" ------

def test_a_commit_arriving_after_verify_ms_is_refused_cause_6_and_the_port_goes_back_after_its_answer():
    """The bench checked verify_ms only on a tick: a commit arriving after the deadline with no tick between was
    accepted (phase 2). Now the frame's arrival sees the deadline: phase 3, the commit refused cause 6 at the raised rate,
    and back at the boot speed once that answer is out."""
    from oep_client import endpoint, host as h, virtual_bench
    from test_port_speed import CAUSE_6, COMMIT, PS, TRY

    class Clock:
        t = 0

        def __call__(self):
            return self.t
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(60000)
    hst.call(ep.link_fn, PS, struct.pack("<IBH", 500000, TRY, 100))
    ep.after_answer()
    clock.t += 150                                                              # past verify_ms, no tick meanwhile
    r = raw_call(hst, ep.link_fn, PS, struct.pack("<IBH", 500000, COMMIT, 0))
    assert (r.resolution, r.detail, r.payload[:4]) == (m.REJECTED, m.UNAVAILABLE, CAUSE_6)
    assert ep.speed_phase(0) == 3 and ep.port_baud(0) == 500000                 # the refusal goes at the raised rate
    ep.after_answer()
    assert ep.speed_phase(0) == 0 and ep.port_baud(0) == 115200


def raw_call(hst, fn, op, payload):
    from oep_client import host as h
    try:
        return hst.call(fn, op, payload)
    except h.Rejected as e:
        return e.result
