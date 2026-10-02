"""The fake probe against the rule changes of oep-spec docs/v1-rule-change-proposal-2026-10-02.md (applied 09622ef..
536fc99): each test names its item. The final text is the spec's (core, debug, fixture, capture, probe-config)."""

import socket
import struct
import subprocess
import sys

import pytest

from oep_client import endpoint, fake, fake_capture, message as m, registry as reg

from test_fake_spec import ITEM, SPEED, Clock, Host, bind_item, slot_item, state

CORE = reg.CORE
UNA = CORE.tlv["unavailable_payload"]
IDLE = reg.PROBE_CONFIG.enum["idle_mode"]
UNKNOWN = 0x3E                                                    # a tag no context defines


def bench(profile=fake.p4_bench, **kw):
    ep = endpoint.Endpoint(profile(), Clock(), **kw)
    h = Host(ep)
    assert h.open().succeeded
    return ep, h


def tlvs(payload: bytes) -> list[tuple[int, bytes]]:
    return m.split_tlvs(payload)


def una(r) -> dict[int, bytes]:
    """An unavailable payload's TLVs, first of each tag."""
    out = {}
    for tag, v in tlvs(r.payload):
        out.setdefault(tag, v)
    return out


def idle_item(ch, mode, tag=ITEM["idle"]):
    return bytes([tag, 3]) + struct.pack("<HB", ch, mode)


def label_item(ch, text: bytes, tag=ITEM["label"]):
    return m.tlv(tag & 0x7F, struct.pack("<H", ch) + text, critical=bool(tag & 0x80))


def pins(d, c, critical=True):
    return m.tlv(0x03, struct.pack("<HH", d, c), critical=critical)


# ---- C-02: a value a later revision may define is unsupported ----------------------------------------------------

def test_c02_list_flags_reserved_bits_are_unsupported():
    ep, h = bench()
    r = h.raw(0, m.OP_LIST, struct.pack("<BHB", 0x02, 0, 0), session=False)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    assert h.raw(0, m.OP_LIST, struct.pack("<BHB", 0x01, 0, 0), session=False).succeeded


def test_c02_attach_method_and_reset_mode_before_the_connection_lookup():
    ep, h = bench()
    r = h.raw(1, 0x02, b"\x02" + SPEED)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    r = h.raw(2, reg.TARGET_RISCV_DM.op["reset"], struct.pack("<HB", 0x777, 3))   # no such connection, mode 3
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")                      # order 6 before order 8
    assert h.raw(2, reg.TARGET_RISCV_DM.op["reset"], struct.pack("<HB", 0x777, 0)).detail == m.NO_CONNECTION
    method = reg.TARGET_RISCV_DM.tlv["reset"]["method"]
    r = h.raw(2, reg.TARGET_RISCV_DM.op["reset"], struct.pack("<HB", 0x777, 0) + m.tlv(method, b"\x02", critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([method | 0x80]))      # 2 reserved: a value it cannot handle


def test_c02_read_from_4_unsupported_and_from_3_with_a_wide_arg_malformed():
    ep, h = bench()
    read = reg.FIXTURE_UART.op["read"]
    r = h.raw(5, read, struct.pack("<BQH", 4, 0, 16), session=False)              # no plan: the format comes first
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    assert h.raw(5, read, struct.pack("<BQH", 3, 0x100, 16), session=False).detail == m.MALFORMED
    assert h.raw(5, read, struct.pack("<BQH", 3, 0xFF, 16), session=False).detail == m.UNAVAILABLE


def test_c02_gpio_drive_of_an_undefined_kind_is_ignored_or_unsupported_when_critical():
    ep, h = bench()
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 20), critical=True))
    drive = reg.FIXTURE_GPIO.tlv["set"]["drive"]
    body = bytes([1]) + struct.pack("<HB", 20, 4)
    r = h.raw(4, reg.FIXTURE_GPIO.op["set"], body + m.tlv(drive, struct.pack("<BBH", 0, 2, 0)))
    assert r.succeeded and r.payload == bytes([0x7F, 1, drive])
    r = h.raw(4, reg.FIXTURE_GPIO.op["set"], body + m.tlv(drive, struct.pack("<BBH", 0, 2, 0), critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([drive | 0x80]))


def test_c02_probe_config_enums_and_bits_a_later_revision_may_define():
    ep, h = bench()
    pair = ep.pairs[1][0]
    raw_slot = slot_item(0, 1, pair)                              # attach 1 at offset 7 of the value
    v = bytearray(raw_slot[2:])
    v[7], v[8:12] = 2, bytes(4)                                   # slot attach 2 (and no retry_ms)
    r = h.raw(6, 0x02, m.tlv(ITEM["slot"], bytes(v)))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([ITEM["slot"]]))
    r = h.raw(6, 0x02, bind_item(0, 0, [(3, 0)]))                 # a stream kind 3
    assert r.detail == m.UNSUPPORTED
    r = h.raw(6, 0x02, idle_item(20, 5))
    assert (r.detail, r.payload[:1]) == (m.UNSUPPORTED, bytes([ITEM["idle"]]))


