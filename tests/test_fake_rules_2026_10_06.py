"""The fake probe against the probe-side rules added since the 2026-10-02 rule changes: oep-spec 2e70f40 (required and
optional ops, C-21), 975d88c / 598bb26 / 8d91db0 (the lines while a wire does not answer, the rest states), 73a0c37
(cs_setup_ns, search_retries, boot_reset), and docs/v1-rule-change-proposal-2026-10-06.md (b4b08f1, 40291a4). Each test
names its item. Updated to oep-spec 0f455a0 (rule review 2026-10-07 §2: any one reason after the session check, no
ignored TLV, no reset method / boot_reset, the new idle / slot forms)."""

import struct

import pytest

from oep_client import catalog, endpoint, fake, message as m, registry as reg

from test_fake_rules_2026_10_02 import ITEM, PLAN_APPLY, SPEED, Clock, Host, slot_item

CORE = reg.CORE
RV, CFG = reg.TARGET_RISCV_DM, reg.PROBE_CONFIG
LOGIC, GROUP, I2C = reg.FIXTURE_LOGIC, reg.FIXTURE_CAPTURE_GROUP, reg.FIXTURE_I2C_TARGET
UNA = CORE.tlv["unavailable_payload"]


def bench(probe=None, **kw):
    ep = endpoint.Endpoint(probe or fake.p4_bench(), Clock(), **kw)
    h = Host(ep)
    assert h.open().succeeded
    return ep, h


def fn_of(ep, name):
    return ep.fns[name]


def with_tlvs(probe: fake.FakeProbe, name: str, change) -> fake.FakeProbe:
    """The profile with the describe TLVs of every `name` fn passed through change(tlvs) -> tlvs."""
    return fake.FakeProbe(probe.label, probe.max_frame, [
        fake.Offered(o.fn, o.instance, o.name, tuple(change(o.tlvs)), o.revision, o.flags) if o.name == name else o
        for o in probe.offered])


def ops_without(name: str, *without: str):
    """change(): the ops tag (core §7.4) of interface `name` with every op but `without` (the optional ones left out)."""
    def change(tlvs):
        return [t for t in tlvs if t[0] != catalog.OPS] + list(fake.ops_of(name, *without))
    return change


def una(r) -> dict[int, bytes]:
    out = {}
    for tag, v in m.split_tlvs(r.payload):
        out.setdefault(tag, v)
    return out


def attached(ep, h, wire=1, pair=(2, 3)):
    r = h.raw(wire, 0x02, b"\x01" + SPEED + m.tlv(0x03, struct.pack("<HH", *pair), critical=True))
    assert r.succeeded, r.describe()
    return struct.unpack_from("<H", r.payload)[0]


# ---- C-21 (2e70f40): required and optional ops; an undefined op is unknown_operation at order 1 ----------------

def test_c21_an_op_fn_0_does_not_define_is_unknown_operation_not_session_required():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    h = Host(ep)
    assert h.raw(0, 0x50, session=False).detail == m.UNKNOWN_OPERATION        # no session: still order 1's second
    assert h.raw(0, 0x50, session=False).detail == m.UNKNOWN_OPERATION
    assert h.raw(0, m.OP_KEEPALIVE, session=False).detail == m.SESSION_REQUIRED   # a defined op: its session check


def test_c21_an_op_an_interface_does_not_define_is_unknown_operation():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    h = Host(ep)
    for fn in (1, 2, 3, 4, 5, 6):                                             # wire, dm, console, gpio, uart, config
        assert h.raw(fn, 0x7E, session=False).detail == m.UNKNOWN_OPERATION, fn
    assert h.open().succeeded
    assert h.raw(1, 0x04).detail == m.UNKNOWN_OPERATION                       # 0x04 is reserved on the wires


