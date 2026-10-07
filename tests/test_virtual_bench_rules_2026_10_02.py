"""The virtual bench against the rule changes of oep-spec docs/v1-rule-change-proposal-2026-10-02.md (applied 09622ef..
536fc99): each test names its item. The final text is the spec's (core, debug, fixture, capture, probe-config); the
tests follow oep-spec 0f455a0 (rule review 2026-10-07 §2): no ignored TLV, any one reason after the session check,
implemented TLVs checked alike with or without bit 7, list = first alone, the new item / drive / bind forms."""

import socket
import struct
import subprocess
import sys

import pytest

from oep_client import endpoint, virtual_bench, virtual_bench_capture, message as m, registry as reg


CORE = reg.CORE
CFG = reg.PROBE_CONFIG
ITEM, ATTACH, STREAM = CFG.tlv["item"], CFG.enum["slot_attach"], CFG.enum["bind_stream"]
SPEED = m.tlv(0x01, struct.pack("<I", 4_000_000), critical=True)    # attach's required max_speed TLV
PLAN_APPLY, PLAN_RELEASE = reg.PROBE_PLAN.op["plan_apply"], reg.PROBE_PLAN.op["plan_release"]   # oep.probe.plan


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


class Host:
    """Whole messages to an endpoint, with corr counting as a host does."""

    def __init__(self, ep, session=0x51, transport=0):
        self.ep, self.session, self.transport, self.corr = ep, session, transport, 0

    def raw(self, fn, op, payload=b"", session=True, corr=None):
        if corr is None:
            self.corr += 1
            corr = self.corr
        req = m.Request(corr, fn, op, payload, self.session if session else None)
        return m.Result.unpack(self.ep.handle(req.pack(), self.transport))

    def open(self, lease=3000, force=0, owner=None):
        """open: lease_ms(u32) force(u8) [TLV owner], the session_id in the header (core §4.1, §6.4)."""
        tail = m.tlv(CORE.tlv["open"]["owner"], owner) if owner is not None else b""
        return self.raw(0, m.OP_OPEN, struct.pack("<IB", lease, force) + tail)

    @property
    def plan_fn(self):
        """The endpoint's oep.probe.plan fn (oep-if-plan)."""
        return self.ep.fns["oep.probe.plan"]

    def ok(self, fn, op, payload=b""):
        r = self.raw(fn, op, payload)
        assert r.succeeded, r.describe()
        return r.payload


def slot_item(n, wire, pair, attach=ATTACH["at_boot"], retry=1, mech=2, name=None, max_speed=0, idle=0):
    """A slot item (probe.config §1.1): slot wire_fn swdio swclk attach retry_ms(u32) max_speed_hz(u32) idle_clock
    mechanism name_len name; `retry` in seconds here."""
    raw = (name or f"s{n}").encode()
    return m.tlv(ITEM["slot"], struct.pack("<BHHHBIIBBB", n, wire, *pair, attach, 1000 * retry, max_speed, idle, mech,
                                           len(raw)) + raw)


def bind_item(port, kind, ident):
    """A bind item (probe.config §1.2): port(u8) kind(u8) id(u16), one stream."""
    return m.tlv(ITEM["bind"], struct.pack("<BBH", port, kind, ident))


def state(ep, fn=6, first_slot=0, first_bind=0):
    """probe.config's state op (lock-free, §3.3): -> (more, storage_state, storage_hash, reason, {slot: raw slot_state},
    [raw bind_state])."""
    rd = m.Reader(Host(ep).raw(fn, CFG.op["state"], bytes([first_slot, first_bind]), session=False).payload)
    more, storage, h, why = rd.take("BBIB")
    slots = {}
    for _ in range(rd.u8()):                                        # slot(u8) state(u8) connection(u16) last_try(u64)
        raw = rd.bytes(12)
        slots[raw[0]] = raw
    binds = [rd.bytes(2) for _ in range(rd.u8())]                   # port(u8) flow(u8)
    rd.tail()
    return more, storage, h, why, slots, binds
UNA = CORE.tlv["unavailable_payload"]
IDLE = reg.PROBE_CONFIG.enum["idle_mode"]
UNKNOWN = 0x3E                                                    # a tag no context defines


def bench(profile=virtual_bench.p4_bench, **kw):
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


def idle_item(ch, mode, tag=ITEM["idle"], drive=0xFF):
    """An idle item (probe.config §1): channel(u16) mode(u8) drive(u8), 4 bytes; 0xFF = the default level."""
    return m.tlv(tag & 0x7F, struct.pack("<HBB", ch, mode, drive), critical=bool(tag & 0x80))


def label_item(ch, text: bytes, tag=ITEM["label"]):
    return m.tlv(tag & 0x7F, struct.pack("<H", ch) + text, critical=bool(tag & 0x80))