# ---- C-03: repeated, short and extended request TLVs ------------------------------------------------------------

def test_c03_a_non_repeating_tag_twice_is_malformed_critical_or_not():
    ep, h = bench()
    pair = pins(*ep.pairs[1][0])
    assert h.raw(1, 0x02, b"\x00" + SPEED + SPEED + pair).detail == m.MALFORMED
    once = m.tlv(0x01, struct.pack("<I", 1_000_000))
    assert h.raw(1, 0x02, b"\x00" + once + once + pair).detail == m.MALFORMED
    assert not ep.conns                                           # nothing ran


def test_c03_short_is_malformed_long_is_unsupported_critical_or_ignored():
    ep, h = bench()
    pair = ep.pairs[1][0]
    assert h.raw(1, 0x02, b"\x00" + m.tlv(0x01, b"\x00\x09\x3d", critical=True)).detail == m.MALFORMED
    long_pins = m.tlv(0x03, struct.pack("<HHB", *pair, 0), critical=True)
    r = h.raw(1, 0x02, b"\x00" + SPEED + long_pins)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x83")      # never extended: the tag as received
    r = h.raw(1, 0x02, b"\x00" + SPEED + pins(*pair) + m.tlv(0x04, b"\x00\x00"))   # idle_clock too long, not critical
    assert r.succeeded and r.payload.endswith(bytes([0x7F, 1, 0x04]))
    assert ep.conns[ep._conn_at(1, pair)].idle_clock == 0         # ignored as a whole


def test_c03_malformed_later_in_the_tail_beats_an_unknown_critical_before_it():
    ep, h = bench()
    r = h.raw(1, 0x02, b"\x00" + m.tlv(UNKNOWN, b"", critical=True) + SPEED + SPEED)
    assert r.detail == m.MALFORMED


def test_c03_role_assignment_and_gpio_drive_repeat():
    ep, h = bench()
    ra = lambda ch: m.tlv(0x10, struct.pack("<HBH", 4, 1, ch), critical=True)
    assert h.raw(0, m.OP_PLAN_APPLY, ra(20) + ra(21)).succeeded
    drive = reg.FIXTURE_GPIO.tlv["set"]["drive"]
    body = bytes([2]) + struct.pack("<HBHB", 20, 4, 21, 4)
    r = h.raw(4, reg.FIXTURE_GPIO.op["set"], body + m.tlv(drive, struct.pack("<BBH", 0, 0, 1))
              + m.tlv(drive, struct.pack("<BBH", 1, 0, 2)))
    assert r.succeeded and ep.gpio_drive == {20: 1, 21: 2}


# ---- C-04: ignored at most 16 entries, the 16th 0x00 = more ------------------------------------------------------

def test_c04_more_than_16_ignored_lists_15_and_0x00():
    ep, h = bench()
    tail = b"".join(m.tlv(0x30 + k, b"") for k in range(20))
    r = h.raw(0, m.OP_KEEPALIVE, tail)
    assert r.succeeded and r.payload == bytes([0x7F, 16]) + bytes(range(0x30, 0x30 + 15)) + b"\x00"
    tail = b"".join(m.tlv(0x30 + k, b"") for k in range(16))
    assert h.raw(0, m.OP_KEEPALIVE, tail).payload == bytes([0x7F, 16]) + bytes(range(0x30, 0x40))