def test_c21_riscv_dm_ops_are_gated_on_the_ops_tag():
    ep, h = bench(fake.esp32_v003())                                          # ops without step
    dm = fn_of(ep, "oep.target.riscv-dm")
    assert not ep.offers(dm, RV.op["step"]) and ep.offers(dm, RV.op["run"])
    assert h.raw(dm, RV.op["step"], struct.pack("<H", 1)).detail == m.UNKNOWN_OPERATION
    ops = next(v for t, v in m.split_tlvs(b"".join(ep._declarations(dm))) if t == catalog.OPS)
    assert ops == bytes([1, 0x7F])                                            # base 1: dmi .. run, no step (core §7.4)
    ep, h = bench(with_tlvs(fake.p4_bench(), "oep.target.riscv-dm",
                            ops_without(RV.name, "read_block", "write_block", "run", "reset", "step")))
    for op in ("read_block", "write_block", "run", "reset", "step"):
        assert h.raw(2, RV.op[op], struct.pack("<H", 1)).detail == m.UNKNOWN_OPERATION, op
    for op, body in (("dmi", struct.pack("<HH", 1, 0)), ("halt", b"\x01\x00"), ("resume", b"\x01\x00")):
        assert h.raw(2, RV.op[op], body).detail == m.NO_CONNECTION, op      # required: the connection is looked up


def test_c21_save_and_erase_without_storage_are_unknown_operation():
    probe = fake.FakeProbe("x", 1024, [fake._config(6, 0, slots_max=4, storage=0) if o.name == CFG.name else o
                                       for o in fake.p4_bench().offered])
    ep, h = bench(probe)
    assert not [t for t in ep.static[6] if t[0] == CFG.tlv["describe"]["storage"]]   # no storage tag (probe.config §2)
    for op in ("save", "erase"):
        assert h.raw(6, CFG.op[op]).detail == m.UNKNOWN_OPERATION
    assert h.raw(6, CFG.op["get"], struct.pack("<H", 0), session=False).succeeded
    ep, h = bench()
    assert h.raw(6, CFG.op["save"]).succeeded and h.raw(6, CFG.op["erase"]).succeeded


def test_c21_capture_query_and_force_need_their_ops():
    ep, h = bench(with_tlvs(fake.p4_x035(), "oep.fixture.logic", ops_without(LOGIC.name, "query", "force")))
    logic = fn_of(ep, "oep.fixture.logic")
    assert h.raw(logic, LOGIC.op["query"], session=False).detail == m.UNKNOWN_OPERATION
    assert h.raw(logic, LOGIC.op["force"]).detail == m.UNKNOWN_OPERATION
    assert h.raw(logic, LOGIC.op["status"], session=False).succeeded
    ep, h = bench(with_tlvs(fake.p4_x035(), "oep.fixture.capture-group", ops_without(GROUP.name, "force")))
    group = fn_of(ep, "oep.fixture.capture-group")
    assert h.raw(group, GROUP.op["force"]).detail == m.UNKNOWN_OPERATION
    ep, h = bench(fake.p4_x035())                                             # both offered
    assert h.raw(fn_of(ep, "oep.fixture.logic"), LOGIC.op["force"]).succeeded
    assert h.raw(fn_of(ep, "oep.fixture.capture-group"), GROUP.op["force"]).succeeded


def test_c21_order_1_refusals_are_not_remembered_and_do_not_restart_the_lease():
    ep, h = bench(fake.esp32_v003())
    dm = fn_of(ep, "oep.target.riscv-dm")
    ep.now.t = 2000
    r = h.raw(dm, RV.op["step"], struct.pack("<H", 1))
    assert r.detail == m.UNKNOWN_OPERATION and h.corr not in ep.resend
    assert ep.expires_ms == 3000                                              # the open's lease, not restarted


# ---- C-31 / C-19: the clock since boot, never back; the boot_id ---------------------------------------------------

def clock(h, session=False):
    """fn 0's clock (core §7.7) -> (boot_id, uptime_ns)."""
    r = h.raw(0, m.OP_CLOCK, session=session)
    assert r.succeeded, r.describe()
    return struct.unpack("<IQ", r.payload)


def test_c31_the_clock_has_the_resolution_it_is_given_and_never_goes_back():
    ns = [5_123_456]
    ep = endpoint.Endpoint(fake.p4_bench(), Clock(), now_ns=lambda: ns[0])
    assert ep.now_ns() == 5_123_456                                           # not cut to ms
    ns[0] = 5_000_000                                                         # a counter read out of order
    assert ep.now_ns() == 5_123_456                                           # does not decrease (core §2.6a)
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    ep.now.t = 7
    assert ep.now_ns() == 7_000_000                                           # ms clock: in ns