def pins(d, c, critical=True):
    return m.tlv(0x03, struct.pack("<HH", d, c), critical=critical)


# ---- C-02: a value a later revision may define is unsupported ----------------------------------------------------

def test_list_request_is_first_alone_and_its_tail_follows_the_general_tlv_rule():
    """core §7.2 (rule review 2026-10-07): list is first(u16) [TLV] - no flags, prefix or exact any more. An unknown
    non-critical TLV is ignored silently, an unknown critical one unsupported with the tag as received (§2.3)."""
    ep, h = bench()
    whole = h.raw(0, m.OP_LIST, struct.pack("<H", 0), session=False)
    assert whole.succeeded
    assert h.raw(0, m.OP_LIST, struct.pack("<H", 0) + m.tlv(0x3E, b"x"), session=False).payload == whole.payload
    r = h.raw(0, m.OP_LIST, struct.pack("<H", 0) + m.tlv(0x3E, b"x", critical=True), session=False)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\xbe")
    assert h.raw(0, m.OP_LIST, b"\x00", session=False).detail == m.MALFORMED          # shorter than first(u16)


def test_c02_attach_method_and_reset_mode_a_later_revision_may_define():
    """A value a later revision may define is unsupported (core §2.5); where the connection is also unknown, any one
    reason that applies answers (core §4.3, rule review 2026-10-07)."""
    ep, h = bench()
    r = h.raw(1, 0x02, b"\x02" + SPEED)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    reset = reg.TARGET_RISCV_DM.op["reset"]
    r = h.raw(2, reset, struct.pack("<HB", 0x777, 3))                            # no such connection, mode 3
    assert r.detail in (m.UNSUPPORTED, m.NO_CONNECTION)
    assert r.detail != m.UNSUPPORTED or r.payload == b"\x00"
    assert h.raw(2, reset, struct.pack("<HB", 0x777, 0)).detail == m.NO_CONNECTION


def test_riscv_dm_reset_has_no_method_tlv():
    """debug §4.3 (rule review 2026-10-07): reset is mode(u8) [TLV] alone - the old method tag 0x01 is an unknown TLV:
    ignored without bit 7, unsupported with the tag as received with it (core §2.3)."""
    ep, h = bench()
    a = ep.pairs[1][0]
    cid = struct.unpack_from("<H", h.ok(1, 0x02, b"\x00" + SPEED + pins(*a)))[0]
    reset = reg.TARGET_RISCV_DM.op["reset"]
    assert "reset" not in reg.TARGET_RISCV_DM.tlv
    r = h.raw(2, reset, struct.pack("<HB", cid, 0) + m.tlv(0x01, b"\x02"))
    assert r.succeeded and len(r.payload) == 6                                    # status flags pc, nothing listed
    r = h.raw(2, reset, struct.pack("<HB", cid, 0) + m.tlv(0x01, b"\x02", critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x81")


def test_c02_read_from_4_unsupported_and_from_3_with_a_wide_arg_reads_from_now():
    """common §1.2 (rule review 2026-10-07): from 4 or more is unsupported; from 3 with an arg above 0xFF is no mark
    kind there is, so it reads from now like from 2 (no malformed any more)."""
    ep, h = bench()
    read = reg.FIXTURE_UART.op["read"]
    r = h.raw(5, read, struct.pack("<BQH", 4, 0, 16), session=False)              # no plan either: any one reason
    assert r.detail in (m.UNSUPPORTED, m.UNAVAILABLE)
    assert r.detail != m.UNSUPPORTED or r.payload == b"\x00"
    assert h.raw(5, read, struct.pack("<BQH", 3, 0x100, 16), session=False).detail == m.UNAVAILABLE   # no plan
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 5, 1, 20), critical=True))
    ep.uart_rx(5, b"abc")
    r = h.raw(5, read, struct.pack("<BQH", 4, 0, 16), session=False)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    for arg in (0x100, 0xFFFF_FFFF_FFFF_FFFF):
        r = h.raw(5, read, struct.pack("<BQH", 3, arg, 16), session=False)
        assert r.succeeded and r.payload == struct.pack("<QBH", 3, 0, 0)          # from now: the write position


def gpio_drive(index, level, critical=False):
    """gpio set's TLV 0x01 drive (fixture §1.1): index(u8) level(u8), 0xFF the default level."""
    return m.tlv(reg.FIXTURE_GPIO.tlv["set"]["drive"], struct.pack("<BB", index, level), critical=critical)