def test_c04_an_answer_keeps_room_for_ignored_and_never_drops_it():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())            # 64-byte frames
    h = Host(ep)
    h.open()
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 5, 1, 21), critical=True))
    ep.uart_rx(5, bytes(range(1, 200)))
    tail = b"".join(m.tlv(0x30 + k, b"") for k in range(18))
    r = h.raw(5, reg.FIXTURE_UART.op["read"], struct.pack("<BQH", 1, 0, 1000) + tail, session=False)
    assert r.succeeded and len(r.payload) + m.RESULT_HEADER <= 64
    _, flags, n = struct.unpack_from("<QBH", r.payload)
    ignored = r.payload[11 + n:]
    assert flags & 1 and ignored[:1] == b"\x7F" and ignored[-1] == 0 and len(ignored) == 18


def test_c04_ignored_tlv_cut_to_fit_ends_in_0x00():
    assert endpoint.ignored_tlv([0x31, 0x32, 0x33, 0x34], room=4) == bytes([0x7F, 2, 0x31, 0x00])
    assert endpoint.ignored_tlv([0x31, 0x32], room=3) == bytes([0x7F, 1, 0x00])


# ---- C-05 / C-15: confirm ----------------------------------------------------------------------------------------

def confirm(h, lo=1, hi=1, corr=None):
    return h.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([lo, hi]), session=False, corr=corr)


def test_c05_confirm_names_the_transport_it_came_on():
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    for index in (0, 1, 3):
        r = confirm(Host(ep, transport=index))
        assert r.succeeded and len(r.payload) == 17 + 3
        assert m.Tail.parse(r.payload[17:]).get(CORE.tlv["confirm_answer"]["transport"]) == bytes([index])
        assert ep.revision_in_use[index] == 1


def test_c15_no_revision_in_range_carries_the_supported_range_and_min_above_max_is_malformed():
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    h = Host(ep)
    r = confirm(h, 2, 3)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00" + m.tlv(0x01, b"\x01\x01"))
    assert confirm(h, 2, 1).detail == m.MALFORMED


# ---- C-10: plan_apply / plan_release on a probe without plan roles -----------------------------------------------

def test_c10_plan_ops_are_unknown_operation_without_plan_roles():
    probe = fake.FakeProbe("bare", 256, [o for o in fake.p4_bench().offered
                                         if o.name in ("oep.core", "oep.wire.rvswd", "oep.target.riscv-dm")])
    ep = endpoint.Endpoint(probe, Clock())
    h = Host(ep)
    h.open()
    assert h.raw(0, m.OP_PLAN_APPLY, b"").detail == m.UNKNOWN_OPERATION
    assert h.raw(0, m.OP_PLAN_RELEASE, b"\x00").detail == m.UNKNOWN_OPERATION


# ---- C-16 / C-17 / C-18 / C-22: the session ----------------------------------------------------------------------

def test_c16_a_rejected_answer_is_remembered_and_a_resend_replays_it():
    ep, h = bench()
    body = struct.pack("<HBH", 4, 1, 3)                           # channel 3: not the gpio's
    first = h.raw(0, m.OP_PLAN_APPLY, m.tlv(0x10, body, critical=True), corr=50)
    assert first.detail == m.UNSUPPORTED
    again = h.raw(0, m.OP_PLAN_APPLY, m.tlv(0x10, body, critical=True), corr=50)
    assert again == first and ep.newest_corr == 50                # from the table, not run again
    assert h.raw(0x77, 0x01, b"", corr=51).detail == m.UNKNOWN_FUNCTION   # order 1: not in the table
    assert 51 not in ep.resend