def test_c31_a_reboot_starts_the_clock_again_and_clock_says_so():
    ep, h = bench()
    ep.now.t = 51_000
    assert clock(h) == (ep.boot_id, 51_000_000_000)                           # no session needed (core §7.7)
    old = ep.boot_id
    ep.reboot()                                                               # a new boot_id, drawn (C-19)
    assert ep.boot_id != old and ep.now_ns() == 0
    ep.now.t += 1000
    boot, up = clock(h)
    assert boot == ep.boot_id and up == 1_000_000_000                         # since this boot


def test_clock_touches_no_session_lock_or_lease():
    """clock with session_id 0 is answered whoever holds the lock, and moves neither the lease nor the resend table;
    with the holder's id it is a lock-free op of the session (core §4.1, §6.3, §7.7)."""
    ep, h = bench()
    expires, newest = ep.expires_ms, ep.newest_corr
    ep.now.t = 500
    other = Host(ep, session=0x99)
    assert clock(other)[0] == ep.boot_id and other.raw(0, m.OP_CLOCK).detail == m.LOCKED
    assert (ep.expires_ms, ep.newest_corr, ep.holder) == (expires, newest, 0x51)
    assert clock(h, session=True)[1] == 500_000_000 and ep.expires_ms == 500 + ep.lease_ms
    r = h.raw(0, m.OP_CLOCK, m.tlv(0x3D, b"\x01"), session=False)               # no fixed part: a TLV ignored silently
    assert r.succeeded and r.payload[12:] == b""                              # (no ignored list, core §2.3)
    assert h.raw(0, m.OP_CLOCK, b"\x01", session=False).detail == m.MALFORMED  # a broken tail


def test_fn_0_has_exactly_the_eight_core_ops_and_no_subscribe():
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    assert ep.ops[0] == set(CORE.op.values()) == {0x01, 0x02, 0x03, 0x04, 0x10, 0x11, 0x12, 0x13}
    h = Host(ep)
    h.open()
    for op in (0x05, 0x14, m.OP_SUBSCRIBE, m.OP_UNSUBSCRIBE):                 # old plan_release, restart, subscribe
        assert h.raw(0, op, b"\x00\x00\x00\x00\x00\x00").detail == m.UNKNOWN_OPERATION


def test_c19_after_a_reboot_with_a_repeated_boot_id_the_old_session_is_gone():
    ep, h = bench()
    ep.reboot(ep.boot_id)                                                     # a probe whose only source repeated it
    assert h.raw(0, m.OP_KEEPALIVE).detail == m.NO_SESSION                    # nothing holds the lock (core §6.2)
    r = h.open()
    assert r.succeeded and struct.unpack("<II", r.payload) == (3000, ep.boot_id)   # lease_ms boot_id, no resumed


def test_c19_fake_serve_draws_its_boot_id_and_counts_in_ns():
    from oep_client import fake_serve
    a, b = (fake_serve.build(fake_serve.parse([])) for _ in range(2))
    assert len({a.boot_id, b.boot_id, 0x1234ABCD}) == 3                       # drawn, not the fixed default
    assert a.now_ns() % 1_000_000 or a.now_ns() != a.now_ns()                 # ns, not ms in ns (almost always)


# ---- C-20 / C-47 / C-41: the values a probe declares --------------------------------------------------------------

@pytest.mark.parametrize("max_frame, window, inflight", [(63, 4096, 4), (1024, 1023, 4), (1024, 4096, 0)])
def test_c20_confirms_bounds_are_the_fakes_too(max_frame, window, inflight):
    probe = fake.FakeProbe("x", max_frame, fake.p4_bench().offered)
    with pytest.raises(ValueError):
        endpoint.Endpoint(probe, Clock(), window=window, max_inflight=inflight)