@pytest.mark.parametrize("critical", [False, True])
def test_gpio_drive_is_a_level_checked_alike_with_or_without_bit_7(critical):
    """fixture §1.1 + core §2.3 (rule review 2026-10-07): drive is index(u8) level(u8); an implemented TLV is checked
    the same with or without bit 7 - a level past drive_levels is unsupported (the tag as received), another length,
    an index past n or an element that is not mode 3 / 4 malformed."""
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 20), critical=True))
    tag = reg.FIXTURE_GPIO.tlv["set"]["drive"] | (0x80 if critical else 0)
    body = bytes([1]) + struct.pack("<HB", 20, 4)
    set_ = reg.FIXTURE_GPIO.op["set"]
    r = h.raw(4, set_, body + gpio_drive(0, len(virtual_bench.DRIVE_LEVELS_MA), critical))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([tag]))
    assert h.raw(4, set_, body + m.tlv(tag & 0x7F, b"\x00\x01\x00", critical=critical)).detail == m.MALFORMED
    assert h.raw(4, set_, body + m.tlv(tag & 0x7F, b"\x00", critical=critical)).detail == m.MALFORMED
    assert h.raw(4, set_, body + gpio_drive(1, 0, critical)).detail == m.MALFORMED  # index >= n
    low_in = bytes([1]) + struct.pack("<HB", 20, 1)
    assert h.raw(4, set_, low_in + gpio_drive(0, 0, critical)).detail == m.MALFORMED   # not mode 3 / 4
    assert not ep.gpio_drive
    r = h.raw(4, set_, body + gpio_drive(0, 0xFF, critical))
    assert r.succeeded and r.payload == b"" and ep.gpio_drive[20] == virtual_bench.DRIVE_DEFAULT
    assert h.raw(4, set_, body + gpio_drive(0, 0, critical)).succeeded and ep.gpio_drive[20] == 0


def test_gpio_any_drive_on_a_probe_without_drive_levels_is_unsupported():
    """fixture §1.1: a drive on a probe that declares no drive_levels is unsupported (the tag as received), even
    0xFF."""
    ep = endpoint.Endpoint(virtual_bench.without_drive_levels(virtual_bench.p4_bench()), Clock())
    h = Host(ep)
    h.open()
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 20), critical=True))
    body = bytes([1]) + struct.pack("<HB", 20, 4)
    for critical in (False, True):
        r = h.raw(4, reg.FIXTURE_GPIO.op["set"], body + gpio_drive(0, 0xFF, critical))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, gpio_drive(0, 0xFF, critical)[:1])
    assert h.raw(4, reg.FIXTURE_GPIO.op["set"], body).succeeded


def test_c02_probe_config_enums_and_bits_a_later_revision_may_define():
    ep, h = bench()
    pair = ep.pairs[1][0]
    r = h.raw(6, 0x02, slot_item(0, 1, pair, attach=2))                # slot attach 2
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([ITEM["slot"]]))
    r = h.raw(6, 0x02, bind_item(0, 3, 0))                             # a stream kind 3
    assert (r.detail, r.payload[:1]) == (m.UNSUPPORTED, bytes([ITEM["bind"]]))
    r = h.raw(6, 0x02, idle_item(20, 5))
    assert (r.detail, r.payload[:1]) == (m.UNSUPPORTED, bytes([ITEM["idle"]]))


# ---- C-03 (as the rule review 2026-10-07 left it): repeated, short and long request TLVs -------------------------

def test_a_non_repeating_tag_twice_the_first_is_used():
    """core §2.3 (rule review 2026-10-07): a tag the definition does not repeat, twice - the reader uses the first
    (no malformed any more)."""
    ep, h = bench()
    pair = ep.pairs[1][0]
    slow = m.tlv(0x01, struct.pack("<I", 1_000_000), critical=True)
    r = h.raw(1, 0x02, b"\x00" + slow + SPEED + pins(*pair))
    assert r.succeeded
    cid, _, _, speed = struct.unpack_from("<HIBI", r.payload)
    assert speed <= 1_000_000                                       # the first max_speed holds
    once = m.tlv(0x04, b"\x01", critical=True)
    other = struct.pack("<HH", *ep.pairs[1][1])
    r = h.raw(1, 0x02, b"\x00" + SPEED + m.tlv(0x03, other, critical=True) + pins(*pair) + once + m.tlv(0x04, b"\x00"))
    assert r.succeeded and ep.conns[struct.unpack_from("<H", r.payload)[0]].idle_clock == 1
    assert ep._conn_at(1, ep.pairs[1][1]) is not None               # the first pins


@pytest.mark.parametrize("critical", [False, True])
def test_an_implemented_tlv_of_another_length_is_malformed_with_or_without_bit_7(critical):
    """core §2.3 (rule review 2026-10-07): a TLV the probe implements is checked the same with or without bit 7 - a
    value shorter or longer than its definition is malformed, and nothing runs."""
    ep, h = bench()
    pair = ep.pairs[1][0]
    assert h.raw(1, 0x02, b"\x00" + m.tlv(0x01, b"\x00\x09\x3d", critical=critical)).detail == m.MALFORMED
    long_speed = m.tlv(0x01, struct.pack("<IB", 4_000_000, 0), critical=critical)
    assert h.raw(1, 0x02, b"\x00" + long_speed + pins(*pair)).detail == m.MALFORMED
    long_pins = m.tlv(0x03, struct.pack("<HHB", *pair, 0), critical=critical)
    assert h.raw(1, 0x02, b"\x00" + SPEED + long_pins).detail == m.MALFORMED
    long_idle = m.tlv(0x04, b"\x00\x00", critical=critical)
    assert h.raw(1, 0x02, b"\x00" + SPEED + pins(*pair) + long_idle).detail == m.MALFORMED
    assert not ep.conns                                             # nothing ran