def test_c17_the_lease_is_rounded_into_1000_to_60000_and_restarts_on_rejected_answers():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    h = Host(ep)
    assert struct.unpack_from("<I", h.open(lease=1).payload)[0] == 1000
    assert struct.unpack_from("<I", h.open(lease=100_000).payload)[0] == 60000
    h.open(lease=1000)
    ep.now = lambda: 900
    assert h.raw(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 3), critical=True)).detail == m.UNSUPPORTED
    assert ep.expires_ms == 1900                                  # restarted by a rejected answer of the holder
    ep.now = lambda: 1000
    h.raw(0x77, 0x01, b"")                                        # unknown_function (order 1): no restart
    assert ep.expires_ms == 1900


def test_c18_session_id_0_and_an_open_with_role_0x81_are_malformed():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    h = Host(ep, session=0)
    assert h.open().detail == m.MALFORMED
    h = Host(ep, session=0x1234)
    r = h.raw(0, m.OP_OPEN, struct.pack("<IIB", 0x1234, 3000, 0), session=True)
    assert r.detail == m.MALFORMED and ep.holder is None


@pytest.mark.parametrize("force, owner", [(2, None), (0, b"bad\x1b[31m"), (0, b"\xff\xfe"), (0, b"x" * 33), (0, b"")])
def test_c22_booleans_and_text_in_a_request(force, owner):
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    tail = b"" if owner is None else m.tlv(CORE.tlv["open"]["owner"], owner)
    r = Host(ep).raw(0, m.OP_OPEN, struct.pack("<IIB", 0x51, 3000, force) + tail, session=False)
    assert r.detail == m.MALFORMED and ep.holder is None


def test_c22_a_utf8_owner_is_taken():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    h = Host(ep)
    assert h.open(owner="日本 bench".encode()).succeeded and ep.owner == "日本 bench".encode()


# ---- C-23 / C-30 / C-24: names, instances, unit_id --------------------------------------------------------------

def test_c30_every_profile_numbers_its_instances_per_name_and_revision():
    for name, profile in fake.PROFILES.items():
        assert profile().instance_errors() == [], name
    bad = fake.FakeProbe("x", 256, [fake.Offered(0, 0, "oep.core"), fake.Offered(1, 1, "oep.fixture.gpio")])
    assert bad.instance_errors()


def test_c24_a_profile_with_an_x_unit_id():
    probe = fake.with_unit_id(fake.esp32_v003(), "x-esp32")
    ep = endpoint.Endpoint(probe, Clock())
    r = Host(ep).raw(0, m.OP_DESCRIBE, struct.pack("<HH", 0, 0), session=False)
    assert (CORE.tlv["describe"]["unit_id"], b"x-esp32") in m.split_tlvs(r.payload[1:])


# ---- debug: P2-★1 / ★5 / ○1 / ○4 / ★4 -------------------------------------------------------------------------------

def test_p2_1_count_0_leaves_out_every_idle_item_channel():
    ep, h = bench()
    a, b, c = ep.pairs[1]
    h.ok(6, 0x02, idle_item(a[0], IDLE["pull_up"]))               # an input idle on pair A
    rd = m.Reader(h.ok(1, 0x01, b"\x00"))
    tried, count = rd.take("BB")
    assert tried == 2                                             # B and C only
    assert h.raw(1, 0x01, b"\x01" + struct.pack("<HH", *a)).succeeded   # named: an input idle is accepted


def test_p2_1_a_named_output_idle_channel_is_unavailable_cause_5_holder_kind_7():
    ep, h = bench()
    a = ep.pairs[1][0]
    h.ok(6, 0x02, idle_item(a[1], IDLE["output_high"]))
    for r in (h.raw(1, 0x01, b"\x01" + struct.pack("<HH", *a)), h.raw(1, 0x02, b"\x00" + SPEED + pins(*a))):
        t = una(r)
        assert r.detail == m.UNAVAILABLE
        assert t[UNA["cause"]] == b"\x05" and t[UNA["holder_kind"]] == b"\x07" and t[UNA["channel"]] == struct.pack("<H", a[1])
    assert not ep.conns