@pytest.mark.parametrize("value", [0, reg.LIMITS["max_op_ms_max"] + 1])
def test_c47_max_op_ms_outside_1_to_600000_is_not_a_probe(value):
    probe = with_tlvs(fake.p4_bench(), fake.CORE_NAME,
                      lambda tlvs: [catalog.u32(fake.CORE_MAX_OP_MS, value) if t[0] == fake.CORE_MAX_OP_MS else t
                                    for t in tlvs])
    with pytest.raises(ValueError):
        endpoint.Endpoint(probe, Clock())
    assert reg.LIMITS["max_op_ms_max"] == 600_000


def test_c41_a_uart_bridge_and_tcp_name_no_usb_interface():
    with pytest.raises(AssertionError):
        fake._transports([(fake.TRANSPORT["uart_bridge"], 0)])
    ep = endpoint.Endpoint(fake.rp2350_pins(), Clock())
    index = ep.add_transport(fake.TRANSPORT["tcp"])
    rows = {v[0]: (v[1], v[2]) for t, v in ep.decl[0] if t == fake.CORE_TRANSPORT}
    assert rows == {0: (fake.TRANSPORT["usb_cdc"], 0), index: (fake.TRANSPORT["tcp"], 0xFF)}


# ---- C-36: short messages and roles in the wrong direction --------------------------------------------------------

@pytest.mark.parametrize("data", [
    m.Result(1, m.COMPLETED, m.SUCCESS).pack(),                               # an answer echoed back
    bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 0, 0, 1),                     # an event
    bytes([m.ROLE_REQUEST, 1, 0, 0, 0]),                                      # 5 bytes: no op
    bytes([m.ROLE_REQUEST, 1, 0, 0, 0, m.OP_KEEPALIVE, 0x51, 0, 0]),          # 9: no whole session_id
    bytes([0x81, 1, 0, 0, 0, m.OP_KEEPALIVE, 0x51, 0, 0, 0]),                 # role 0x81: none any more (core §2.4)
    b"",
])
def test_c36_the_probe_discards_what_is_not_a_whole_request(data):
    ep, h = bench()
    assert ep.handle(data, 0) is None and ep.discarded == 1
    assert h.raw(0, m.OP_KEEPALIVE).succeeded                                 # and goes on


# ---- C-39: list is fixed for the boot; first beyond the matches -------------------------------------------------

def test_c39_list_from_beyond_the_matches_gives_the_total_and_count_0():
    ep, h = bench()
    r = h.raw(0, m.OP_LIST, catalog.pack_list_request(50), session=False)     # first(u16) alone (core §7.2)
    total, entries = catalog.unpack_list_result(r.payload)
    assert (total, entries) == (len(ep.names) - 1, [])                       # fn 0 is never listed (core §7.2)


# ---- △12: every channel that is not the probe's own is in its idle state from boot------------------------------------

def test_t12_every_channel_not_the_probes_own_is_parked_at_boot():
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    assert set(ep.parked) == set(range(55)) - {24, 25}                        # channels 55, its own 24 / 25
    assert set(ep.parked.values()) == {0}                                     # Hi-Z without settings


# ---- C-21 (rest), core §4.3: after the session check, any one reason that applies --------------------------------