@pytest.mark.parametrize("critical", [False, True])
def test_an_implemented_tlvs_value_the_probe_does_not_handle_is_unsupported_with_or_without_bit_7(critical):
    """core §2.3: a value the definition leaves unused or this probe does not handle is unsupported with the tag as
    received, critical or not (idle_clock 2: debug §3 defines 0 and 1)."""
    ep, h = bench()
    r = h.raw(1, 0x02, b"\x00" + SPEED + pins(*ep.pairs[1][0]) + m.tlv(0x04, b"\x02", critical=critical))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([0x04 | (0x80 if critical else 0)]))
    assert not ep.conns


def test_an_unknown_tlv_is_ignored_silently_or_refused_when_critical():
    """core §2.3 (rule review 2026-10-07): no ignored TLV (0x7F) in the answer any more - an unknown non-critical TLV
    is ignored, an unknown critical one is unsupported with the tag as received."""
    ep, h = bench()
    tail = b"".join(m.tlv(0x30 + k, b"x") for k in range(20))
    r = h.raw(0, m.OP_KEEPALIVE, tail)
    assert r.succeeded and r.payload == b""
    r = h.raw(0, m.OP_KEEPALIVE, tail + m.tlv(0x3E, b"", critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\xbe")
    assert not hasattr(endpoint, "ignored_tlv") and not hasattr(m, "TAG_IGNORED")


def test_an_unknown_critical_and_a_malformed_tlv_any_one_reason():
    """core §4.3 (rule review 2026-10-07): after the session check every reason that applies may answer - an unknown
    critical TLV (unsupported) and a cut-short max_speed (malformed) in one request get either, and nothing runs."""
    ep, h = bench()
    r = h.raw(1, 0x02, b"\x00" + m.tlv(UNKNOWN, b"", critical=True) + m.tlv(0x01, b"\x00", critical=True))
    assert r.detail in (m.MALFORMED, m.UNSUPPORTED)
    assert r.detail != m.UNSUPPORTED or r.payload == bytes([UNKNOWN | 0x80])
    assert not ep.conns


def test_c03_role_assignment_and_gpio_drive_repeat():
    ep, h = bench()
    ra = lambda ch: m.tlv(0x10, struct.pack("<HBH", 4, 1, ch), critical=True)
    assert h.raw(h.plan_fn, PLAN_APPLY, ra(20) + ra(21)).succeeded
    body = bytes([2]) + struct.pack("<HBHB", 20, 4, 21, 4)
    r = h.raw(4, reg.FIXTURE_GPIO.op["set"], body + gpio_drive(0, 1) + gpio_drive(1, 3))
    assert r.succeeded and ep.gpio_drive == {20: 1, 21: 3}
    r = h.raw(4, reg.FIXTURE_GPIO.op["set"], body + gpio_drive(0, 1) + gpio_drive(0, 3))
    assert r.detail == m.MALFORMED                                  # the same index twice (fixture §1.1)


def test_an_answer_that_fills_the_frame_has_no_ignored_list():
    """core §2.3: an unknown TLV is not listed in the answer, so the answer has the whole frame for its data."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003_64(), Clock())         # 64-byte frames
    h = Host(ep)
    h.open()
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 5, 1, 21), critical=True))
    ep.uart_rx(5, bytes(range(1, 200)))
    tail = b"".join(m.tlv(0x30 + k, b"") for k in range(8))
    r = h.raw(5, reg.FIXTURE_UART.op["read"], struct.pack("<BQH", 1, 0, 1000) + tail, session=False)
    assert r.succeeded and len(r.payload) + m.RESULT_HEADER <= 64
    _, flags, n = struct.unpack_from("<QBH", r.payload)
    assert flags & 1 and r.payload[11 + n:] == b"" and n == 64 - m.RESULT_HEADER - 11


# ---- C-05 / C-15: confirm ----------------------------------------------------------------------------------------

def confirm(h, lo=1, hi=1, corr=None):
    return h.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([lo, hi]), session=False, corr=corr)


def test_c05_confirm_names_the_transport_it_came_on():
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    for index in (0, 1, 3):
        r = confirm(Host(ep, transport=index))
        assert r.succeeded and len(r.payload) == 17 + 4
        assert m.Tail.parse(r.payload[17:]).get(CORE.tlv["confirm_answer"]["transport"]) == bytes([index])
        assert ep.revision_in_use[index] == 1


def test_c15_no_revision_in_range_carries_the_supported_range_and_min_above_max_is_malformed():
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    h = Host(ep)
    r = confirm(h, 2, 3)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00" + m.tlv(0x01, b"\x01\x01"))
    assert confirm(h, 2, 1).detail == m.MALFORMED


# ---- C-10: a probe without plan roles has no oep.probe.plan ---------------------------------------------------------

def test_c10_a_probe_without_plan_roles_lists_no_plan():
    """oep-if-plan: a probe lists oep.probe.plan exactly when an interface has plan roles; fn 0's old plan_release
    number (0x05) is no core op (unknown_operation)."""
    keep = (virtual_bench.CORE_NAME, "oep.wire.rvswd", "oep.target.riscv-dm")
    probe = virtual_bench.VirtualProbe("bare", 256, [virtual_bench.Offered(o.fn, o.instance, o.name, tuple(t for t in o.tlvs if t[0] != 0x09))
                                         for o in virtual_bench.p4_bench().offered if o.name in keep])   # ops made anew
    ep = endpoint.Endpoint(probe, Clock())
    assert "oep.probe.plan" not in ep.fns and ep.ops[0] == set(reg.CORE.op.values())
    h = Host(ep)
    h.open()
    assert h.raw(0, 0x05, b"\x00").detail == m.UNKNOWN_OPERATION
    assert "oep.probe.plan" in endpoint.Endpoint(virtual_bench.p4_bench(), Clock()).fns   # gpio, uart: plan roles


# ---- C-16 / C-17 / C-18 / C-22: the session ----------------------------------------------------------------------

def test_c16_a_rejected_answer_is_remembered_and_a_resend_replays_it():
    ep, h = bench()
    body = struct.pack("<HBH", 4, 1, 3)                           # channel 3: not the gpio's
    first = h.raw(h.plan_fn, PLAN_APPLY, m.tlv(0x10, body, critical=True), corr=50)
    assert first.detail == m.UNSUPPORTED
    again = h.raw(h.plan_fn, PLAN_APPLY, m.tlv(0x10, body, critical=True), corr=50)
    assert again == first and ep.newest_corr == 50                # from the table, not run again
    assert h.raw(0x77, 0x01, b"", corr=51).detail == m.UNKNOWN_FUNCTION   # order 1: not in the table
    assert 51 not in ep.resend


def test_c17_the_lease_is_rounded_into_1000_to_60000_and_restarts_on_rejected_answers():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    h = Host(ep)
    assert struct.unpack_from("<I", h.open(lease=1).payload)[0] == 1000
    assert struct.unpack_from("<I", h.open(lease=100_000).payload)[0] == 60000
    h.open(lease=1000)
    ep.now = lambda: 900
    assert h.raw(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 3), critical=True)).detail == m.UNSUPPORTED
    assert ep.expires_ms == 1900                                  # restarted by a rejected answer of the holder
    ep.now = lambda: 1000
    h.raw(0x77, 0x01, b"")                                        # unknown_function (order 1): no restart
    assert ep.expires_ms == 1900


def test_c18_session_id_0_is_malformed():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    h = Host(ep, session=0)
    assert h.open().detail == m.MALFORMED and ep.holder is None


@pytest.mark.parametrize("force", [1, 2, 0xFF])
def test_a_boolean_in_a_request_is_true_when_not_zero(force):
    """core §2.1 (rule review 2026-10-07): the reader takes every non-zero boolean as true - open's force 2 takes the
    lock like force 1, it is not malformed."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    assert Host(ep, session=0x99).open().succeeded
    r = Host(ep).open(force=force)
    assert r.succeeded and ep.holder == 0x51


@pytest.mark.parametrize("owner, ok", [(b"bad\x1b[31m", True), (b"\xff\xfe", True), (b"x" * 32, True),
                                       (b"x" * 33, False), (b"", False)])
def test_request_text_is_not_validated_but_owner_keeps_its_length(owner, ok):
    """core §2.1 / §6.4 (rule review 2026-10-07): a probe does not check request text (the host replaces what it
    shows); owner keeps its 1-32 byte length rule."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    r = Host(ep).open(owner=owner)
    if ok:
        assert r.succeeded and ep.owner == owner
    else:
        assert r.detail == m.MALFORMED and ep.holder is None


def test_c22_a_utf8_owner_is_taken():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    h = Host(ep)
    assert h.open(owner="日本 bench".encode()).succeeded and ep.owner == "日本 bench".encode()


# ---- C-23 / C-30 / C-24: names, instances, unit_id --------------------------------------------------------------

def test_c30_every_profile_numbers_its_instances_per_name_and_revision():
    for name, profile in virtual_bench.PROFILES.items():
        assert profile().instance_errors() == [], name
    bad = virtual_bench.VirtualProbe("x", 256, [virtual_bench.Offered(0, 0, virtual_bench.CORE_NAME), virtual_bench.Offered(1, 1, "oep.fixture.gpio")])
    assert bad.instance_errors()


def test_c24_a_profile_with_an_x_unit_id():
    probe = virtual_bench.with_unit_id(virtual_bench.esp32_v003(), "x-esp32")
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


def test_p2_1_a_named_output_idle_channel_is_unavailable_cause_5_and_the_channel():
    """debug §1; the unavailable payload is cause, channel, fn only (core §4.3, rule review 2026-10-07)."""
    ep, h = bench()
    a = ep.pairs[1][0]
    h.ok(6, 0x02, idle_item(a[1], IDLE["output_high"]))
    for r in (h.raw(1, 0x01, b"\x01" + struct.pack("<HH", *a)), h.raw(1, 0x02, b"\x00" + SPEED + pins(*a))):
        t = una(r)
        assert r.detail == m.UNAVAILABLE
        assert t == {UNA["cause"]: b"\x05", UNA["channel"]: struct.pack("<H", a[1])}
    assert not ep.conns


def test_p2_1_an_attach_without_pins_has_no_idle_item_candidate():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())            # one SWIO pair
    h = Host(ep)
    h.open()
    swio = ep.pairs[1][0][0]
    h.ok(9, 0x02, idle_item(swio, IDLE["pull_up"]))
    r = h.raw(1, 0x02, b"\x00" + SPEED)
    assert r.detail == m.UNAVAILABLE and una(r) == {UNA["cause"]: b"\x05", UNA["channel"]: struct.pack("<H", swio)}
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
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 8), critical=True))
    rp = endpoint.Endpoint(virtual_bench.rp2350_pins(), Clock())
    hp = Host(rp)
    hp.open()
    hp.ok(hp.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 1), critical=True))
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
    assert status == endpoint.STATE and (moved, before, after) == (0, 0, 0)   # not ok: all 0 (debug §4.2, a168c34)
    assert tg.halted and not tg.dcsr_step and tg.dpc != 0                  # halted again; its dpc is read with dmi
    assert m.Tail.parse(r.payload[10:]).get(0x01) is None


def test_p2_4_the_attach_answer_carries_search_retries():
    ep, h = bench()
    a = ep.pairs[1][0]
    ep._target(1, a).search_retries = 70000
    r = h.ok(1, 0x02, b"\x00" + SPEED + pins(*a))
    assert m.Tail.parse(r[11:]).get(reg.WIRE_RVSWD.tlv["attach_answer"]["search_retries"]) == b"\xff\xff"


# ---- fixture: P2-★2 / ★3 / ○13 ---------------------------------------------------------------------------------------

def x035():
    ep = endpoint.Endpoint(virtual_bench.with_i2c_pullups(virtual_bench.p4_x035()), Clock())
    h = Host(ep)
    h.open()
    return ep, h


def test_p2_2_spi_target_drives_miso_only_while_cs_is_active():
    ep, h = x035()
    spi = 9
    plan = b"".join(m.tlv(0x10, struct.pack("<HBH", spi, role, ch), critical=True)
                    for role, ch in ((1, 30), (2, 31), (3, 32), (4, 33)))
    h.ok(6 + 4, 0x02, idle_item(32, IDLE["pull_up"]))             # MISO's idle: a pull-up input
    h.ok(h.plan_fn, PLAN_APPLY, plan)
    assert ep.pin_state(32) == "idle pull-up"                     # before configure: the idle state
    h.ok(spi, reg.FIXTURE_SPI_TARGET.op["configure"], b"\x00\x00")
    assert ep.pin_state(32) == "miso-hi-z" and ep.pin_state(30) == "input" and ep.pin_state(33) == "input"
    ep.spi_select(spi, True)
    assert ep.pin_state(32) == "miso-driven"
    ep.spi_select(spi, False)
    assert ep.pin_state(32) == "miso-hi-z"


def test_p2_3_i2c_target_open_drain_and_declared_pullups():
    """fixture §3 (rule review 2026-10-07): configure is address(u8) alone; a probe with pull-ups of its own declares
    features bit2 (no pullup_ohms any more)."""
    ep, h = x035()
    i2c = 8
    r = h.raw(0, m.OP_DESCRIBE, struct.pack("<HH", i2c, 0), session=False)
    d = dict(m.split_tlvs(r.payload[1:]))
    assert struct.unpack("<I", d[0x06])[0] & virtual_bench.I2C_INTERNAL_PULLUPS == 0x04 and 0x42 not in d
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", i2c, 1, 30), critical=True)
         + m.tlv(0x10, struct.pack("<HBH", i2c, 2, 31), critical=True))
    assert ep.pin_state(30) == "idle hi-z"                        # state 0: released, nothing ACKed
    h.ok(i2c, reg.FIXTURE_I2C_TARGET.op["configure"], b"\x42")
    assert ep.pin_state(30) == ep.pin_state(31) == "open-drain pull-up"
    assert h.raw(i2c, reg.FIXTURE_I2C_TARGET.op["configure"], b"\x42\x01").detail == m.MALFORMED   # no mode: a broken tail
    plain = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    d = dict(m.split_tlvs(Host(plain).raw(0, m.OP_DESCRIBE, struct.pack("<HH", i2c, 0), session=False).payload[1:]))
    assert not struct.unpack("<I", d[0x06])[0] & virtual_bench.I2C_INTERNAL_PULLUPS   # the profiles declare none


def test_p2_o13_taking_a_plan_changes_no_pin_and_an_analog_plan_on_an_output_idle_is_refused():
    ep, h = x035()
    h.ok(10, 0x02, idle_item(16, IDLE["output_high"]) + idle_item(20, IDLE["output_low"]) + idle_item(21, IDLE["output_high"]))
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 4, 1, 20), critical=True)
         + m.tlv(0x10, struct.pack("<HBH", 7, 0, 21), critical=True))
    assert ep.pin_state(20) == "gpio 3" and ep.gpio_log == []     # gpio: the idle's output low kept, no set yet
    assert ep.pin_state(21) == "idle output-high"                 # logic: it only listens
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 5, 2, 22), critical=True))
    assert ep.pin_state(22) == "uart-tx-high"                     # uart TX: from the plan
    r = h.raw(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 11, 0, 16), critical=True))
    t = una(r)
    assert r.detail == m.UNAVAILABLE and t == {UNA["cause"]: b"\x05", UNA["channel"]: struct.pack("<H", 16)}


# ---- capture: P2-○8 / ○10 / ○11 ----------------------------------------------------------------------------------------

CAP_TLV = virtual_bench_capture.TLV


def test_p2_o8_samples_rounded_down_and_the_configure_tlvs():
    """capture §3.3 (rule review 2026-10-07): configure's TLVs follow core §2.3 alone - an unhandled mode / rate /
    trigger is unsupported with the tag as received, critical or not."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())            # logic: max_samples 65536, one-shot only
    h = Host(ep)
    h.open()
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 6, 0, 4), critical=True))
    cfg = reg.FIXTURE_LOGIC.op["configure"]
    rate = m.tlv(CAP_TLV["rate"], struct.pack("<I", 1_000_000), critical=True)
    one_shot = m.tlv(CAP_TLV["mode"], b"\x01") + m.tlv(CAP_TLV["samples"], struct.pack("<I", 100))   # §3.3: required
    r = h.ok(6, cfg, rate + m.tlv(CAP_TLV["mode"], b"\x01") + m.tlv(CAP_TLV["samples"], struct.pack("<I", 1 << 20)))
    assert m.Tail.parse(r).get(reg.FIXTURE_ANALOG.tlv["configure_answer"]["actual_samples"]) == struct.pack("<I", 65536)
    r = h.raw(6, cfg, rate + m.tlv(CAP_TLV["mode"], b"\x03", critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["mode"] | 0x80]))
    r = h.raw(6, cfg, rate + m.tlv(CAP_TLV["mode"], b"\x07"))     # undefined, not critical: the same (core §2.3)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["mode"]]))
    r = h.raw(6, cfg, rate + m.tlv(CAP_TLV["mode"], b"\x03"))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["mode"]]))
    r = h.raw(6, cfg, m.tlv(CAP_TLV["rate"], struct.pack("<I", 9_000_000), critical=True) + one_shot)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["rate"] | 0x80]))
    r = h.raw(6, cfg, rate + one_shot + m.tlv(CAP_TLV["trigger"], struct.pack("<BBI", 3, 0, 0), critical=True))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([CAP_TLV["trigger"] | 0x80]))   # cross up: the analog's