def test_p2_1_an_attach_without_pins_has_no_idle_item_candidate():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())            # one SWIO pair
    h = Host(ep)
    h.open()
    swio = ep.pairs[1][0][0]
    h.ok(9, 0x02, idle_item(swio, IDLE["pull_up"]))
    r = h.raw(1, 0x02, b"\x00" + SPEED)
    assert r.detail == m.UNAVAILABLE and una(r)[UNA["holder_kind"]] == b"\x07"
    assert h.raw(1, 0x02, b"\x00" + SPEED + pins(swio, 0xFFFF)).succeeded   # named: an input idle is accepted


def test_p2_1_the_pins_of_a_closed_connection_go_back_to_their_idle_state():
    ep, h = bench()
    a = ep.pairs[1][0]
    cid = struct.unpack_from("<H", h.ok(1, 0x02, b"\x00" + SPEED + pins(*a)))[0]
    assert ep.pin_state(a[0]) == "wire"
    h.ok(1, 0x03, struct.pack("<H", cid))
    assert ep.pin_state(a[0]) == "idle hi-z"


def test_p2_5_an_undeclared_combination_is_unsupported_scan_with_its_index():
    ep, h = bench()
    a, b = ep.pairs[1][:2]
    r = h.raw(1, 0x01, b"\x02" + struct.pack("<HHHH", *a, a[0], b[1]))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00" + m.tlv(0x40, b"\x01"))
    r = h.raw(1, 0x02, b"\x00" + SPEED + pins(a[0], b[1], critical=False))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x03")       # attach: the pins tag as received
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 8), critical=True))
    rp = endpoint.Endpoint(fake.rp2350_pins(), Clock())
    hp = Host(rp)
    hp.open()
    hp.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 1), critical=True))
    r = hp.raw(1, 0x01, b"\x01" + struct.pack("<HH", 0, 1))       # a held channel: unavailable cause 1, the channel
    assert r.detail == m.UNAVAILABLE and una(r)[UNA["cause"]] == b"\x01" and una(r)[UNA["channel"]] == b"\x01\x00"


@pytest.mark.parametrize("version, found", [(2, True), (3, True), (4, True), (1, False), (15, False), (0, False)])
def test_p2_o1_found_is_version_2_or_more_and_not_15(version, found):
    ep, h = bench()
    for tg in ep.targets.values():
        tg.version = version
    rd = m.Reader(h.ok(1, 0x01, b"\x00"))
    assert rd.take("BB")[1] == (3 if found else 0)


def test_p2_o4_a_halt_that_times_out_clears_haltreq_and_a_step_that_cannot_halt_again_says_step_left():
    ep, h = bench()
    a = ep.pairs[1][0]
    cid = struct.unpack_from("<H", h.ok(1, 0x02, b"\x00" + SPEED + pins(*a)))[0]
    tg = ep._target(1, a)
    tg.halt_stuck, tg.haltreq = True, True
    r = h.raw(2, reg.TARGET_RISCV_DM.op["halt"], struct.pack("<H", cid))
    assert (r.detail, r.payload[0], tg.haltreq) == (m.FAILED, endpoint.TIMEOUT, False)
    tg.halt_stuck, tg.halted = False, True
    tg.step_stuck = "runs"
    r = h.raw(2, reg.TARGET_RISCV_DM.op["step"], struct.pack("<H", cid))
    assert r.payload[0] == endpoint.STATE and m.Tail.parse(r.payload[10:]).get(0x01) == b""
    assert not tg.halted and tg.dcsr_step
    tg.halted, tg.step_stuck = True, "halts"
    r = h.raw(2, reg.TARGET_RISCV_DM.op["step"], struct.pack("<H", cid))
    status, moved, before, after = struct.unpack_from("<BBII", r.payload)
    assert status == endpoint.STATE and after != before and tg.halted and not tg.dcsr_step
    assert m.Tail.parse(r.payload[10:]).get(0x01) is None


def test_p2_4_the_attach_answer_carries_search_retries():
    ep, h = bench()
    a = ep.pairs[1][0]
    ep._target(1, a).search_retries = 70000
    r = h.ok(1, 0x02, b"\x00" + SPEED + pins(*a))
    assert m.Tail.parse(r[11:]).get(reg.WIRE_RVSWD.tlv["attach_answer"]["search_retries"]) == b"\xff\xff"