def assignment(fn, role, ch, critical=True):
    return m.tlv(reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"], struct.pack("<HBH", fn, role, ch), critical=critical)


def test_plan_apply_any_one_reason_that_applies():
    """core §4.3 (rule review 2026-10-07): after the session check the probe checks everything before any change and
    answers with any one reason that applies - no order among malformed, unknown_function (an fn in the payload),
    unsupported and unavailable any more."""
    ep, h = bench(fake.p4_x035())
    # fn 99 does not exist; the i2c-target (fn 8) misses its SCL (malformed)
    r = h.raw(h.plan_fn, PLAN_APPLY, assignment(99, 1, 20) + assignment(8, 1, 21))
    assert r.detail in (m.MALFORMED, m.UNKNOWN_FUNCTION)
    # fn 99 and an unknown critical TLV
    r = h.raw(h.plan_fn, PLAN_APPLY, m.tlv(0x3E, b"", critical=True) + assignment(4, 1, 20) + assignment(99, 1, 21))
    assert r.detail in (m.UNKNOWN_FUNCTION, m.UNSUPPORTED) and (r.detail != m.UNSUPPORTED or r.payload == b"\xbe")
    r = h.raw(h.plan_fn, PLAN_APPLY, m.tlv(0x3E, b"", critical=True) + assignment(4, 1, 20))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\xbe")
    assert not [a for a in ep.plan if a[0] == 4]                              # nothing changed


def test_plan_apply_unsupported_or_unavailable_and_nothing_changes():
    ep, h = bench(fake.p4_x035())
    assert h.raw(h.plan_fn, PLAN_APPLY, assignment(5, 1, 20)).succeeded         # the uart holds channel 20
    # the gpio's first assignment meets that hold (unavailable), its second names a channel it does not offer
    r = h.raw(h.plan_fn, PLAN_APPLY, assignment(4, 1, 20) + assignment(4, 1, 24))
    assert r.detail in (m.UNSUPPORTED, m.UNAVAILABLE)
    detail = una(m.Result(0, 0, 0, r.payload[1:])) if r.detail == m.UNSUPPORTED else una(r)
    assert detail[UNA["channel"]] == struct.pack("<H", 24 if r.detail == m.UNSUPPORTED else 20)
    assert not [a for a in ep.plan if a[0] == 4]


def test_subscribe_is_the_emitting_interfaces_own_op():
    """core §11.3: subscribe / unsubscribe go to the fn that sends notifications (no target fn in the payload);
    logic, analog and capture-group set them in their ops, every other fn answers unknown_operation at order 1."""
    ep, h = bench(fake.p4_x035())
    for fn in (7, 11, 12):                                                    # logic, analog, capture-group
        assert {m.OP_SUBSCRIBE, m.OP_UNSUBSCRIBE} <= ep.ops[fn]
        assert h.raw(fn, m.OP_UNSUBSCRIBE).succeeded                          # not subscribed: nothing, ok
        assert h.raw(fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0)).succeeded and fn in ep.subscribed
    for fn in (0, 4, 13):                                                     # the core, gpio, link
        assert not {m.OP_SUBSCRIBE, m.OP_UNSUBSCRIBE} & ep.ops[fn]
        assert h.raw(fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0)).detail == m.UNKNOWN_OPERATION
    assert h.raw(7, m.OP_SUBSCRIBE, struct.pack("<H", 0)).detail == m.MALFORMED   # min_bytes(u16) max_delay_ms(u32)
    assert h.raw(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), session=False).detail == m.SESSION_REQUIRED
    assert h.raw(99, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0)).detail == m.UNKNOWN_FUNCTION


def cfg_set(h, fn, *items):
    return h.raw(fn, CFG.op["set"], b"".join(items))


def uart_item(fn, baud=115200, fmt=0):
    return m.tlv(ITEM["uart"], struct.pack("<HIB", fn, baud, fmt))


def idle(ch, mode, drive=0xFF):
    """An idle item: channel(u16) mode(u8) drive(u8), 4 bytes (probe.config §1); 0xFF the default level."""
    return m.tlv(ITEM["idle"], struct.pack("<HBB", ch, mode, drive))


def test_probe_config_set_any_one_reason_and_nothing_changes():
    """core §4.3 (rule review 2026-10-07): an item the probe does not declare (unsupported, the item tag as received)
    and an item of another length (malformed, probe.config §1) in one set - either answers, nothing is applied."""
    without_disable = with_tlvs(fake.p4_bench(), "oep.probe.config", lambda tlvs: [
        catalog.tlv(CFG.tlv["describe"]["items"], bytes(v for v in ITEM.values() if v != ITEM["disable"]))
        if t[0] == CFG.tlv["describe"]["items"] else t for t in tlvs])
    ep, h = bench(without_disable)
    disable = m.tlv(ITEM["disable"], struct.pack("<H", 20), critical=True)
    r = cfg_set(h, 6, disable, m.tlv(ITEM["idle"], struct.pack("<HB", 21, 0)))   # an idle of 3 bytes
    assert r.detail in (m.MALFORMED, m.UNSUPPORTED) and (r.detail != m.UNSUPPORTED or r.payload == b"\x87")
    r = cfg_set(h, 6, disable, idle(21, 0))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([ITEM["disable"] | 0x80]))
    r = cfg_set(h, 6, idle(21, 0), m.tlv(ITEM["idle"], struct.pack("<HBBB", 22, 0, 0xFF, 0)))   # 5 bytes: too long
    assert r.detail == m.MALFORMED
    r = h.raw(6, CFG.op["unset"], b"\x02" + bytes([3, ITEM["disable"], 20, 0]) + bytes([5, ITEM["idle"], 21]))
    assert r.detail in (m.MALFORMED, m.UNSUPPORTED)                          # the second row is cut short
    assert not ep.config