def test_p2_o10_capture_group_with_nothing_bound_and_the_checks_before_start():
    ep, h = x035()
    grp = reg.FIXTURE_CAPTURE_GROUP.op
    r = h.raw(12, grp["start"])
    assert r.detail == m.UNAVAILABLE and una(r)[UNA["cause"]] == b"\x06"
    assert h.raw(12, grp["stop"]).succeeded and h.raw(12, grp["force"]).succeeded
    assert h.ok(12, grp["status"])[0] == 0
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 7, 0, 30), critical=True)
         + m.tlv(0x10, struct.pack("<HBH", 11, 0, 17), critical=True))
    for fn, hz in ((7, 1_000_000), (11, 1000)):
        h.ok(fn, reg.FIXTURE_LOGIC.op["configure"], m.tlv(CAP_TLV["mode"], b"\x03", critical=True)
             + m.tlv(CAP_TLV["rate"], struct.pack("<I", hz), critical=True))
    h.ok(12, grp["bind"], struct.pack("<BHH", 2, 7, 11))
    h.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))              # fn 11 has no subscription
    r = h.raw(12, grp["start"])
    assert r.detail == m.UNAVAILABLE and una(r)[UNA["fn"]] == struct.pack("<H", 11)
    assert ep.captures[7].state == virtual_bench_capture.STATE["configured"]   # nothing started