# ---- fixture: P2-★2 / ★3 / ○13 ---------------------------------------------------------------------------------------

def x035():
    ep = endpoint.Endpoint(fake.with_i2c_pullups(fake.p4_x035()), Clock())
    h = Host(ep)
    h.open()
    return ep, h


def test_p2_2_spi_target_drives_miso_only_while_cs_is_active():
    ep, h = x035()
    spi = 9
    plan = b"".join(m.tlv(0x10, struct.pack("<HBH", spi, role, ch), critical=True)
                    for role, ch in ((1, 30), (2, 31), (3, 32), (4, 33)))
    h.ok(6 + 4, 0x02, idle_item(32, IDLE["pull_up"]))             # MISO's idle: a pull-up input
    h.ok(0, m.OP_PLAN_APPLY, plan)
    assert ep.pin_state(32) == "idle pull-up"                     # before configure: the idle state
    h.ok(spi, reg.FIXTURE_SPI_TARGET.op["configure"], b"\x00\x00")
    assert ep.pin_state(32) == "miso-hi-z" and ep.pin_state(30) == "input" and ep.pin_state(33) == "input"
    ep.spi_select(spi, True)
    assert ep.pin_state(32) == "miso-driven"
    ep.spi_select(spi, False)
    assert ep.pin_state(32) == "miso-hi-z"


def test_p2_3_i2c_target_open_drain_and_declared_pullups():
    ep, h = x035()
    i2c = 8
    r = h.raw(0, m.OP_DESCRIBE, struct.pack("<HH", i2c, 0), session=False)
    d = dict(m.split_tlvs(r.payload[1:]))
    assert struct.unpack("<I", d[0x06])[0] & 0x04 and struct.unpack("<I", d[0x42])[0] == 45000
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", i2c, 1, 30), critical=True)
         + m.tlv(0x10, struct.pack("<HBH", i2c, 2, 31), critical=True))
    assert ep.pin_state(30) == "idle hi-z"                        # state 0: released, nothing ACKed
    h.ok(i2c, reg.FIXTURE_I2C_TARGET.op["configure"], b"\x42\x01")
    assert ep.pin_state(30) == ep.pin_state(31) == "open-drain pull-up"
    plain = endpoint.Endpoint(fake.p4_x035(), Clock())
    assert plain.i2c_pullup_ohms == {8: 0}                        # the profiles declare none


def test_p2_o13_taking_a_plan_changes_no_pin_and_an_analog_plan_on_an_output_idle_is_refused():
    ep, h = x035()
    h.ok(10, 0x02, idle_item(16, IDLE["output_high"]) + idle_item(20, IDLE["output_low"]) + idle_item(21, IDLE["output_high"]))
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 20), critical=True)
         + m.tlv(0x10, struct.pack("<HBH", 7, 0, 21), critical=True))
    assert ep.pin_state(20) == "gpio 3" and ep.gpio_log == []     # gpio: the idle's output low kept, no set yet
    assert ep.pin_state(21) == "idle output-high"                 # logic: it only listens
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 5, 2, 22), critical=True))
    assert ep.pin_state(22) == "uart-tx-high"                     # uart TX: from the plan
    r = h.raw(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 11, 0, 16), critical=True))
    t = una(r)
    assert r.detail == m.UNAVAILABLE and t[UNA["cause"]] == b"\x05" and t[UNA["holder_kind"]] == b"\x07"


# ---- capture: P2-○8 / ○10 / ○11 ----------------------------------------------------------------------------------------

CAP_TLV = fake_capture.TLV