def test_probe_config_set_any_one_of_unknown_function_unsupported_malformed():
    ep, h = bench()
    r = cfg_set(h, 6, idle(21, 9), uart_item(99))                             # mode 9 unsupported, fn 99 unknown
    assert r.detail in (m.UNKNOWN_FUNCTION, m.UNSUPPORTED)
    r = cfg_set(h, 6, uart_item(99, baud=0))                                  # baud 0 is the form's; fn 99
    assert r.detail in (m.MALFORMED, m.UNKNOWN_FUNCTION)
    assert cfg_set(h, 6, uart_item(5, baud=0)).detail == m.MALFORMED
    r = cfg_set(h, 6, slot_item(0, 1, (2, 3), attach=5), slot_item(1, 99, (4, 5)))
    assert r.detail in (m.UNKNOWN_FUNCTION, m.UNSUPPORTED)
    assert not ep.config


def test_an_undefined_attach_is_unsupported():
    ep, h = bench()
    r = cfg_set(h, 6, slot_item(0, 1, (2, 3), attach=5))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([ITEM["slot"]]))


def test_c21_a_plan_item_on_a_channel_the_fn_does_not_offer_names_the_item():
    ep, h = bench(fake.p4_x035())
    plan = m.tlv(ITEM["plan"], struct.pack("<HBH", 4, 1, 24), critical=True)  # 24 is the probe's own: no fn offers it
    r = cfg_set(h, 10, plan)
    assert r.detail == m.UNSUPPORTED and r.payload[0] == ITEM["plan"] | 0x80


# ---- 975d88c / 598bb26 / 8d91db0: the lines while the wire does not answer, the rest state ------------------------

def test_lines_rest_undriven_from_an_unanswered_exchange_until_one_succeeds():
    ep, h = bench()
    pair = ep.pairs[1][0]
    cid = attached(ep, h, pair=pair)
    tg = ep._target(1, pair)
    assert ep.pin_state(pair[0]) == ep.pin_state(pair[1]) == "wire"           # the rest state (rvswd §3.1)
    tg.present = False                                                        # the target lost power
    r = h.raw(2, RV.op["dmi"], struct.pack("<HHB", cid, 1, 2) + b"\x11")
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, struct.pack("<HBH", 0, endpoint.LINE, 0))
    assert ep.pin_state(pair[0]) == ep.pin_state(pair[1]) == "wire-free"      # undriven between exchanges (debug §2)
    r = h.raw(2, RV.op["halt"], struct.pack("<H", cid))
    assert r.payload == bytes([endpoint.LINE]) and ep.pin_state(pair[0]) == "wire-free"   # each retry fails the same
    tg.present = True
    assert h.raw(2, RV.op["halt"], struct.pack("<H", cid)).succeeded
    assert ep.pin_state(pair[0]) == "wire"                                    # a success: the rest state again


@pytest.mark.parametrize("op, body", [
    ("reset", b"\x00"), ("step", b""), ("read_block", struct.pack("<IH", 0x20000000, 1)),
    ("write_block", struct.pack("<IH", 0x20000000, 1) + bytes(4)), ("run", struct.pack("<IIBB", 0x20000000, 10, 0, 0)),
    ("resume", b""),
])
def test_every_target_op_fails_with_status_line_when_nothing_answers(op, body):
    ep, h = bench()
    cid = attached(ep, h)
    ep._target(1, (2, 3)).present = False
    r = h.raw(2, RV.op[op], struct.pack("<H", cid) + body)
    assert (r.resolution, r.detail, r.payload[:1]) in {(m.COMPLETED, m.FAILED, bytes([endpoint.LINE])),
                                                      (m.COMPLETED, m.FAILED, b"\x00")}   # done(u16) 0 first
    assert endpoint.LINE in r.payload[:3]