def test_p2_o11_a_read_past_the_write_position():
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 5, 1, 20), critical=True))
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


@pytest.mark.parametrize("ch", [24, 55, 0xFFFF])                  # the probe's own, = channels, far past
def test_pc4_the_channel_of_an_item(ch):
    ep, h = bench()
    for item in (label_item(ch, b"x"), idle_item(ch, IDLE["hi_z"]), m.tlv(ITEM["disable"], struct.pack("<H", ch))):
        r = h.raw(6, 0x02, item)
        assert (r.detail, r.payload[:1]) == (m.UNSUPPORTED, item[:1])
    assert h.raw(6, 0x02, label_item(54, b"x")).succeeded         # below channels, not its own, declared by none


@pytest.mark.parametrize("text, ok", [(b"x" * 32, True), (b"x" * 33, False), (b"", False), (b"a\nb", True),
                                      (b"\x7f", True), (b"\xc3", True), ("nrst ✓".encode(), True)])
def test_pc5_label_text(text, ok):
    """probe.config §1 (rule review 2026-10-07): a label's text is checked for its length (1-32) only - request text
    is not validated (core §2.1); the host replaces what it shows."""
    ep, h = bench()
    r = h.raw(6, 0x02, label_item(20, text))
    assert r.succeeded if ok else r.detail == m.MALFORMED