def test_p2_o8_samples_rounded_down_and_the_critical_tlvs():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())            # logic: max_samples 65536, one-shot only
    h = Host(ep)
    h.open()
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 6, 0, 4), critical=True))
    cfg = reg.FIXTURE_LOGIC.op["configure"]
    rate = m.tlv(CAP_TLV["rate"], struct.pack("<I", 1_000_000), critical=True)
    r = h.ok(6, cfg, rate + m.tlv(CAP_TLV["samples"], struct.pack("<I", 1 << 20)))
    assert m.Tail.parse(r).get(reg.FIXTURE_ANALOG.tlv["configure_answer"]["actual_samples"]) == struct.pack("<I", 65536)
    r = h.raw(6, cfg, rate + m.tlv(CAP_TLV["mode"], b"\x03", critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["mode"] | 0x80]))
    r = h.raw(6, cfg, rate + m.tlv(CAP_TLV["mode"], b"\x07"))     # undefined, not critical: ignored
    assert r.succeeded and m.Tail.parse(r.payload).ignored == [CAP_TLV["mode"]]
    r = h.raw(6, cfg, m.tlv(CAP_TLV["rate"], struct.pack("<I", 9_000_000), critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["rate"] | 0x80]))
    r = h.raw(6, cfg, rate + m.tlv(CAP_TLV["trigger"], struct.pack("<BBI", 3, 0, 0), critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["trigger"] | 0x80]))   # cross up: the analog's


def test_p2_o10_capture_group_with_nothing_bound_and_the_checks_before_start():
    ep, h = x035()
    grp = reg.FIXTURE_CAPTURE_GROUP.op
    r = h.raw(12, grp["start"])
    assert r.detail == m.UNAVAILABLE and una(r)[UNA["cause"]] == b"\x06"
    assert h.raw(12, grp["stop"]).succeeded and h.raw(12, grp["force"]).succeeded
    assert h.ok(12, grp["status"])[0] == 0
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 7, 0, 30), critical=True)
         + m.tlv(0x10, struct.pack("<HBH", 11, 0, 17), critical=True))
    for fn, hz in ((7, 1_000_000), (11, 1000)):
        h.ok(fn, reg.FIXTURE_LOGIC.op["configure"], m.tlv(CAP_TLV["mode"], b"\x03", critical=True)
             + m.tlv(CAP_TLV["rate"], struct.pack("<I", hz), critical=True))
    h.ok(12, grp["bind"], struct.pack("<BHH", 2, 7, 11))
    h.ok(0, m.OP_SUBSCRIBE, struct.pack("<HHI", 7, 0, 0))         # fn 11 has no subscription
    r = h.raw(12, grp["start"])
    assert r.detail == m.UNAVAILABLE and una(r)[UNA["fn"]] == struct.pack("<H", 11)
    assert ep.captures[7].state == fake_capture.STATE["configured"]   # nothing started


def test_p2_o11_a_read_past_the_write_position():
    ep, h = bench()
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 5, 1, 20), critical=True))
    ep.uart_rx(5, b"abc")
    r = h.raw(5, reg.FIXTURE_UART.op["read"], struct.pack("<BQH", 0, 100, 16), session=False)
    assert r.payload == struct.pack("<QBH", 3, 0, 0)


# ---- probe.config: PC-1 / PC-3 / PC-4 / PC-5 / PC-8 ---------------------------------------------------------------------

def test_pc3_an_idle_pull_the_channel_lacks_is_unsupported():
    ep, h = bench()
    ep.no_pull = {20: {IDLE["pull_down"]}}
    r = h.raw(6, 0x02, idle_item(20, IDLE["pull_down"], tag=ITEM["idle"] | 0x80))
    assert (r.detail, r.payload[:1]) == (m.UNSUPPORTED, bytes([ITEM["idle"] | 0x80]))
    assert h.raw(6, 0x02, idle_item(20, IDLE["pull_up"])).succeeded


def test_pc3_a_saved_idle_pull_the_channel_lacks_fails_the_whole_save_at_boot():
    ep, h = bench()
    h.ok(6, 0x02, idle_item(20, IDLE["pull_down"]) + label_item(21, b"x"))
    h.ok(6, reg.PROBE_CONFIG.op["save"])
    ep.no_pull = {20: {IDLE["pull_down"]}}
    ep.reboot(0x99)
    assert state(ep)[1:4][2] == 3 and not ep.config               # reason 3: applying was refused, nothing applied