# ---- 975d88c: the reset TLV of attach on a channel whose idle is an output -------------------------------------------

def test_attach_reset_tlv_on_an_output_idle_channel_is_unavailable_and_runs_nothing():
    ep, h = bench(fake.esp32_v003())
    swio = ep.pairs[1][0]
    tg = ep._target(1, swio)
    tg.silent_until_reset = True
    assert h.raw(9, CFG.op["set"], idle(23, 4)).succeeded                    # NRST idles high
    reset = m.tlv(0x05, struct.pack("<HH", 23, 20), critical=True)
    r = h.raw(1, 0x02, b"\x00" + SPEED + reset)
    assert r.detail == m.UNAVAILABLE
    assert una(r) == {UNA["cause"]: bytes([CORE.enum["unavailable_cause"]["held_by_settings"]]),
                      UNA["channel"]: struct.pack("<H", 23)}                  # cause, channel, fn only (core §4.3)
    assert tg.silent_until_reset                                              # the line was never pulled


# ---- 73a0c37: search_retries only when a bring-up ran ----------------------------------------------------------------

def search_retries(r):
    return m.Tail.parse(r.payload[11:]).get(reg.WIRE_RVSWD.tlv["attach_answer"]["search_retries"])


def test_search_retries_only_when_a_bring_up_ran():
    ep, h = bench(fake.esp32_v003())
    swio = ep.pairs[1][0]
    ep._target(1, swio).search_retries = 0x12345                              # saturates
    attach = lambda extra=b"", speed=4_000_000: h.raw(1, 0x02, b"\x00" + m.tlv(0x01, struct.pack("<I", speed),
                                                                               critical=True) + extra)
    assert search_retries(attach()) == b"\xff\xff"                            # a new connection
    assert search_retries(attach()) is None                                   # joined without a bring-up
    assert search_retries(attach(speed=1_000_000)) == b"\xff\xff"             # lowered for max_speed
    assert search_retries(attach(m.tlv(0x05, struct.pack("<HH", 23, 20), critical=True))) == b"\xff\xff"   # reset


# ---- ○2: the reset op is ndmreset and drives no line --------------------------------------------------------------

def test_o2_reset_is_ndmreset_and_moves_no_line():
    """debug §4.3 (rule review 2026-10-07): reset is mode(u8) alone (no method TLV) and uses ndmreset; it never moves
    the reset line. Its answer is status(u8) flags(u8) pc(u32)."""
    ep, h = bench(fake.esp32_v003())
    cid = attached(ep, h, pair=ep.pairs[1][0])
    sid = struct.unpack_from("<H", h.ok(3, reg.TARGET_CONSOLE.op["open"], struct.pack("<HB", cid, 2)))[0]
    for mode in (0, 1, 2):
        r = h.raw(2, RV.op["reset"], struct.pack("<HB", cid, mode))
        assert r.succeeded and len(r.payload) == 6
        status, flags, pc = struct.unpack("<BBI", r.payload)
        assert status == 0 and flags & 1 and (flags & 2) == (2 if mode == 1 else 0)
    marks = [mk for mk in ep.streams[sid].marks if mk[2] == reg.COMMON.enum["mark_kind"]["reset"]]
    assert [mk[4] for mk in marks] == [reg.COMMON.enum["mark_detail_reset"]["ndmreset"]] * 3
    assert "nrst" not in reg.COMMON.enum["mark_detail_reset"] and ep.gpio_log == []
    assert "reset" not in RV.tlv


# ---- △5 / △6: i2c-target's reserved addresses; uart write without TX; spi arm count > length -----------------------

@pytest.mark.parametrize("address, detail", [(0x00, m.UNSUPPORTED), (0x07, m.UNSUPPORTED), (0x78, m.UNSUPPORTED),
                                             (0x7F, m.UNSUPPORTED), (0x80, m.MALFORMED), (0x08, None), (0x77, None)])