def test_pc8_a_saved_bind_on_a_port_that_is_not_a_serial_port_makes_the_save_unreadable():
    ep, h = bench()
    h.ok(6, 0x02, slot_item(0, 1, ep.pairs[1][0], attach=0, retry=0) + bind_item(0, STREAM["slot_console"], 0))
    h.ok(6, reg.PROBE_CONFIG.op["save"])
    ep.transports[0] = virtual_bench.TRANSPORT["vendor_bulk"]              # "another firmware": index 0 is no serial port now
    ep.serial_ports.discard(0)
    ep.reboot(0x99)
    assert state(ep)[1] == 2 and state(ep)[3] == 2                # unreadable, reason 2


def test_virtual_bench_serve_has_no_boot_reset_any_more():
    """probe.config (rule review 2026-10-07): no boot_reset / retry with reset - virtual_bench_serve's --boot-reset is gone."""
    from oep_client import virtual_bench_serve
    with pytest.raises(SystemExit):
        virtual_bench_serve.parse(["--tcp", "0", "--profile", "esp32-v003", "--slot", "v003", "--boot-reset"])
    ep = virtual_bench_serve.build(virtual_bench_serve.parse(["--tcp", "0", "--profile", "esp32-v003", "--slot", "v003"]))
    assert not hasattr(ep, "slot_reset_log")