@pytest.mark.parametrize("ch", [24, 55, 0xFFFF])                  # reserved, = channels, far past
def test_pc4_the_channel_of_an_item(ch):
    ep, h = bench()
    for item in (label_item(ch, b"x"), idle_item(ch, IDLE["hi_z"]), m.tlv(ITEM["disable"], struct.pack("<H", ch))):
        r = h.raw(6, 0x02, item)
        assert (r.detail, r.payload[:1]) == (m.UNSUPPORTED, item[:1])
    assert h.raw(6, 0x02, label_item(54, b"x")).succeeded         # below channels, not reserved, declared by none


@pytest.mark.parametrize("text, ok", [(b"x" * 32, True), (b"x" * 33, False), (b"", False), (b"a\nb", False),
                                      (b"\x7f", False), (b"\xc3", False), ("nrst ✓".encode(), True)])
def test_pc5_label_text(text, ok):
    ep, h = bench()
    r = h.raw(6, 0x02, label_item(20, text))
    assert r.succeeded if ok else r.detail == m.MALFORMED


def test_pc8_a_saved_bind_on_a_port_that_is_not_a_serial_port_makes_the_save_unreadable():
    ep, h = bench()
    h.ok(6, 0x02, slot_item(0, 1, ep.pairs[1][0], attach=0, retry=0) + bind_item(0, 0, [(1, 0)]))
    h.ok(6, reg.PROBE_CONFIG.op["save"])
    ep.transports[0] = fake.TRANSPORT["vendor_bulk"]              # "another firmware": index 0 is no serial port now
    ep.serial_ports.discard(0)
    ep.reboot(0x99)
    assert state(ep)[1] == 2 and state(ep)[3] == 2                # unreadable, reason 2


def test_pc1_fake_serve_boot_reset_finds_the_firmware_nrst_label():
    from oep_client import fake_serve
    ep = fake_serve.build(fake_serve.parse(["--tcp", "0", "--profile", "esp32-v003", "--slot", "v003",
                                            "--silent-until-reset", "0", "--boot-reset"]))
    assert ep.slot_reset_log == [(0, 23, 20)]                     # describe's "NRST" (step (c))


# ---- C-07 / C-05 on fake_serve's TCP -----------------------------------------------------------------------------

def tcp_serve(*argv):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--tcp", "0", "--framing", "length",
                             *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    port = int(proc.stdout.readline().split()[1])
    return proc, port


def exchange(sock, message: bytes) -> bytes:
    sock.sendall(struct.pack("<H", len(message)) + message)
    head = b""
    while len(head) < 2:
        head += sock.recv(2 - len(head))
    n = struct.unpack("<H", head)[0]
    body = b""
    while len(body) < n:
        body += sock.recv(n - len(body))
    return body


def test_c05_c07_fake_serve_tcp_is_a_tcp_transport_and_closes_on_an_over_long_length():
    proc, port = tcp_serve("--profile", "p4-x035")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            r = m.Result.unpack(exchange(s, m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01").pack()))
            index = m.Tail.parse(r.payload[17:]).get(0x01)[0]
            r = m.Result.unpack(exchange(s, m.Request(2, 0, m.OP_DESCRIBE, struct.pack("<HH", 0, 0)).pack()))
            transports = [v for t, v in m.split_tlvs(r.payload[1:]) if t == CORE.tlv["describe"]["transport"]]
            assert bytes([index, fake.TRANSPORT["tcp"], 0xFF]) in transports and index == 4
            s.sendall(struct.pack("<H", 4000) + b"\x01" * 10)       # over max_frame 1024: the probe closes
            s.settimeout(5)
            assert s.recv(16) == b""
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_swio_swclk_other_than_0xffff_is_an_undeclared_combination_in_scan_and_attach():
    """oep-if-debug §3 (oep-spec e9cd891): a swio pair whose swclk is not 0xFFFF is unsupported, as §1 says."""
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    h = Host(ep)
    h.open()
    swio = ep.pairs[1][0][0]
    r = h.raw(1, 0x01, b"\x01" + struct.pack("<HH", swio, 5))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00" + m.tlv(0x40, b"\x00"))
    r = h.raw(1, 0x02, b"\x00" + SPEED + pins(swio, 5))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x83")