def test_t5_i2c_target_refuses_the_reserved_addresses(address, detail):
    ep, h = bench(fake.p4_x035())
    assert h.raw(h.plan_fn, PLAN_APPLY, assignment(8, 1, 20) + assignment(8, 2, 21)).succeeded
    r = h.raw(8, I2C.op["configure"], bytes([address]))                       # address(u8) alone (fixture §3)
    if detail is None:
        assert r.succeeded
    else:
        assert r.detail == detail and (detail != m.UNSUPPORTED or r.payload == b"\x00")


def test_t6_uart_write_without_tx_is_unavailable_cause_6():
    ep, h = bench(fake.p4_x035())
    uart = reg.FIXTURE_UART
    assert h.raw(h.plan_fn, PLAN_APPLY, assignment(5, uart.enum["role"]["rx"], 20)).succeeded   # RX only
    r = h.raw(5, uart.op["write"], struct.pack("<H", 1) + b"x")
    assert r.detail == m.UNAVAILABLE and una(r)[UNA["cause"]] == bytes([CORE.enum["unavailable_cause"]["wrong_state"]])
    assert h.raw(5, uart.op["read"], struct.pack("<BQH", 2, 0, 16), session=False).succeeded
    assert h.raw(h.plan_fn, PLAN_APPLY, assignment(5, uart.enum["role"]["rx"], 20)
                 + assignment(5, uart.enum["role"]["tx"], 21)).succeeded
    assert h.raw(5, uart.op["write"], struct.pack("<H", 1) + b"x").succeeded


def test_t6_spi_arm_count_over_length_is_malformed():
    ep, h = bench(fake.p4_x035())
    spi = reg.FIXTURE_SPI_TARGET
    assert h.raw(9, spi.op["arm"], struct.pack("<HH", 4, 8) + bytes(8)).detail == m.MALFORMED


# ---- 73a0c37: spi-target's cs_setup_ns ------------------------------------------------------------------------------

def test_cs_setup_ns_is_declared_by_a_probe_that_drives_miso_in_software():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    spi = fn_of(ep, "oep.fixture.spi-target")
    tag = reg.FIXTURE_SPI_TARGET.tlv["describe"]["cs_setup_ns"]
    assert [struct.unpack("<I", v)[0] for t, v in ep.decl[spi] if t == tag] == [4000]
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    assert not [t for t in ep.static[fn_of(ep, "oep.fixture.spi-target")] if t[0] == tag]   # MISO at once: left out


def test_group_bind_any_one_reason_and_a_bound_tracks_configure_is_cause_4():
    """core §4.3 (rule review 2026-10-07): the form and the state are checked before any change, and any one reason
    that applies answers; a bound track's own configure / start is unavailable cause 4 (capture §4)."""
    ep, h = bench(fake.p4_x035())
    group = fn_of(ep, "oep.fixture.capture-group")
    ep.groups[group].tracks = [7]                                             # a bound track that is capturing
    ep.captures[7].state = reg.FIXTURE_LOGIC.enum["state"]["capturing"]
    assert h.raw(group, GROUP.op["bind"], struct.pack("<BHH", 2, 7, 7)).detail in (m.MALFORMED, m.UNAVAILABLE)
    assert h.raw(group, GROUP.op["bind"], struct.pack("<BH", 1, 4)).detail in (m.UNSUPPORTED, m.UNAVAILABLE)
    assert h.raw(group, GROUP.op["bind"], struct.pack("<BH", 1, 7)).detail == m.UNAVAILABLE
    assert ep.groups[group].tracks == [7]


def test_a_bound_tracks_start_any_one_reason():
    ep, h = bench(fake.p4_x035())
    ep.captures[7].group = ep.groups[fn_of(ep, "oep.fixture.capture-group")]
    bad = b"\x01"                                                             # a TLV cut short
    assert h.raw(7, LOGIC.op["start"], bad).detail in (m.MALFORMED, m.UNAVAILABLE)
    r = h.raw(7, LOGIC.op["start"])
    assert r.detail == m.UNAVAILABLE and una(r) == {UNA["cause"]: b"\x04"}  # no holder_fn any more