# ---- C-07 / C-05 on virtual_bench_serve's TCP -----------------------------------------------------------------------------

def tcp_serve(*argv):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length",
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


def test_c05_c07_virtual_bench_serve_tcp_is_a_tcp_transport_and_closes_on_an_over_long_length():
    proc, port = tcp_serve("--profile", "p4-x035")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            r = m.Result.unpack(exchange(s, m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01").pack()))
            index = m.Tail.parse(r.payload[17:]).get(0x01)[0]
            r = m.Result.unpack(exchange(s, m.Request(2, 0, m.OP_DESCRIBE, struct.pack("<HH", 0, 0)).pack()))
            transports = [v for t, v in m.split_tlvs(r.payload[1:]) if t == CORE.tlv["describe"]["transport"]]
            assert bytes([index, virtual_bench.TRANSPORT["tcp"], 0xFF]) in transports and index == 4
            s.sendall(struct.pack("<H", 4000) + b"\x01" * 10)       # over max_frame 1024: the probe closes
            s.settimeout(5)
            assert s.recv(16) == b""
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_swio_swclk_other_than_0xffff_is_an_undeclared_combination_in_scan_and_attach():
    """oep-if-debug §3 (oep-spec e9cd891): a swio pair whose swclk is not 0xFFFF is unsupported, as §1 says."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    h = Host(ep)
    h.open()
    swio = ep.pairs[1][0][0]
    r = h.raw(1, 0x01, b"\x01" + struct.pack("<HH", swio, 5))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00" + m.tlv(0x40, b"\x00"))
    r = h.raw(1, 0x02, b"\x00" + SPEED + pins(swio, 5))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x83")
