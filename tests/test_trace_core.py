"""Rule-by-rule trace of oep-spec a09fa45 docs/oep-core.ja.md (core): a test for each unit no other test checks on its
own, each naming its section ("core §x.y"). The virtual bench is checked directly - endpoint.Endpoint on a full profile
and, where the rule is fn 0's, also the core-v1 profile (virtual_core.Core) - and so is the client (message, host,
core, link). A *_mutant test breaks the bench or the client the way the rule forbids and shows the same check fails."""

import random
import struct

import pytest

from oep_client import capture as cap, catalog, core, endpoint, host as h, link, message as m, names, registry as reg
from oep_client import virtual_bench as vb

CORE = reg.CORE
OWNER = CORE.tlv["open"]["owner"]
UNA = CORE.tlv["unavailable_payload"]
CAUSE = CORE.enum["unavailable_cause"]
PLAN = reg.PROBE_PLAN
CFG = reg.PROBE_CONFIG
GPIO = reg.FIXTURE_GPIO
RVSWD = reg.WIRE_RVSWD
RV = reg.TARGET_RISCV_DM
SID = 0x51


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


def build(profile=vb.p4_x035, **kw):
    """An endpoint on `profile` with a clock a test moves (ms)."""
    return endpoint.Endpoint(profile(), Clock(), **kw)


PROFILES = {"full": vb.p4_x035, "core-v1": vb.core_v1}


@pytest.fixture(params=sorted(PROFILES))
def ep(request):
    """fn 0 on both implementations: the full bench (endpoint.Endpoint) and the core-v1 profile (virtual_core.Core)."""
    return build(PROFILES[request.param])


class Wire:
    """Whole request messages to an endpoint, a corr per request as a host does (core §4.1)."""

    def __init__(self, ep, sid=SID, transport=0):
        self.ep, self.sid, self.transport, self.corr = ep, sid, transport, 0

    def bytes(self, fn, op, payload=b"", sid=None, corr=None):
        if corr is None:
            self.corr += 1
            corr = self.corr
        return m.Request(corr, fn, op, payload, self.sid if sid is None else sid).pack()

    def send(self, data, transport=None):
        out = self.ep.handle(data, self.transport if transport is None else transport)
        return None if out is None else m.Result.unpack(out)

    def raw(self, fn, op, payload=b"", sid=None, corr=None, transport=None):
        return self.send(self.bytes(fn, op, payload, sid, corr), transport)

    def open(self, lease=3000, force=0, tail=b"", sid=None):
        return self.raw(0, m.OP_OPEN, struct.pack("<IB", lease, force) + tail, sid)

    def ok(self, fn, op, payload=b"", sid=None):
        r = self.raw(fn, op, payload, sid)
        assert r.succeeded, r.describe()
        return r.payload

    def lock_state(self):
        r = self.raw(0, m.OP_LOCK_STATE, sid=0)
        locked, remaining = struct.unpack_from("<BI", r.payload)
        return locked, remaining, m.Tail.parse(r.payload[5:]).get(OWNER)


def rejected(r, reason):
    assert r is not None and (r.resolution, r.detail) == (m.REJECTED, reason), r and r.describe()
    return r


def describe(ep, fn=0, transport=0):
    """Every describe TLV of `fn`, paged by first (core §7.3)."""
    w, out = Wire(ep, transport=transport), []
    while True:
        r = w.raw(0, m.OP_DESCRIBE, struct.pack("<HH", fn, len(out)), sid=0)
        assert r.succeeded, r.describe()
        page = m.split_tlvs(r.payload[1:])
        out += page
        if not r.payload[0]:
            return out


def entries(ep):
    """Every list entry, paged by first (core §7.2) -> (total, entries)."""
    w, out = Wire(ep), []
    while True:
        total, page = catalog.unpack_list_result(w.ok(0, m.OP_LIST, struct.pack("<H", len(out)), sid=0))
        out += page
        if not page or len(out) >= total:
            return total, out


def host_on(ep, **kw):
    return h.Host(lambda b: ep.handle(b, 0), rng=random.Random(7), **kw)


def scripted(*answers):
    """A send that answers each request with the next of `answers` (bytes after the header, or a callable on the
    request) under its own corr; the requests it saw in `.sent`."""
    queue = list(answers)

    def send(data):
        send.sent.append(data)
        a = queue.pop(0)
        body = a(data) if callable(a) else a
        return data[:1].replace(b"\x01", b"\x02") + data[1:3] + body
    send.sent = []
    return send


def completed(payload=b"", outcome=m.SUCCESS):
    return bytes([m.COMPLETED, outcome]) + payload


def refused(reason, payload=b""):
    return bytes([m.REJECTED, reason]) + payload


def assign(fn, role, ch, critical=False):
    return m.tlv(PLAN.tlv["plan_apply"]["role_assignment"], struct.pack("<HBH", fn, role, ch), critical=critical)


def tags(payload):
    return [t for t, _ in m.split_tlvs(payload)]


# ---- §0, §1.2 --------------------------------------------------------------------------------------------------------

def test_core_is_fn_0_has_no_name_and_is_never_listed(ep):
    """core §0, §7.2: the core speaks on fn 0, has no name and no list entry; its version is confirm's revision."""
    total, listed = entries(ep)
    assert all(e.fn != m.CORE_FN for e in listed) and total == len(listed)
    w = Wire(ep)
    r = w.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([1, 1]), sid=0)
    assert r.payload[:5] == m.CONFIRM_RESULT + bytes([reg.PROTOCOL_REVISION])
    assert describe(ep, 0)                                                    # fn 0 answers by number, not by name


def test_interfaces_are_named_by_reverse_dns_or_the_oep_prefix_with_their_own_revision():
    """core §0, §12 rule 1: an interface has a reverse DNS name or the project's `oep.<layer>.<name>`, labels of
    a-z 0-9 - (not at either end), two labels or more, 1 to 48 bytes; each list entry carries its own revision."""
    ep = build(lambda: vb.with_stand_in(vb.p4_x035()))
    _, listed = entries(ep)
    assert any(e.name.startswith("io.github.") for e in listed)
    for e in listed:
        names.validate(e.name)
        assert e.revision >= 1
        if e.name.startswith("oep."):
            assert e.name.split(".")[1] in ("probe", "wire", "target", "fixture") and len(e.name.split(".")) >= 3
    for bad in ("Oep.probe.x", "oep", "a.-b", "a.b-", "x" * 47 + ".y"):
        with pytest.raises(names.InvalidName):
            names.validate(bad)


def test_fn_0_offers_its_eight_ops_and_answers_each(ep):
    """core §1.2: fn 0's confirm, list, describe, clock, open, end, keepalive and lock_state are all there - in its ops
    tag and answered (none is unknown_operation)."""
    ops = next(catalog.unpack_ops(v) for t, v in describe(ep) if t == catalog.OPS)
    assert ops == set(CORE.op.values())
    w = Wire(ep)
    bodies = {m.OP_CONFIRM: m.CONFIRM_REQUEST + b"\x01\x01", m.OP_LIST: b"\0\0", m.OP_DESCRIBE: b"\0\0\0\0",
              m.OP_CLOCK: b"", m.OP_LOCK_STATE: b"", m.OP_OPEN: struct.pack("<IB", 3000, 0)}
    for op, body in bodies.items():
        assert w.raw(0, op, body, sid=SID if op == m.OP_OPEN else 0).succeeded, op
    for op in (m.OP_KEEPALIVE, m.OP_END):
        assert w.raw(0, op).succeeded, op


def test_fn_0_declares_unit_id_transport_and_max_op_ms(ep):
    """core §1.2, §7.5: fn 0's describe carries unit_id, transport and max_op_ms (all required)."""
    got = {t for t, _ in describe(ep)}
    d = CORE.tlv["describe"]
    assert {d["unit_id"], d["transport"], d["max_op_ms"]} <= got


def test_every_listed_fn_and_fn_0_declare_their_ops():
    """core §1.2, §7.4: the common tag ops is in fn 0's describe and in every listed fn's."""
    for profile in (vb.p4_x035, vb.p4_bench, vb.esp32_v003, vb.rp2350_pins):
        ep = build(profile)
        for fn in [0] + [e.fn for e in entries(ep)[1]]:
            assert any(t == catalog.OPS for t, _ in describe(ep, fn)), (profile.__name__, fn)


def test_a_probe_with_channels_declares_them_and_parks_them_idle():
    """core §1.2, §7.5, §8: a probe with channels declares channels in fn 0's describe, and every channel not its own
    is in its idle state (Hi-Z without settings) before the first request."""
    ep = build()
    channels = next(struct.unpack("<H", v)[0] for t, v in describe(ep) if t == CORE.tlv["describe"]["channels"])
    assert channels == 55
    for ch in range(channels):
        if ch not in ep.own_channels:
            assert ep.pin_state(ch) == "idle hi-z", ch


def test_a_probe_declares_a_transport_and_speaks_its_frames():
    """core §1.2, §3: at least one transport (fn 0's transport TLV) and its frames: the core's confirm in a serial
    frame (COBS + CRC) from the virtual bench's serial side round-trips."""
    from oep_client import cobs
    ep = build()
    rows = [v for t, v in describe(ep) if t == CORE.tlv["describe"]["transport"]]
    assert rows and all(len(v) == 3 for v in rows)
    req = m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01").pack()
    framed = cobs.frame(req)
    assert cobs.unframe(framed[1:-1]) == req
    out = ep.handle(cobs.unframe(framed[1:-1]), 0)
    assert cobs.unframe(cobs.frame(out)[1:-1])[5:9] == m.CONFIRM_RESULT


def test_every_fn_that_sends_notifications_has_subscribe_and_unsubscribe():
    """core §1.2, §10.3: an interface that sends notifications sets both subscribe and unsubscribe in its ops."""
    ep = build()
    senders = [fn for fn, ops in ep.ops.items() if m.OP_SUBSCRIBE in ops or m.OP_UNSUBSCRIBE in ops]
    assert senders and all({m.OP_SUBSCRIBE, m.OP_UNSUBSCRIBE} <= ep.ops[fn] for fn in senders)
    for i in reg.INTERFACES.values():
        assert ("subscribe" in i.op) == ("unsubscribe" in i.op) == bool(i.event) or not i.event, i.name


# ---- §2.1 ------------------------------------------------------------------------------------------------------------

def test_a_boolean_is_sent_0_or_1_and_any_non_zero_reads_true(ep):
    """core §2.1: open's force read as true for any non-zero byte (the probe), sent as 0 / 1 (the host); lock_state's
    locked read as true for any non-zero byte (the host)."""
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    assert a.open().succeeded
    assert b.open(force=2).succeeded and ep_holder(ep) == 0x20
    hst = h.Host(scripted(completed(struct.pack("<II", 3000, 1))), rng=random.Random(1))
    hst.revision, hst._describes[0] = 1, [(CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 1000))]
    hst.open(force=True)
    assert hst.send.sent[-1][m.REQUEST_HEADER + 4] == 1
    hst = h.Host(scripted(completed(struct.pack("<BI", 7, 100))))
    assert hst.lock_state() == (True, 100)


def ep_holder(ep):
    return ep.current_core.holder if ep.probe.core_contract else ep.holder


def test_text_is_shown_with_control_characters_and_bad_utf8_replaced():
    """core §2.1: before showing an answer's text the host replaces C0 controls, 0x7F and invalid UTF-8."""
    assert m.shown(b"a\x1b[2Jb\x7f\xff\x00") == "a�[2Jb���"
    owner = m.tlv(OWNER, b"x\x07y")
    hst = h.Host(scripted(completed(struct.pack("<BI", 1, 5) + owner)))
    assert hst.lock_owner() == (True, 5, "x�y")
    assert h.Locked(m.Result(1, m.REJECTED, m.LOCKED, struct.pack("<I", 9) + owner)).owner == "x�y"


def test_text_shown_mutant():
    """core §2.1 mutant: a host that shows the raw text lets a control character through - the check above fails."""
    raw = b"a\x1bb"
    assert "\x1b" in raw.decode() and "\x1b" not in m.shown(raw)


# ---- §2.2 TLV --------------------------------------------------------------------------------------------------------

def test_one_tlv_form_and_a_length_past_the_end(ep):
    """core §2.2: one TLV form whatever the length (0 to 65535); a TLV whose len runs past its message: malformed in a
    request, a broken answer for the host."""
    for value in (b"", b"x" * 255, b"y" * 300):
        assert m.split_tlvs(m.tlv(0x3D, value)) == [(0x3D, value)]
    w = Wire(ep)
    rejected(w.raw(0, m.OP_CLOCK, b"\x3d\x05\x00ab", sid=0), m.MALFORMED)
    rejected(w.raw(0, m.OP_LIST, b"\0\0\x3d\x02\x00a", sid=0), m.MALFORMED)
    with pytest.raises(m.ShortPayload):
        m.Tail.parse(b"\x3d\x05\x00ab")
    hst = h.Host(scripted(completed(struct.pack("<IQ", 1, 2) + b"\x3d\x05\x00ab")))
    with pytest.raises(m.ShortPayload):
        hst.clock()


def test_bit_7_is_the_critical_mark_in_requests_and_an_unknown_tag_in_answers(ep):
    """core §2.2: the tag number is the low 7 bits; in a request bit 7 marks critical (0x81 is open's owner with the
    mark); in an answer a TLV with bit 7 set is skipped as an unknown tag."""
    w = Wire(ep)
    assert w.open(tail=m.tlv(OWNER, b"me", critical=True)).succeeded
    assert w.lock_state()[2] == b"me"
    p = struct.pack("<BI", 1, 5) + m.tlv(OWNER | 0x80, b"alias")
    assert h.Host(scripted(completed(p))).lock_owner() == (True, 5, None)
    una = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x81, b"\x01") + m.tlv(0x82, b"\x05\x00")))
    assert una.cause is None and una.channels == []
    d = catalog.decode_description(m.tlv(catalog.FEATURES | 0x80, b"\x01\0\0\0") + m.tlv(catalog.OPS | 0x80, b"\x10\x01"))
    assert d.features is None and d.ops is None and not d.specific
    hst = host_on(build())
    hst._describes[0] = [(CORE.tlv["describe"]["max_op_ms"] | 0x80, struct.pack("<I", 1)),
                         (CORE.tlv["describe"]["transport"] | 0x80, b"\x00\x01\xff")]
    assert core.max_op_ms(hst) == core.FALLBACK_MAX_OP_MS and core.transports(hst) == []


def test_tlv_number_0_is_never_a_tlv(ep):
    """core §2.2: TLV number 0, with or without bit 7, is malformed in a request and a broken TLV in an answer;
    unsupported's fixed-part mark 0x00 is not a TLV."""
    w = Wire(ep)
    for zero in (b"\x00\x00\x00", b"\x80\x00\x00"):
        rejected(w.raw(0, m.OP_CLOCK, zero, sid=0), m.MALFORMED)
    with pytest.raises(m.ProtocolError):
        m.Tail.parse(b"\x80\x00\x00")
    u = h.Unsupported(m.Result(1, m.REJECTED, m.UNSUPPORTED, b"\x00" + m.tlv(0x02, b"\x05\x00")))
    assert u.tag is None and u.tlvs == [(0x02, b"\x05\x00")]


def test_unsupported_names_the_tag_as_received(ep):
    """core §2.2, §2.3, §4.3: an unknown critical TLV is refused unsupported with its tag byte as received (bit 7 too);
    an unknown non-critical one is ignored."""
    w = Wire(ep)
    r = rejected(w.raw(0, m.OP_CLOCK, m.tlv(0x45, b"z", critical=True), sid=0), m.UNSUPPORTED)
    assert r.payload == b"\xc5"
    assert w.raw(0, m.OP_CLOCK, m.tlv(0x45, b"z"), sid=0).succeeded
    assert h.Unsupported(r).tag == 0xC5


def test_unsupported_tag_mutant(monkeypatch):
    """core §2.2 mutant: a probe that names the tag without bit 7 is caught by the check above."""
    orig = endpoint.Take.check

    def stripped(self):
        if self.unknown_critical is not None:
            raise endpoint.Reject(m.UNSUPPORTED, bytes([self.unknown_critical & 0x7F]))
        orig(self)
    monkeypatch.setattr(endpoint.Take, "check", stripped)
    with pytest.raises(AssertionError):
        test_unsupported_names_the_tag_as_received(build())


def test_the_tag_space_is_the_payloads_context(ep):
    """core §2.2, §2.5: the same number means different things in different contexts - 0x01 is open's owner (1-32
    bytes: 40 is malformed), nothing on clock (ignored), confirm's transport in its answer, cause in unavailable,
    supported in confirm's unsupported."""
    w = Wire(ep)
    assert w.raw(0, m.OP_CLOCK, m.tlv(0x01, b"x" * 40), sid=0).succeeded
    rejected(w.open(tail=m.tlv(0x01, b"x" * 40)), m.MALFORMED)
    r = w.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)
    assert m.Tail.parse(r.payload[16:]).get(0x01) == b"\x00"
    r = rejected(w.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x02\x03", sid=0), m.UNSUPPORTED)
    assert h.Unsupported(r).supported == (1, 1)
    assert CORE.tlv["unavailable_payload"]["cause"] == CORE.tlv["unsupported_payload"]["supported"] == OWNER == 0x01


def test_a_tag_repeats_only_where_its_definition_says():
    """core §2.2, §2.3: a repeating tag is a list (unavailable's channel, fn 0's label: all kept in order); a tag that
    does not repeat is used once - the first."""
    una = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x01, b"\x01") + m.tlv(0x01, b"\x02")
                                 + m.tlv(0x02, b"\x05\x00") + m.tlv(0x02, b"\x07\x00")))
    assert una.cause == "pin_in_use" and una.channels == [5, 7]
    ep = build()
    labels = core.firmware_labels(host_on(ep))
    assert labels == [(2, "SWDIO"), (54, "SWCLK"), (51, "LED")]


# ---- §2.3 fixed forms and TLVs ---------------------------------------------------------------------------------------

def test_containers_know_their_length(ep):
    """core §2.3: every container ends where its own count or length says - list's entries (count, name_len), describe's
    TLVs, clock's fixed part - read with no other knowledge, nothing left over."""
    w = Wire(ep)
    p = w.ok(0, m.OP_LIST, b"\0\0", sid=0)
    rd = m.Reader(p)
    total, count = rd.take("HB")
    for _ in range(count):
        rd.take("HHB")
        rd.bytes(rd.u8())
    assert rd.tail().tlvs == []
    p = w.ok(0, m.OP_DESCRIBE, b"\0\0\0\0", sid=0)
    assert m.split_tlvs(p[1:])
    assert len(w.ok(0, m.OP_CLOCK, sid=0)) == 12


FIXED = {m.OP_CONFIRM: 16, m.OP_CLOCK: 12, m.OP_OPEN: 8, m.OP_END: 0, m.OP_KEEPALIVE: 0, m.OP_LOCK_STATE: 5}


def test_fn_0_answers_have_their_fixed_parts_and_a_tlv_tail_after():
    """core §2.3, §11: each fn 0 answer is exactly its fixed part (confirm 16 then TLV transport, clock 12, open 8,
    end / keepalive 0, lock_state 5), never longer; what is added comes after it as TLVs, which the host skips."""
    extra = m.tlv(0x3E, b"new")
    ep = build(tail=extra)
    w = Wire(ep)
    sizes = {}
    for op, body, sid in ((m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", 0), (m.OP_CLOCK, b"", 0),
                          (m.OP_OPEN, struct.pack("<IB", 3000, 0), SID), (m.OP_LOCK_STATE, b"", 0),
                          (m.OP_KEEPALIVE, b"", SID), (m.OP_END, b"", SID)):
        p = w.ok(0, op, body, sid)
        assert p.endswith(extra)
        sizes[op] = len(p) - len(extra) - (4 if op == m.OP_CONFIRM else 0)
    assert sizes == FIXED
    hst = host_on(build(tail=extra))
    assert hst.confirm()["tail"].get(0x3E) == b"new"
    hst.open()
    hst.keepalive()
    assert hst.lock_state()[0] and hst.clock().boot_id == 0x1234ABCD
    hst.end()


def test_a_list_is_count_then_elements_without_lengths():
    """core §2.3, §7.2: list's answer is total(u16) count(u8) and count entries, each read field by field (fn instance
    revision name_len name) - no element length; TLVs may follow the counted list."""
    ep = build(tail=m.tlv(0x3E, b"x"))
    hst = host_on(ep)
    got = core.list_entries(hst)
    assert [e.name for e in got] == [ep.names[fn] for fn in sorted(ep.names) if fn]
    p = Wire(ep).ok(0, m.OP_LIST, b"\0\0", sid=0)
    assert p[3:5] == struct.pack("<H", got[0].fn) and p[2] == len(m.Reader(p).take("HB") and got[:p[2]])


def test_the_host_skips_unknown_tags_and_refuses_a_short_answer():
    """core §2.3: after an answer's fixed part the host skips tags it does not know; an answer shorter than its fixed
    part is broken; a known TLV whose value is not its defined length is not used."""
    hst = h.Host(scripted(completed(struct.pack("<IQ", 9, 10) + m.tlv(0x7E, b"?"))))
    assert hst.clock().uptime_ns == 10
    with pytest.raises(m.ShortPayload):
        h.Host(scripted(completed(struct.pack("<IQ", 9, 10)[:11]))).clock()
    confirm = m.CONFIRM_RESULT + struct.pack("<BHIBI", 1, 64, 64, 1, 3)
    assert h.Host(scripted(completed(confirm + m.tlv(0x01, b"\x02")))).confirm()["transport"] == 2
    assert h.Host(scripted(completed(confirm + m.tlv(0x01, b"\x02\x00")))).confirm()["transport"] is None
    una = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x01, b"\x01\x00") + m.tlv(0x02, b"\x05")
                                 + m.tlv(0x05, b"\x01\x00\x00")))
    assert (una.cause, una.channels, una.fn) == (None, [], None)
    for bad in (b"", b"x" * 33):
        p = struct.pack("<BI", 1, 5) + m.tlv(OWNER, bad)
        assert h.Host(scripted(completed(p))).lock_owner() == (True, 5, None)
    p = struct.pack("<BI", 1, 5) + m.tlv(OWNER, b"ok") + m.tlv(OWNER, b"x" * 33)
    assert h.Host(scripted(completed(p))).lock_owner()[2] == "ok"      # the first copy; the broken later one unused


def streaming(profile=vb.p4_x035, min_bytes=0, max_delay_ms=0):
    """A logic capture streaming at 1 MHz on channels 20, 21 with its subscription: (ep, hst, track, clock)."""
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    hst = h.Host(lambda b: ep.handle(b, 0), rng=random.Random(3))
    hst.open(3000)
    lc = cap.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
    lc.configure(rate=1_000_000, mode=cap.STREAMING)
    lc.subscribe(min_bytes, max_delay_ms)
    lc.start()
    return ep, hst, lc, clock


def frames(ep, role=None):
    return [f for f in ep.pushes() if role is None or f[0] == role]


def test_data_frames_are_their_fixed_part_then_tlvs():
    """core §2.3, §10.2: a data frame is role fn seq position(u64) len(u16) data, then TLVs - the same for every
    interface; an event's fixed part is its kind's, then TLVs."""
    ep, hst, lc, clock = streaming()
    clock.t += 5
    data = frames(ep, m.ROLE_DATA)
    assert data
    for f in data:
        fn, seq, position, n = struct.unpack_from("<HHQH", f, 1)
        assert fn == lc.fn and len(f) >= 15 + n
        m.Tail.parse(f[15 + n:])
    lc.stop()
    for f in frames(ep, m.ROLE_EVENT):
        assert cap.unpack_event(f) is not None


def test_the_host_sends_harmful_or_meaningless_if_ignored_tlvs_critical():
    """core §2.3: TLVs that would be harmful or pointless if ignored (scan's max_speed here; attach's reset in
    test_conformance_host_critical_calls) go with the critical bit."""
    from oep_client import riscv
    sent = []
    ep = build(vb.esp32_v003)
    hst = h.Host(lambda b: sent.append(b) or ep.handle(b, 0), rng=random.Random(5))
    hst.open()
    riscv.Wire(hst, "oep.wire.swio").scan(max_speed=1_000_000)
    req = m.Request.unpack(sent[-1])
    tail = req.payload[-7:]                                                    # the last TLV: tag len(2) u32
    assert m.split_tlvs(tail) == [(0x80 | RVSWD.tlv["scan"]["max_speed"], struct.pack("<I", 1_000_000))]


def test_an_implemented_tlv_is_the_same_with_or_without_bit_7(ep):
    """core §2.3: the probe treats a TLV it implements alike with and without bit 7 - open's owner of 33 bytes (outside
    1-32) is malformed either way, and taken either way when right."""
    w = Wire(ep)
    for critical in (False, True):
        rejected(w.open(tail=m.tlv(OWNER, b"x" * 33, critical=critical)), m.MALFORMED)
        assert ep_holder(ep) is None
    assert w.open(tail=m.tlv(OWNER, b"y", critical=True)).succeeded and w.lock_state()[2] == b"y"


def test_a_request_tlvs_value_is_not_extended():
    """core §2.3: a request TLV's value is never longer than defined (new fields take new tags): plan's role_assignment
    with one byte more is malformed, nothing applied."""
    ep = build()
    w = Wire(ep)
    w.open()
    plan = ep.fns[PLAN.name]
    rejected(w.raw(plan, PLAN.op["plan_apply"], m.tlv(0x10, struct.pack("<HBHB", 4, 1, 20, 0))), m.MALFORMED)
    assert not ep.plan
    assert w.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 20)).succeeded


def test_a_tag_twice_the_first_is_used_and_the_later_is_checked_too(ep):
    """core §2.3: a non-repeating tag sent twice: the first is used; the later one is checked as well - a good owner
    followed by an owner of a wrong length makes the whole open malformed."""
    w = Wire(ep)
    rejected(w.open(tail=m.tlv(OWNER, b"good") + m.tlv(OWNER, b"x" * 33)), m.MALFORMED)
    assert ep_holder(ep) is None
    assert w.open(tail=m.tlv(OWNER, b"one") + m.tlv(OWNER, b"two")).succeeded
    assert w.lock_state()[2] == b"one"
    assert m.Tail.parse(m.tlv(5, b"a") + m.tlv(5, b"b")).get(5) == b"a"


def test_a_request_shorter_than_its_fixed_part_is_malformed(ep):
    """core §2.3: a request shorter than its fixed part is malformed: confirm, list, describe, open - and no field of a
    fixed part may be left out (open without its force byte)."""
    w = Wire(ep)
    rejected(w.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01", sid=0), m.MALFORMED)
    rejected(w.raw(0, m.OP_LIST, b"\0", sid=0), m.MALFORMED)
    rejected(w.raw(0, m.OP_DESCRIBE, b"\0\0\0", sid=0), m.MALFORMED)
    rejected(w.raw(0, m.OP_OPEN, struct.pack("<I", 3000)), m.MALFORMED)
    assert ep_holder(ep) is None


def test_a_counted_list_may_be_followed_by_tlvs():
    """core §2.3: a variable list has its count in front, so TLVs can follow it: list's entries then a tail the host
    skips, unsupported's tag then its TLVs."""
    ep = build(tail=m.tlv(0x3E, b"after"))
    p = Wire(ep).ok(0, m.OP_LIST, b"\0\0", sid=0)
    total, page = catalog.unpack_list_result(p)
    assert page and p.endswith(m.tlv(0x3E, b"after"))
    u = h.Unsupported(m.Result(1, m.REJECTED, m.UNSUPPORTED, b"\x00" + m.tlv(0x02, b"\x03\x00")))
    assert u.tlvs == [(0x02, b"\x03\x00")]


# ---- §2.4 unknown values ---------------------------------------------------------------------------------------------

class FakeStream:
    """A pyserial-shaped stream that hands out `incoming` and records writes."""

    def __init__(self, incoming=b""):
        self.incoming, self.written = bytearray(incoming), bytearray()
        self.baudrate = 115200

    @property
    def in_waiting(self):
        return len(self.incoming)

    def read(self, n=1):
        out = bytes(self.incoming[:n])
        del self.incoming[:n]
        return out

    def write(self, data):
        self.written += data
        return len(data)

    def reset_input_buffer(self):
        pass


def serial_link(*messages):
    from oep_client import cobs
    lk = link.SerialLink.on_stream(FakeStream(), framing="cobs", timeout=0.2)
    lk.stream.incoming += b"".join(cobs.frame(x) for x in messages)
    return lk


def test_the_host_routes_by_role_and_drops_unknown_roles_and_requests():
    """core §2.4, §10.1: frames are sorted by role; only results (0x02) are matched by corr - an event or data frame
    whose fn equals the corr is not taken for the reply; unknown roles and requests (0x01) are dropped; a result for
    another corr is read past."""
    reply = m.Result(7, m.COMPLETED, m.SUCCESS, b"ok").pack()
    event = bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 7, 0, 1)
    data = bytes([m.ROLE_DATA]) + struct.pack("<HHQH", 7, 0, 0, 0)
    stray = m.Result(6, m.COMPLETED, m.SUCCESS).pack()
    lk = serial_link(event, data, b"\x05abcdef", m.Request(7, 0, 4, b"", 0).pack(), b"\x00\x07\x00\x00\x00", stray,
                     reply)
    assert lk._recv_for(7) == reply
    assert list(lk.events) == [event] and list(lk.pushes) == [data]
    assert lk.dropped == 3 and lk.stale == 1


def test_the_probe_drops_what_is_not_a_whole_request(ep):
    """core §2.4, §2.5: the probe answers nothing to a message whose role is not 0x01 (0x00 invalid, 0x02-0x04, the
    unassigned ones) or to a request shorter than its 10-byte header."""
    w = Wire(ep)
    good = w.bytes(0, m.OP_CLOCK, sid=0)
    for role in (0x00, 0x02, 0x03, 0x04, 0x05, 0x81, 0xFF):
        assert ep.handle(bytes([role]) + good[1:], 0) is None
    for n in range(10):
        assert ep.handle(good[:n], 0) is None
    assert w.send(good).succeeded


def test_the_host_takes_short_results_and_notifications_for_broken_frames():
    """core §2.4: a result shorter than 5 bytes, an event or data frame shorter than its header, is a broken frame."""
    from oep_client import cobs
    for short in (b"\x02\x07\x00\x01", bytes([m.ROLE_EVENT]) + b"\x01\x00\x00\x00",
                  bytes([m.ROLE_DATA]) + b"\x01\x00\x00"):
        lk = serial_link(short)
        with pytest.raises(cobs.CorruptFrame):
            lk._recv_for(7)
    lk = link.SerialLink.on_stream(FakeStream(), framing="length", timeout=0.2)
    with pytest.raises(link.FramingLost):
        lk._route(b"\x02\x07\x00\x01")


def test_an_unknown_resolution_or_outcome_is_a_failure():
    """core §2.4: a resolution or a completed outcome this host does not know is a failure."""
    for body in (bytes([2, 0]), bytes([m.COMPLETED, 3]), bytes([m.COMPLETED, 0xFF])):
        hst = h.Host(scripted(body))
        hst.revision = 1
        with pytest.raises(h.Failed):
            hst.call(4, 0x11, locked=False)
        assert not m.Result(1, body[0], body[1]).succeeded and not m.Result(1, body[0], body[1]).ran


def test_an_unknown_reject_reason_or_status_is_a_failure():
    """core §2.4: an unknown reason is a refusal (raised, never success); an unknown interface status is a failure."""
    hst = h.Host(scripted(refused(0x55)))
    with pytest.raises(h.Rejected) as e:
        hst.request(4, 0x11, locked=False)
    assert "0x55" in str(e.value)
    assert m.Result(1, m.REJECTED, 0x55).describe() == "rejected: reason 0x55"


def test_an_unknown_event_kind_is_dropped_and_its_seq_counted():
    """core §2.4, §10.2: an event of a kind the host does not know is dropped; its seq still counts, so the data after
    it shows no lost frame."""
    ep, hst, lc, clock = streaming()
    clock.t += 1
    data = frames(ep, m.ROLE_DATA)
    first = struct.unpack_from("<H", data[0], 3)[0]
    lc.stop()
    stopped = [f for f in ep.pushes() if f[0] == m.ROLE_EVENT and f[5] == cap.EVENT_STOPPED]
    unknown = bytes([m.ROLE_EVENT]) + struct.pack("<HHB", lc.fn, first + 1, 0x7E) + b"\x01\x02"
    later = [d[:3] + struct.pack("<H", first + 2 + k) + d[5:] for k, d in enumerate(data[1:])]
    lk = serial_link(data[0], unknown, *later, *stopped)
    lk.pump(0.05)
    got = lc.stream(lk, seconds=0.01)
    assert got.seq_lost == 0
    assert [e.kind for e in lc.events(lk)] == ["stopped"]


def test_unknown_values_that_tell_no_failure_are_shown_as_unknown():
    """core §2.4, §12.1: an unknown cause is shown as its raw value; an unknown reason stays a refusal."""
    una = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x01, b"\x3a") + m.tlv(0x02, b"\x05\x00")))
    assert una.cause == "58" and una.channels == [5]


# ---- §2.5 number spaces ----------------------------------------------------------------------------------------------

def test_fn_0_op_space(ep):
    """core §2.5: fn 0's ops are 0x01-0x0F discovery and 0x10-0x1F session; anything else (and an unused one in those
    ranges) is unknown_operation, before session_required."""
    for op in CORE.op.values():
        assert 0x01 <= op <= 0x0F or 0x10 <= op <= 0x1F
    assert {op for op in CORE.op.values() if op < 0x10} == {m.OP_CONFIRM, m.OP_LIST, m.OP_DESCRIBE, m.OP_CLOCK}
    w = Wire(ep)
    for op in (0x00, 0x05, 0x0F, 0x14, 0x1F, 0x20, 0xFF):
        rejected(w.raw(0, op, sid=0), m.UNKNOWN_OPERATION)


def test_interface_op_space_keeps_0x01_0x0f_for_the_shared_ops():
    """core §2.5, §10.3: in every interface 0x01 is subscribe and 0x02 unsubscribe and no other op is below 0x10."""
    for i in reg.INTERFACES.values():
        for name, op in i.op.items():
            assert op >= 0x10 or (name, op) in (("subscribe", 0x01), ("unsubscribe", 0x02)), (i.name, name)
            assert op <= 0xFF


def test_reason_and_cause_spaces():
    """core §2.5, §4.3, §12 item: the core's reject reasons and unavailable causes are 0x01-0x3F; an interface's own
    are 0x40-0x7F; nothing at 0x80 and up."""
    assert all(0x01 <= v <= 0x3F for v in reg.REJECT_REASONS.values())
    assert all(0x01 <= v <= 0x3F for v in CAUSE.values())
    for i in reg.INTERFACES.values():
        for enum, values in i.enum.items():
            if enum in ("reject_reason", "unavailable_cause", "status"):
                assert all(0x40 <= v <= 0x7F for v in values.values()), (i.name, enum)


def test_outcome_space():
    """core §2.5, §4.2: outcome 0 success, 1 failed, 2 partial; any other is reserved (a failure for the host)."""
    assert reg.OUTCOMES == {"success": 0, "failed": 1, "partial": 2}
    assert [m.Result(1, m.COMPLETED, o).ran for o in range(4)] == [True, True, True, False]


def test_event_kind_space():
    """core §2.5: an interface's event kinds are 0x01-0x7F (0x80 up unassigned); fn 0 has none."""
    assert CORE.event == {}
    for i in reg.INTERFACES.values():
        for kind in i.event.values():
            code = kind if isinstance(kind, int) else kind["code"] if isinstance(kind, dict) else kind
            assert 0x01 <= code <= 0x7F, i.name


def test_tlv_tag_spaces():
    """core §2.5, §7.4: a TLV number is 0x01-0x7F in every context (0 invalid, bit 7 the critical mark); describe's
    common tags 0x01-0x3F, an interface's own and fn 0's probe-wide ones 0x40-0x7F."""
    def numbers(tlv):
        for ctx, tags_ in tlv.items():
            for name, tag in tags_.items():
                yield ctx, name, tag
    for space in [CORE.tlv] + [i.tlv for i in reg.INTERFACES.values()]:
        for ctx, name, tag in numbers(space):
            assert 0x01 <= tag <= 0x7F, (ctx, name)
            if ctx == "describe":
                assert tag >= 0x40, name
    assert all(0x01 <= v <= 0x3F for v in reg.DESCRIBE_COMMON.values())


def test_resource_numbers_are_u32_from_1():
    """core §2.5, §9: resource numbers are one u32 space of the probe from 1 (0 never given)."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    first = struct.unpack_from("<I", w.ok(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 2, 3, 4_000_000)))[0]
    assert first == 1
    con = ep.fns["oep.target.console"]
    second = struct.unpack_from("<I", w.ok(con, reg.TARGET_CONSOLE.op["open"], struct.pack("<IB", first, ep.mechanism_number("sdi"))))[0]
    assert second == 2


# ---- §2.8 name and revision ------------------------------------------------------------------------------------------

def test_the_host_does_not_use_an_interface_revision_it_does_not_know():
    """core §2.8: the fixed forms are (name, revision)'s; a host never uses a revision it does not know."""
    probe = vb.p4_x035()
    probe.offered = [vb.Offered(o.fn, o.instance, o.name, o.tlvs, 2, o.inner) if o.name == GPIO.name else o
                     for o in probe.offered]
    ep = endpoint.Endpoint(probe, Clock())
    hst = host_on(ep)

    class Rev1(core.Interface):
        NAME, REVISION = GPIO.name, 1
    with pytest.raises(core.UnsupportedRevision):
        Rev1(hst)


def test_an_optional_op_is_known_by_ops_and_not_sent_when_absent():
    """core §2.8, §12.1: an optional op added without a new revision is used only when the fn's ops set it - the host
    reads ops (not features) and sends nothing for an op the fn does not offer."""
    ep = build(vb.esp32_v003)                                                # riscv-dm without step
    sent = []
    hst = h.Host(lambda b: sent.append(b) or ep.handle(b, 0), rng=random.Random(2))
    dm = core.find(hst, RV.name)
    assert core.offers(hst, dm, RV.op["run"]) and not core.offers(hst, dm, RV.op["step"])
    n = len(sent)
    with pytest.raises(h.Rejected) as e:
        core.require(hst, dm, RV.op["step"])
    assert e.value.result.detail == m.UNKNOWN_OPERATION and len(sent) == n


def test_the_protocol_revision_is_confirms():
    """core §2.8, §7.1: the core's shapes are protocol revision 1; the host asks for the range it speaks (1..1)."""
    assert reg.PROTOCOL_REVISION == h.MIN_REVISION == h.MAX_REVISION == 1
    ep = build()
    hst = host_on(ep)
    assert hst.confirm()["revision"] == 1


# ---- §4.1 request ----------------------------------------------------------------------------------------------------

def test_the_host_corr_is_never_0_and_wraps_to_1():
    """core §4.1: corr is a u16 never 0, moved forward by one per new request, 65535 followed by 1."""
    hst = h.Host(lambda b: b)
    hst._corr = 65533
    assert [hst.next_corr() for _ in range(4)] == [65534, 65535, 1, 2]


def test_every_request_carries_a_session_id():
    """core §4.1: every request has a session_id - the session's for a request in it, 0 for a lock-free one outside."""
    ep = build()
    sent = []
    hst = h.Host(lambda b: sent.append(b) or ep.handle(b, 0), rng=random.Random(9))
    hst.lock_state()
    hst.open()
    hst.keepalive()
    hst.lock_state()
    sids = [m.Request.unpack(b).session for b in sent]
    assert all(len(b) >= m.REQUEST_HEADER for b in sent)
    assert sids[0] == 0 and sids[-2] == hst.session != 0 and sids[-1] == 0


def test_a_locked_op_with_session_id_0_is_session_required(ep):
    """core §4.1, §4.3: an op that needs the lock with session_id 0 is session_required (also on an interface)."""
    w = Wire(ep)
    for op in (m.OP_END, m.OP_KEEPALIVE):
        rejected(w.raw(0, op, sid=0), m.SESSION_REQUIRED)
    if not ep.probe.core_contract:
        rejected(w.raw(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 0), sid=0), m.SESSION_REQUIRED)


def test_a_lock_free_op_with_0_touches_no_session_and_with_an_id_is_checked(ep):
    """core §4.1, §6.1: a lock-free request with session_id 0 skips the session check and leaves the lease; with an id
    it goes through the check (no_session / locked) and, the holder's, restarts the lease."""
    w = Wire(ep)
    rejected(w.raw(0, m.OP_CLOCK), m.NO_SESSION)
    w.open(3000)
    ep.now.t = 2000
    assert w.raw(0, m.OP_CLOCK, sid=0).succeeded and w.lock_state()[1] == 1000
    rejected(w.raw(0, m.OP_CLOCK, sid=0x77), m.LOCKED)
    assert w.lock_state()[1] == 1000
    assert w.raw(0, m.OP_CLOCK).succeeded and w.lock_state()[1] == 3000


def test_open_with_session_id_0_is_malformed(ep):
    """core §4.1, §6.1: open carries the id it opens; 0 is malformed."""
    rejected(Wire(ep).open(sid=0), m.MALFORMED)
    assert ep_holder(ep) is None


# ---- §4.2 answer -----------------------------------------------------------------------------------------------------

def test_one_answer_per_request_with_its_corr_and_no_fn_or_op(ep):
    """core §4.2: one answer per request, with the request's corr; the answer has no fn or op - rejected is
    resolution 0x00 with the reason as detail, completed 0x01 with the outcome."""
    w = Wire(ep)
    for corr in (1, 300, 65535):
        raw = ep.handle(m.Request(corr, 0, m.OP_CLOCK, b"", 0).pack(), 0)
        assert raw[0] == m.ROLE_RESULT and raw[1:3] == struct.pack("<H", corr) and raw[3:5] == b"\x01\x00"
        assert len(raw) == 5 + 12
    raw = ep.handle(m.Request(9, 0, m.OP_KEEPALIVE, b"", 0).pack(), 0)
    assert raw == m.Result(9, 0x00, m.SESSION_REQUIRED).pack()
    w.open()
    raw = ep.handle(m.Request(10, 0, m.OP_KEEPALIVE, b"", 0x99).pack(), 0)
    assert raw[3:5] == bytes([0x00, m.LOCKED]) and len(raw) >= 9


def test_executed_and_failed_is_completed_failed_not_rejected():
    """core §4.2, §12 rule 4: a request accepted and run that did not work is completed failed (with how far it got),
    never rejected: a riscv-dm halt on a connection whose target stops answering."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    conn = struct.unpack_from("<I", w.ok(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 2, 3, 4_000_000)))[0]
    for tg in ep.targets.values():                                           # nothing answers on the wire any more
        tg.present = False
    r = w.raw(2, RV.op["halt"], struct.pack("<I", conn))
    assert r.resolution == m.COMPLETED and r.detail == m.FAILED and r.payload[:1] == bytes([reg.STATUS["line"]])


def test_the_host_does_not_run_a_failed_request_again():
    """core §4.2: a failure is no ground for running a state change again - one request, Failed raised."""
    hst = h.Host(scripted(completed(b"\x00", m.FAILED)))
    with pytest.raises(h.Failed):
        hst.call(4, 0x10, b"x", locked=False)
    assert len(hst.send.sent) == 1


# ---- §4.3 reasons and their order ------------------------------------------------------------------------------------

def test_each_core_reason_is_given_where_the_table_says():
    """core §4.3: unknown_function (the header's fn, or describe's), unknown_operation, malformed, unavailable with its
    TLVs, no_session, locked with remaining ms and owner, session_required, no_resource, unsupported with its tag,
    result_lost - each with its value and payload."""
    ep = build()
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    rejected(a.raw(99, 0x10, sid=0), m.UNKNOWN_FUNCTION)
    rejected(a.raw(0, m.OP_DESCRIBE, struct.pack("<HH", 99, 0), sid=0), m.UNKNOWN_FUNCTION)
    rejected(a.raw(0, 0x30, sid=0), m.UNKNOWN_OPERATION)
    rejected(a.raw(0, m.OP_LIST, b"", sid=0), m.MALFORMED)
    rejected(a.raw(0, m.OP_KEEPALIVE), m.NO_SESSION)
    rejected(a.raw(0, m.OP_KEEPALIVE, sid=0), m.SESSION_REQUIRED)
    assert a.open(tail=m.tlv(OWNER, b"tool")).succeeded
    r = rejected(b.open(), m.LOCKED)
    assert struct.unpack_from("<I", r.payload)[0] == 3000 and m.Tail.parse(r.payload[4:]).get(OWNER) == b"tool"
    plan = ep.fns[PLAN.name]
    assert a.raw(plan, PLAN.op["plan_apply"], assign(5, 1, 20)).succeeded
    r = rejected(a.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 20)), m.UNAVAILABLE)
    una = h.Unavailable(r)
    assert una.cause == "pin_in_use" and una.channels == [20]
    rejected(a.raw(2, RV.op["halt"], struct.pack("<I", 777)), m.NO_RESOURCE)
    r = rejected(a.raw(0, m.OP_CLOCK, m.tlv(0x30, b"", critical=True)), m.UNSUPPORTED)
    assert r.payload == b"\xb0"
    rejected(a.raw(0, m.OP_KEEPALIVE, corr=1), m.RESULT_LOST)
    assert (m.UNKNOWN_FUNCTION, m.UNKNOWN_OPERATION, m.MALFORMED, m.UNAVAILABLE, m.WINDOW_EXCEEDED, m.NO_SESSION,
            m.LOCKED, m.SESSION_REQUIRED, m.NO_RESOURCE, m.UNSUPPORTED, m.RESULT_LOST) == (
        1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12)
    assert isinstance(h.rejection(m.Result(1, 0, m.WINDOW_EXCEEDED)), h.Rejected)


def test_the_header_order_corr_fn_op_session(ep):
    """core §4.3 order 1: corr 0 is malformed before anything; then unknown_function, unknown_operation,
    session_required in that order."""
    w = Wire(ep)
    rejected(w.raw(99, 0x7F, sid=0, corr=0), m.MALFORMED)
    rejected(w.raw(99, 0x7F, sid=0), m.UNKNOWN_FUNCTION)
    rejected(w.raw(0, 0x7F, sid=0), m.UNKNOWN_OPERATION)
    rejected(w.raw(0, m.OP_END, sid=0), m.SESSION_REQUIRED)


def test_the_resend_check_comes_before_the_session_check(ep):
    """core §4.3 order 2, §5.1: a request of the last session sent again is answered from the record (or result_lost)
    before the session check - after the session's lease ran out, not no_session."""
    w = Wire(ep)
    w.open(1000)
    data = w.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    ep.now.t = 5000                                                           # the lease ran out
    again = ep.handle(data, 0)
    assert again == first or m.Result.unpack(again).detail == m.RESULT_LOST
    rejected(w.raw(0, m.OP_KEEPALIVE), m.NO_SESSION)


def test_the_session_check_comes_after_the_header_and_before_the_payload(ep):
    """core §4.3 order 3, 4: another session's request is locked even with a broken payload; the holder's broken one
    is then malformed."""
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open()
    rejected(b.raw(0, m.OP_KEEPALIVE, b"\x01"), m.LOCKED)
    rejected(a.raw(0, m.OP_KEEPALIVE, b"\x01"), m.MALFORMED)


def test_an_open_s_session_check_comes_before_its_payload(ep):
    """core §4.3 order 3 before 4, §6.2: an open of another id without force while the lock is held is locked, and the
    ended id's open is no_session, even when the owner TLV (or the fixed part) is broken; the holder's broken open is
    then malformed (review 4: the full bench read the payload first and answered malformed)."""
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open()
    bad = m.tlv(OWNER, b"x" * 33)
    rejected(b.open(tail=bad), m.LOCKED)
    rejected(b.raw(0, m.OP_OPEN, b"\0\0"), m.LOCKED)                         # too short to ask for force
    rejected(a.open(tail=bad), m.MALFORMED)
    assert a.raw(0, m.OP_END).succeeded
    rejected(a.open(tail=bad), m.NO_SESSION)


def test_an_order_4_unknown_function_moves_the_last_corr_and_the_lease(ep):
    """core §5.1, §6.1: a holder's request refused in order 4 with unknown_function (describe naming an fn the probe
    lacks) passed orders 2 and 3: it is recorded and moves the last corr, and the lease restarts at its answer (review 4:
    the full bench took any unknown_function for the header's and did neither)."""
    w = Wire(ep)
    w.open(lease=3000)
    ep.now.t = 2000
    data = w.bytes(0, m.OP_DESCRIBE, struct.pack("<HH", 0x7777, 0))
    first = ep.handle(data, 0)
    rejected(m.Result.unpack(first), m.UNKNOWN_FUNCTION)
    assert w.lock_state()[1] == 3000                                          # restarted at the answer
    assert ep.handle(data, 0) == first                                        # recorded
    rejected(m.Result.unpack(ep.handle(data + m.tlv(0x3E, b"x"), 0)), m.MALFORMED)   # its corr is in the record


def test_the_rest_is_checked_before_anything_changes(ep):
    """core §4.3 order 4: format, support and state are all checked before a change - a refused open changes nothing
    (no lock, no owner); a refused plan changes no plan."""
    w = Wire(ep)
    r = w.open(tail=m.tlv(OWNER, b"") + m.tlv(0x30, b"", critical=True))
    assert r.resolution == m.REJECTED and r.detail in (m.MALFORMED, m.UNSUPPORTED)
    assert ep_holder(ep) is None and w.lock_state() == (0, 0, None)


def test_the_cause_fn_and_channel_tlvs():
    """core §4.3: unavailable's cause (1-7, the names below), channel (u16, repeats) and fn (u16); the host reads each,
    missing ones as None."""
    assert CAUSE == {"pin_in_use": 1, "limit": 2, "storage_full": 3, "bound_in_group": 4, "held_by_settings": 5,
                     "wrong_state": 6, "policy_denied": 7}
    una = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x01, b"\x04") + m.tlv(0x05, b"\x0b\x00")
                                 + m.tlv(0x02, b"\x10\x00") + m.tlv(0x02, b"\x11\x00")))
    assert (una.cause, una.fn, una.channels) == ("bound_in_group", 11, [16, 17])
    assert (h.Unavailable(m.Result(1, 0, 4)).cause, h.Unavailable(m.Result(1, 0, 4)).fn) == (None, None)


# ---- §4.4 pipeline ---------------------------------------------------------------------------------------------------

def test_confirm_gives_this_transports_limits(ep):
    """core §4.4, §7.1: confirm returns max_frame (64 or more), window (max_frame or more), max_inflight (1 or more)."""
    p = Wire(ep).ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)
    max_frame, window, inflight = struct.unpack_from("<HIB", p, 5)
    assert max_frame >= 64 and window >= max_frame and inflight >= 1
    assert (max_frame, window, inflight) == (ep.probe.max_frame, ep.window, ep.max_inflight)


def test_the_host_keeps_max_inflight_and_window():
    """core §4.4: the host's pipeline never has more than max_inflight requests or window bytes outstanding."""
    ep = build()
    outstanding, most = [], [0, 0]

    class Stream(FakeStream):
        def write(self, data):
            from oep_client import cobs
            for part in bytes(data).split(b"\x00"):
                if part:
                    outstanding.append(cobs.unframe(part))
            most[0] = max(most[0], len(outstanding))
            most[1] = max(most[1], sum(len(x) for x in outstanding))
            return len(data)

        def read(self, n=1):
            from oep_client import cobs
            if not self.incoming and outstanding:
                self.incoming += cobs.frame(ep.handle(outstanding.pop(0), 0))
            return super().read(n)

        @property
        def in_waiting(self):
            return max(1, len(self.incoming))
    lk = link.SerialLink.on_stream(Stream(), framing="cobs", timeout=0.5)
    reqs = [m.Request(i + 1, 0, m.OP_CLOCK, b"", 0).pack() for i in range(9)]
    out = lk.exchange(reqs, 3, 40)
    assert [m.Result.unpack(x).corr for x in out] == list(range(1, 10))
    assert most[0] <= 3 and most[1] <= 40


def test_the_probe_takes_new_requests_while_one_is_processed_and_answers_in_order():
    """core §4.4: within window / max_inflight a request is taken while an earlier one is processed; requests are
    processed and answered in the order received."""
    ep = build()
    w = Wire(ep)
    datas = [w.bytes(0, m.OP_CLOCK, sid=0) for _ in range(ep.max_inflight)]
    assert ep.receive(datas[0]) == "queued"
    ep._processing = None
    for d in datas[1:]:
        assert ep.receive(d) == "queued"
    answers = [ep.process() for _ in datas]
    assert [m.Result.unpack(a).corr for a in answers] == [m.Request.unpack(d).corr for d in datas]


def test_a_copy_of_an_unfinished_request_is_not_a_new_one():
    """core §4.4: a copy of a request still waiting on the same transport is not another outstanding request - dropped
    unexamined, never run."""
    ep = build()
    w = Wire(ep)
    w.open()
    data = w.bytes(0, m.OP_KEEPALIVE)
    assert ep.receive(data) == "queued" and ep.receive(data) == "coalesced"
    assert ep.credits() == {0: 1}
    before = ep.executions
    ep.process()
    assert ep.executions == before + 1


def test_a_fixed_result_past_max_frame_is_unsupported_or_cut_as_the_interface_says():
    """core §4.4: a request that fits but whose fixed result cannot fit max_frame is refused unsupported (tag 0x00)
    before it runs - a riscv-dm dmi list of 14 reads on a 64-byte frame (10 + 4 x 14 > 64), even before its connection
    is looked up; where the interface defines how to cut a result it is cut - link's source sends max_frame - 7."""
    ep = build(vb.esp32_v003_64)
    w = Wire(ep)
    w.open()
    dm = ep.fns[RV.name]
    read = bytes([RV.enum["dmi_step"]["read"], 0x11])
    for n, fits in ((13, True), (14, False)):
        body = struct.pack("<IH", 777, n) + read * n
        assert m.REQUEST_HEADER + len(body) <= 64
        r = w.raw(dm, RV.op["dmi"], body)
        if fits:
            rejected(r, m.NO_RESOURCE)                                       # it would fit: the connection is looked up
        else:
            assert rejected(r, m.UNSUPPORTED).payload == b"\x00"
    fn = ep.fns[reg.PROBE_LINK.name]
    p = w.ok(fn, reg.PROBE_LINK.op["source"], struct.pack("<I", 1000), sid=0)
    assert struct.unpack_from("<H", p)[0] == 64 - 7 and 5 + len(p) <= 64


def test_limits_are_per_transport_and_the_resend_table_is_shared():
    """core §4.4, §5.1, §7.1: each transport's confirm gives its own limits and its own transport index; the resend
    table is one for the probe - a request sent again on another transport gets its recorded answer."""
    ep = build()
    for t in (0, 1, 2):
        p = Wire(ep, transport=t).ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)
        assert m.Tail.parse(p[16:]).get(0x01) == bytes([t])
    w = Wire(ep)
    w.open()
    data = w.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    assert ep.handle(data, 1) == first


def test_the_host_waits_at_least_the_floor():
    """core §4.4: the host waits at least the processing bound + 1000 ms + the transfer time: max_op_ms, fn 0's quick
    ops min(max_op_ms, 1000), 1000 while max_op_ms is unknown; an argument's wait never shortens it."""
    hst = h.Host(lambda b: b)
    assert hst.op_bound_ms(0, m.OP_CONFIRM) == 1000 and hst.op_bound_ms(4, 0x10) == 1000
    hst._describes[0] = [(CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 5000))]
    assert hst.op_bound_ms(0, m.OP_LIST) == 1000 and hst.op_bound_ms(0, m.OP_OPEN) == 5000
    assert hst.op_bound_ms(4, 0x10) == 5000
    hst._describes[0] = [(CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 300))]
    assert hst.op_bound_ms(0, m.OP_CLOCK) == 300
    lk = link.SerialLink.on_stream(FakeStream(), framing="cobs")
    lk.expected_s = lambda: 5.0
    lk.tx_len, lk.max_frame = 20, 64
    assert lk.wait_floor_s() == pytest.approx(5.0 + 1.0 + (20 + 64 * 3) * 10 / 115200)


def test_an_interfaces_wait_does_not_shorten_the_floor():
    """core §4.4: every request waits at least the floor - a short argument wait (expecting 10 ms) still waits for
    max_op_ms."""
    seen = []
    hst = h.Host(lambda b: seen.append(hst.expect_ms) or m.Result(m.Request.unpack(b).corr, 1, 0).pack())
    hst._describes[0] = [(CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 4000))]
    hst.request(4, 0x10, b"", locked=False, expect_ms=10)
    assert seen == [4000]


# ---- §5.1 resend -----------------------------------------------------------------------------------------------------

def test_the_probe_keeps_the_last_corr_and_refuses_one_not_ahead(ep):
    """core §5.1: the last session's last new corr is kept (required, even with no byte record): a request not ahead
    of it - half the space or more behind, or equal and not recorded - is result_lost, never run."""
    ep.remember_max = 0                                                      # no byte record at all
    w = Wire(ep)
    w.open()
    w.raw(0, m.OP_KEEPALIVE, corr=100)
    rejected(w.raw(0, m.OP_KEEPALIVE, corr=100), m.RESULT_LOST)
    rejected(w.raw(0, m.OP_KEEPALIVE, corr=99), m.RESULT_LOST)
    rejected(w.raw(0, m.OP_KEEPALIVE, corr=100 + 0x8000), m.RESULT_LOST)
    assert w.raw(0, m.OP_KEEPALIVE, corr=100 + 0x7FFF).succeeded


def test_the_record_answers_a_resend_by_its_bytes(ep):
    """core §5.1: the same corr again - same bytes: the recorded answer; other bytes: malformed; bytes or answer not
    kept: result_lost. Nothing runs again."""
    w = Wire(ep)
    w.open()
    data = w.bytes(0, m.OP_LOCK_STATE)
    first = ep.handle(data, 0)
    ep.now.t = 500
    assert ep.handle(data, 0) == first
    other = data + m.tlv(0x3E, b"x")
    rejected(m.Result.unpack(ep.handle(other, 0)), m.MALFORMED)
    ep.remember_max = 12                                                      # a 10-byte request kept, not its 17-byte answer
    big = w.bytes(0, m.OP_CLOCK)
    ep.handle(big, 0)
    rejected(m.Result.unpack(ep.handle(big, 0)), m.RESULT_LOST)
    ep.remember_max = 9                                                       # not even the request
    big = w.bytes(0, m.OP_LOCK_STATE)
    ep.handle(big, 0)
    rejected(m.Result.unpack(ep.handle(big, 0)), m.RESULT_LOST)


def test_a_new_corr_that_is_refused_still_moves_the_last_corr(ep):
    """core §5.1: a new request of the last session past order 2 moves the last corr even when refused later."""
    w = Wire(ep)
    w.open()
    rejected(w.raw(0, m.OP_KEEPALIVE, b"\x01", corr=50), m.MALFORMED)
    rejected(w.raw(0, m.OP_KEEPALIVE, corr=49), m.RESULT_LOST)
    assert w.raw(0, m.OP_KEEPALIVE, corr=51).succeeded


def test_end_and_expiry_keep_the_order_state_and_the_same_id_open_keeps_it(ep):
    """core §5.1, §9: end and lease expiry keep the order state and the records; the holder's open again keeps the
    resend state; only a new id's open replaces it."""
    w = Wire(ep)
    w.open()
    data = w.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    assert w.open().succeeded                                                # the holder's open: kept
    assert ep.handle(data, 0) == first
    assert w.raw(0, m.OP_END).succeeded
    assert ep.handle(data, 0) == first                                       # after end: still answered
    rejected(w.raw(0, m.OP_KEEPALIVE, corr=0xF000), m.RESULT_LOST)
    other = Wire(ep, 0x99)
    assert other.open().succeeded                                            # a new id: a new state
    rejected(m.Result.unpack(ep.handle(data, 0)), m.LOCKED)


def test_session_0_requests_are_never_deduplicated(ep):
    """core §5.1: session_id 0 requests are not deduplicated - the same clock bytes twice are run twice; corr 0 is
    malformed."""
    w = Wire(ep)
    data = w.bytes(0, m.OP_CLOCK, sid=0)
    a = ep.handle(data, 0)
    ep.now.t = 7
    b = ep.handle(data, 0)
    assert a != b and struct.unpack_from("<Q", b, 9)[0] == 7_000_000
    rejected(w.raw(0, m.OP_CLOCK, sid=0, corr=0), m.MALFORMED)


def test_the_host_resends_the_same_bytes_and_never_a_new_corr():
    """core §4.1, §5.1: a lost or broken answer: the host sends the very same request again (no new corr)."""
    from oep_client import cobs
    ep = build()
    written = []

    class Stream(FakeStream):
        def write(self, data):
            written.append(bytes(data))
            if len(written) == 2:
                self.incoming += cobs.frame(ep.handle(cobs.unframe(bytes(data)[1:-1]), 0))
            return len(data)
    lk = link.SerialLink.on_stream(Stream(), framing="cobs", timeout=0.1)
    lk.wait_add_s = 0.0
    req = m.Request(5, 0, m.OP_CLOCK, b"", 0).pack()
    assert m.Result.unpack(lk.send(req)).corr == 5
    assert len(written) == 2 and written[0] == written[1]


def test_the_host_gives_up_after_the_resend_and_recovers_before_more():
    """core §5.1: a resend unanswered too: the outcome is unknown, the transport is recovered (confirm, its boot_id)
    before anything else is sent."""
    lk = link.SerialLink.on_stream(FakeStream(), framing="cobs", timeout=0.05)
    lk.wait_add_s = 0.0
    with pytest.raises(link.TransportFailed):
        lk.send(m.Request(5, 0, m.OP_KEEPALIVE, b"", 1).pack())
    assert lk.failed_transport
    with pytest.raises(ConnectionError):
        lk.send(m.Request(6, 0, m.OP_CLOCK, b"", 0).pack())


def test_a_new_session_id_after_a_loss_never_reuses_the_ended_one():
    """core §5.1, §6.1: a host opens a new session under a fresh random id - never the ended one."""
    ep = build()
    hst = host_on(ep)
    hst.open()
    first = hst.session
    hst.end()
    hst.open()
    assert hst.session not in (None, 0, first)


# ---- §6 sessions -----------------------------------------------------------------------------------------------------

def test_one_lock_and_only_its_holder_changes_state():
    """core §6.1: one lock; only its holder's state-changing requests run - another id's are locked."""
    ep = build()
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open()
    plan = ep.fns[PLAN.name]
    rejected(b.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 20)), m.LOCKED)
    assert not ep.plan
    assert a.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 20)).succeeded


def test_the_host_draws_a_random_non_zero_session_id():
    """core §6.1: the host picks each session_id at random (u32, never 0, never a fixed value)."""
    class Rng:
        def __init__(self):
            self.calls = []

        def randrange(self, a, b):
            self.calls.append((a, b))
            return 0xDEADBEEF
    ep = build()
    rng = Rng()
    hst = h.Host(lambda b: ep.handle(b, 0), rng=rng)
    hst.open()
    assert rng.calls == [(1, 1 << 32)] and hst.session == 0xDEADBEEF == ep.holder
    assert h.Host(lambda b: b).rng.__class__ is random.SystemRandom


def test_the_lease_stops_while_the_holders_request_runs_and_restarts_at_its_answer():
    """core §6.1: the holder's new request past the session check pauses the lease while it runs; the answer (rejected
    too) restarts lease_ms; a resend, a session-0 request or another session's do not."""
    ep = build()
    w = Wire(ep)
    w.open(1000)
    ep.receive(w.bytes(0, m.OP_KEEPALIVE))
    ep.process(spend=lambda: setattr(ep.now, "t", ep.now.t + 5000))         # runs past the lease
    assert ep.holder == SID and ep.expires_ms == 6000
    ep.now.t = 5500
    rejected(w.raw(0, m.OP_KEEPALIVE, b"\x01"), m.MALFORMED)
    assert ep.expires_ms == 6500
    data = w.bytes(0, m.OP_LOCK_STATE)
    ep.handle(data, 0)
    ep.now.t = 6000
    ep.handle(data, 0)
    w.raw(0, m.OP_CLOCK, sid=0)
    Wire(ep, 0x77).raw(0, m.OP_KEEPALIVE)
    assert ep.expires_ms == 6500


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_the_decision_table(profile):
    """core §6.2: lock free - open (not the last ended id) takes it with lease and owner; the ended id's open or any
    other request with an id is no_session. Held by S - S's open renews lease_ms keeping everything; another id's open
    without force is locked with remaining and owner, with force takes it; S's other requests run; another id's are
    locked."""
    ep = build(PROFILES[profile])
    s, o = Wire(ep, 0x10), Wire(ep, 0x20)
    rejected(s.raw(0, m.OP_KEEPALIVE), m.NO_SESSION)
    assert s.open(2000, tail=m.tlv(OWNER, b"S")).succeeded
    assert s.lock_state() == (1, 2000, b"S")
    ep.now.t = 500
    r = s.open(5000, tail=m.tlv(OWNER, b"T"))
    assert struct.unpack_from("<I", r.payload)[0] == 5000 and s.lock_state() == (1, 5000, b"S")
    r = rejected(o.open(), m.LOCKED)
    assert struct.unpack_from("<I", r.payload)[0] == 5000 and m.Tail.parse(r.payload[4:]).get(OWNER) == b"S"
    rejected(o.raw(0, m.OP_KEEPALIVE), m.LOCKED)
    assert s.raw(0, m.OP_KEEPALIVE).succeeded
    assert o.open(force=1).succeeded and ep_holder(ep) == 0x20
    assert o.raw(0, m.OP_END).succeeded
    rejected(o.open(), m.NO_SESSION)                                          # the ended id: never again
    rejected(s.raw(0, m.OP_KEEPALIVE), m.NO_SESSION)
    assert s.open().succeeded                                                 # not the last ended id


def test_open_answers_the_lease_and_boot_id_and_nothing_resumes(ep):
    """core §6.4, §6.5: open's answer is lease_ms and boot_id; an ended session is never resumed."""
    w = Wire(ep)
    p = w.ok(0, m.OP_OPEN, struct.pack("<IB", 3000, 0))
    assert struct.unpack("<II", p[:8]) == (3000, ep.boot_id)
    w.raw(0, m.OP_END)
    rejected(w.raw(0, m.OP_KEEPALIVE), m.NO_SESSION)
    rejected(w.open(), m.NO_SESSION)


@pytest.mark.parametrize("asked, given", [(0, 3000), (1, 1000), (999, 1000), (1000, 1000), (60000, 60000),
                                          (60001, 60000), (0xFFFFFFFF, 60000)])
def test_lease_ms_is_taken_or_rounded_into_range(asked, given):
    """core §6.4: lease_ms 0 is the probe's default; 1000-60000 taken as asked, others rounded into that range; the host
    takes the answer's lease_ms."""
    for profile in PROFILES.values():
        ep = build(profile)
        assert struct.unpack_from("<I", Wire(ep).ok(0, m.OP_OPEN, struct.pack("<IB", asked, 0)))[0] == given
    hst = host_on(build())
    assert hst.open(asked).lease_ms == given == hst.lease_ms


def test_end_releases_the_lock_and_everything_the_session_made():
    """core §6.4, §8, §9: end releases the lock and every resource of the session (plan, connections, streams,
    subscriptions); its pins go back to idle."""
    ep = build()
    w = Wire(ep)
    w.open()
    plan = ep.fns[PLAN.name]
    w.ok(plan, PLAN.op["plan_apply"], assign(4, 1, 20))
    w.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 4))
    w.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
    assert ep.pin_state(20) == "gpio 4" and ep.subscribed
    w.ok(0, m.OP_END)
    assert ep.holder is None and not ep.plan and not ep.subscribed and ep.pin_state(20) == "idle hi-z"


def test_keepalive_only_extends_the_lease(ep):
    """core §6.4: keepalive restarts the lease and nothing else."""
    w = Wire(ep)
    w.open(2000, tail=m.tlv(OWNER, b"k"))
    ep.now.t = 1500
    before = w.lock_state()
    assert w.raw(0, m.OP_KEEPALIVE).succeeded
    assert before == (1, 500, b"k") and w.lock_state() == (1, 2000, b"k")


def test_lock_state_tells_any_holder_and_0_when_free(ep):
    """core §6.4: lock_state says whether any session holds the lock and its remaining ms (0 when free)."""
    w = Wire(ep)
    assert w.lock_state() == (0, 0, None)
    Wire(ep, 0x33).open(4000)
    ep.now.t = 1000
    assert w.lock_state()[:2] == (1, 3000)


def test_the_owner_is_kept_while_the_lock_is_and_shown(ep):
    """core §6.4: the owner (TLV 0x01, 1-32 bytes, not critical) comes from the open that takes the lock, is kept while
    it is held (the holder's open again does not change it), shown after lock_state and rejected locked, and
    forgotten when the lock ends; no owner, no TLV."""
    s = Wire(ep, 0x10)
    s.open(tail=m.tlv(OWNER, b"a" * 32))
    assert s.lock_state()[2] == b"a" * 32
    s.open(tail=m.tlv(OWNER, b"b"))
    assert s.lock_state()[2] == b"a" * 32
    s.raw(0, m.OP_END)
    assert s.lock_state() == (0, 0, None)
    Wire(ep, 0x11).open()
    r = rejected(Wire(ep, 0x12).open(), m.LOCKED)
    assert len(r.payload) == 4 and Wire(ep).lock_state()[2] is None


def test_the_host_sends_a_clean_owner_and_takes_none_back_as_no_failure():
    """core §6.4, §2.1: the host's owner is 1-32 bytes of text without control characters; a probe that keeps no owner
    is no failure."""
    assert h.owner_text("a\x1bb") == b"a?b" and len(h.owner_text("あ" * 20)) <= 32
    hst = h.Host(scripted(completed(struct.pack("<BI", 1, 10))))
    assert hst.lock_owner() == (True, 10, None)


def test_force_releases_the_old_sessions_resources_before_handing_over():
    """core §6.4, §9: force takes the lock from another session; the old session's resources are released as at its
    end first (its pins idle, its subscriptions over), nothing passes to the new one."""
    ep = build()
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open()
    plan = ep.fns[PLAN.name]
    a.ok(plan, PLAN.op["plan_apply"], assign(4, 1, 20))
    a.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 3))
    a.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
    assert b.open(force=1).succeeded
    assert not ep.plan and not ep.subscribed and ep.pin_state(20) == "idle hi-z"
    rejected(a.raw(0, m.OP_KEEPALIVE), m.LOCKED)


# ---- §7 discovery ----------------------------------------------------------------------------------------------------

def test_confirm_names_the_transport_it_came_on():
    """core §7.1, §7.5: confirm's TLV 0x01 transport is the index of the transport it came on - an entry of fn 0's
    describe on that transport."""
    for profile in PROFILES.values():
        ep = build(profile)
        indexes = {v[0] for t, v in describe(ep) if t == CORE.tlv["describe"]["transport"]}
        for t in indexes:
            p = Wire(ep, transport=t).ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)
            assert m.Tail.parse(p[16:]).get(0x01) == bytes([t])


def test_confirms_shape_is_the_same_for_every_revision_and_fits_64_bytes(ep):
    """core §7.1: confirm's header, fixed part and magics are those of every revision; its request and answer fit 64
    bytes."""
    req = m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([1, 200]), 0).pack()
    out = ep.handle(req, 0)
    assert len(req) <= 64 and len(out) <= 64
    assert out[5:9] == b"OEP!" and out[9] == 1 and len(out) == 5 + 16 + 4


def test_the_revision_is_chosen_per_transport():
    """core §7.1: the revision a confirm chose holds on the transport (or TCP connection) it came on."""
    ep = build()
    Wire(ep, transport=0).ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)
    ep.handle(m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", 0).pack(), 1, link="peer")
    assert set(ep.revision_in_use) == {0, "peer"}


def test_later_confirms_ask_for_the_revision_in_use():
    """core §7.1: after the first confirm on a transport the host asks min_rev = max_rev = the revision in use."""
    ep = build()
    sent = []
    hst = h.Host(lambda b: sent.append(b) or ep.handle(b, 0))
    hst.confirm()
    hst.confirm()
    assert m.Request.unpack(sent[0]).payload[4:] == bytes([h.MIN_REVISION, h.MAX_REVISION])
    assert m.Request.unpack(sent[1]).payload[4:] == b"\x01\x01"
    assert hst.confirm_body() == m.CONFIRM_REQUEST + b"\x01\x01"


def test_no_revision_in_range_is_unsupported_with_supported(ep):
    """core §7.1: no revision in the range: unsupported, tag 0x00 then TLV 0x01 supported (min, max); min_rev > max_rev
    is malformed."""
    w = Wire(ep)
    r = rejected(w.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x02\x05", sid=0), m.UNSUPPORTED)
    assert r.payload == b"\x00" + m.tlv(0x01, b"\x01\x01")
    rejected(w.raw(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x02\x01", sid=0), m.MALFORMED)
    hst = host_on(ep)
    with pytest.raises(h.Unsupported) as e:
        hst.confirm(2, 5)
    assert e.value.supported == (1, 1)


def test_list_pages_from_first_and_fits_every_frame():
    """core §7.2: list returns the interfaces from first, as many as one frame holds; total is their number; no entry
    is fn 0."""
    for profile in (vb.esp32_v003_64, vb.p4_x035):
        ep = build(profile)
        w, seen, first = Wire(ep), [], 0
        every = [e for e in entries(ep)[1]]
        while True:
            raw = ep.handle(w.bytes(0, m.OP_LIST, struct.pack("<H", first), sid=0), 0)
            assert len(raw) <= ep.probe.max_frame
            total, page = catalog.unpack_list_result(raw[5:])
            if not page:
                break
            if first + len(page) < total:                    # "as many as one frame holds": the next one does not fit
                assert len(raw) + len(catalog.pack_entry(every[first + len(page)])) > ep.probe.max_frame
            seen += page
            first += len(page)
        assert total == len(seen) == len(ep.names) - 1 and 0 not in [e.fn for e in seen]


def test_a_name_of_48_bytes_fits_the_smallest_frame():
    """core §7.2: names 1-48 bytes of a-z 0-9 - . ; one entry with a 48-byte name is a 62-byte answer."""
    entry = catalog.ListEntry(5, 0, 1, "io.github.x." + "a" * 36)
    assert len(entry.name) == 48
    names.validate(entry.name)
    assert 5 + 3 + len(catalog.pack_entry(entry)) == 62 <= reg.MIN_MAX_FRAME
    with pytest.raises(names.InvalidName):
        names.validate("io.github.x." + "a" * 37)


def test_instances_are_unique_per_name_and_revision():
    """core §7.2: instance tells interfaces of the same (name, revision) apart - unique within the pair."""
    for profile in (vb.p4_x035, vb.p4_bench, vb.esp32_v003):
        _, listed = entries(build(profile))
        pairs = [(e.name, e.revision, e.instance) for e in listed]
        assert len(pairs) == len(set(pairs))


def test_the_list_holds_for_the_boot_and_the_host_forgets_fns_at_a_reboot():
    """core §7.2, §6.5: list's answer is the same while boot_id is; the host may keep name -> fn until it changes."""
    ep = build()
    assert entries(ep) == entries(ep)
    hst = host_on(ep)
    hst.confirm()
    core.find(hst, GPIO.name)
    assert hst._fns
    ep.reboot()
    hst.confirm()
    assert not hst._fns


def test_list_from_first_past_total_is_total_and_count_0(ep):
    """core §7.2: first at or past total -> total and count 0."""
    total, _ = entries(ep)
    for first in (total, total + 1, 0xFFFF):
        assert Wire(ep).ok(0, m.OP_LIST, struct.pack("<H", first), sid=0)[:3] == struct.pack("<HB", total, 0)


def test_describe_paging_ends_with_more_0_and_nothing(ep):
    """core §7.3: first at or past the number of TLVs -> no TLVs and more 0; the host stops at more 0."""
    n = len(describe(ep))
    for first in (n, n + 5, 0xFFFF):
        assert Wire(ep).ok(0, m.OP_DESCRIBE, struct.pack("<HH", 0, first), sid=0) == b"\x00"


def test_the_common_describe_tags_have_their_forms():
    """core §7.4: role_channels role(u8) base(u16) bitmap; max_clock_hz / min_clock_hz / features u32; max_length u16;
    channel_group group(u8) n(u8) n x (role u8, channel u16); ops base(u8) bitmap."""
    sizes = {catalog.MAX_CLOCK_HZ: 4, reg.DESCRIBE_COMMON["min_clock_hz"]: 4, catalog.FEATURES: 4, catalog.MAX_LENGTH: 2}
    seen = set()
    for profile in (vb.p4_x035, vb.p4_bench, vb.esp32_v003, vb.rp2350_pins):
        ep = build(profile)
        for fn in [e.fn for e in entries(ep)[1]]:
            for t, v in describe(ep, fn):
                seen.add(t)
                if t in sizes:
                    assert len(v) == sizes[t], (fn, t)
                if t == catalog.ROLE_CHANNELS:
                    assert len(v) >= 4
                if t == catalog.CHANNEL_GROUP:
                    assert len(v) == 2 + 3 * v[1]
                if t == catalog.OPS:
                    assert not catalog.check_ops(v)
    assert set(reg.DESCRIBE_COMMON.values()) - {reg.DESCRIBE_COMMON["min_clock_hz"]} <= seen


def test_max_length_bounds_a_request():
    """core §7.4: a request past max_length is unsupported."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    conn = struct.unpack_from("<I", w.ok(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 2, 3, 4_000_000)))[0]
    limit = ep.block_max[2]
    r = rejected(w.raw(2, RV.op["read_block"], struct.pack("<IIH", conn, 0x2000_0000, limit // 4 + 1)), m.UNSUPPORTED)
    assert r.payload == b"\x00"
    assert w.raw(2, RV.op["read_block"], struct.pack("<IIH", conn, 0x2000_0000, limit // 4)).resolution == m.COMPLETED


def test_min_clock_hz_bounds_a_requested_speed():
    """core §7.4: min_clock_hz (u32) is the lowest clock the function runs at - a max_speed below it is unsupported; the
    host reads it as a u32."""
    probe = vb.p4_bench()
    probe.offered = [vb.Offered(o.fn, o.instance, o.name, o.tlvs + (catalog.u32(catalog.MIN_CLOCK_HZ, 100_000),),
                                o.revision, o.inner) if o.name == RVSWD.name else o for o in probe.offered]
    ep = endpoint.Endpoint(probe, Clock())
    v = next(v for t, v in describe(ep, 1) if t == catalog.MIN_CLOCK_HZ)
    assert len(v) == 4 and catalog.decode_description(m.tlv(catalog.MIN_CLOCK_HZ, v)).min_clock_hz == 100_000
    w = Wire(ep)
    w.open()
    assert rejected(w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 2, 3, 99_999)), m.UNSUPPORTED).payload == b"\x00"
    assert w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 2, 3, 100_000)).succeeded


def test_a_channel_group_is_matched_whole_and_role_channels_bound_their_roles():
    """core §7.4: with channel_group the chosen pins must be one group exactly; role_channels lists the candidates of
    the roles it names; a pin chosen any way (an attach's arguments, the plan) is checked against them."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    assert w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 2, 5, 4_000_000)).detail == m.UNSUPPORTED
    assert w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 4, 5, 4_000_000)).succeeded
    ep = build(vb.rp2350_pins)
    w = Wire(ep)
    w.open()
    assert w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 19, 3, 4_000_000)).detail == m.UNSUPPORTED
    assert w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 0, 1, 4_000_000)).succeeded
    plan = ep.fns[PLAN.name]
    rejected(w.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 19)), m.UNSUPPORTED)


def test_ops_value_form_and_the_examples():
    """core §7.4: ops is base(u8) and a bitmap of one byte or more, base + 8 x bytes <= 256; `07 02 00 10 FF` is ops
    0x10-0x17, `07 02 00 10 07` dmi / halt / resume, `F9 01` is not valid; the host reads the bitmap."""
    assert catalog.unpack_ops(b"\x10\xff") == set(range(0x10, 0x18))
    assert catalog.unpack_ops(b"\x10\x07") == {0x10, 0x11, 0x12}
    assert catalog.split_tlv(bytes.fromhex("07020010ff")) == [(0x07, b"\x10\xff")]
    for bad in (b"\xf9\x01", b"\x10", b""):
        assert catalog.check_ops(bad)
    assert not catalog.check_ops(b"\x00" + b"\x01" * 32) and not catalog.check_ops(b"\xf8\x01")
    probe = vb.p4_x035()
    probe.offered = [vb.Offered(o.fn, o.instance, o.name, (m.tlv(catalog.OPS, b"\xf9\x01"),) + o.tlvs[1:], o.revision,
                                o.inner) if o.name == GPIO.name else o for o in probe.offered]
    hst = host_on(endpoint.Endpoint(probe, Clock()))
    with pytest.raises(core.UnusableFunction):
        core.ops(hst, 4)


# ---- §7.5 probe-wide declarations ------------------------------------------------------------------------------------

def decl(ep, name, transport=0):
    tag = CORE.tlv["describe"][name]
    return [v for t, v in describe(ep, 0, transport) if t == tag]


def test_firmware_model_and_chip_are_text():
    """core §7.5: firmware, model and chip are text (optional)."""
    ep = build()
    for name in ("firmware", "model", "chip"):
        for v in decl(ep, name):
            v.decode("utf-8")
    assert decl(ep, "chip") and not decl(build(vb.p4_bench), "chip")


def test_unit_id_is_required_its_grammar_and_the_same_on_every_transport():
    """core §7.5: unit_id is required, 1-32 bytes of a-z 0-9 -, the same in the describe of every transport; it is
    made from the unit alone (not the transport kind)."""
    ep = build()
    values = {bytes(decl(ep, "unit_id", t)[0]) for t in range(4)}
    assert len(values) == 1
    v = values.pop()
    assert vb.UNIT_ID.fullmatch(v.decode())
    with pytest.raises(AssertionError):
        vb._core("1", "m", "BAD_ID", 0, {})


def test_channels_are_numbered_from_0():
    """core §7.5: channels is u16; channels are 0 to channels - 1; a probe without it has none."""
    ep = build()
    (v,) = decl(ep, "channels")
    assert len(v) == 2 and ep.channels == struct.unpack("<H", v)[0]
    plan = ep.fns[PLAN.name]
    w = Wire(ep)
    w.open()
    assert w.raw(plan, PLAN.op["plan_apply"], assign(4, 1, ep.channels)).detail == m.UNSUPPORTED
    assert not decl(build(vb.core_v1), "channels")


def test_labels_are_the_firmwares_fixed_names():
    """core §7.5: label is channel(u16) text - the firmware's fixed names; the host reads them as shown text."""
    ep = build()
    for v in decl(ep, "label"):
        assert struct.unpack_from("<H", v)[0] < ep.channels and v[2:].decode()
    assert core.probe_labels(host_on(ep))["LED"] == 51


def test_one_transport_entry_per_transport_with_its_kind_and_interface():
    """core §7.5: transport is index(u8) kind(u8) interface(u8), one per transport, required; a UART bridge or TCP has
    interface 0xFF; kinds 1-6."""
    ep = build()
    rows = [tuple(v) for v in decl(ep, "transport")]
    assert len({r[0] for r in rows}) == len(rows) == 4 and all(len(r) == 3 for r in rows)
    assert all(r[1] in CORE.enum["transport_kind"].values() for r in rows)
    assert CORE.enum["transport_kind"] == {"uart_bridge": 1, "usb_cdc": 2, "usb_serial_jtag": 3, "vendor_bulk": 4,
                                           "hid": 5, "tcp": 6}
    rows = [tuple(v) for v in decl(build(vb.esp32_v003), "transport")]
    assert rows == [(0, 1, 0xFF)]


def test_max_op_ms_is_required_and_in_range():
    """core §7.5: max_op_ms (u32) is required, 1-600000; a host does not use a probe outside it; an explicit wait past
    it is unsupported."""
    ep = build(vb.esp32_v003)
    (v,) = decl(ep, "max_op_ms")
    assert 1 <= struct.unpack("<I", v)[0] <= 600000
    assert h.check_max_op_ms(0) and h.check_max_op_ms(600001) and not h.check_max_op_ms(600000)
    w = Wire(ep)
    w.open()
    reset = m.tlv(RVSWD.tlv["attach"]["reset"], struct.pack("<HH", 23, 20000), critical=True)
    swio = ep.pairs[1][0]
    assert w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, *swio, 4_000_000) + reset).detail == m.UNSUPPORTED


def test_a_firmware_that_adds_a_transport_gives_it_an_unused_index():
    """core §7.5: transport indexes are kept; one added takes an index not used before."""
    ep = build(vb.rp2350_pins)
    before = set(ep.transports)
    index = ep.add_transport(CORE.enum["transport_kind"]["tcp"])
    assert index not in before and set(ep.transports) == before | {index}


def test_an_x_unit_id_keys_nothing_kept_across_sessions(tmp_path):
    """core §7.5: an `x-` unit_id is not unique; the host keys nothing kept across sessions by it (the link-speed record
    keeps nothing for it) and does not name a probe by it."""
    from oep_client import speed_record
    rec = speed_record.SpeedRecord(tmp_path / "speeds.json")
    rec.note("/dev/ttyUSB0", "x-esp32", 921600, True)
    rec.note("/dev/ttyUSB0", "fafe00000003", 921600, True)
    assert rec.lookup("/dev/ttyUSB0", "x-esp32") == ([], []) and rec.results("/dev/ttyUSB0", "x-esp32") == {}
    assert rec.lookup("/dev/ttyUSB0", "fafe00000003") == ([921600], [])
    assert all("x-" not in key for key in rec.data)


# ---- §7.6 clock ------------------------------------------------------------------------------------------------------

def test_clock_gives_the_boot_id_of_confirm_and_open_and_the_uptime(ep):
    """core §7.6, §6.5: clock's boot_id is confirm's and open's; uptime_ns the probe's clock read for this request."""
    w = Wire(ep)
    ep.now.t = 1234
    boot, up = struct.unpack("<IQ", w.ok(0, m.OP_CLOCK, sid=0))
    conf = struct.unpack_from("<I", w.ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0), 12)[0]
    opened = struct.unpack_from("<I", w.ok(0, m.OP_OPEN, struct.pack("<IB", 0, 0)), 4)[0]
    assert boot == conf == opened == ep.boot_id and up == 1_234_000_000
    ep.now.t = 1300
    assert struct.unpack("<IQ", w.ok(0, m.OP_CLOCK, sid=0))[1] == 1_300_000_000


def test_clock_is_read_at_the_request_mutant(monkeypatch):
    """core §7.6 mutant: a clock answering a value read earlier is caught by the check above."""
    ep = build()
    frozen = ep.now_ns()
    monkeypatch.setattr(ep, "now_ns", lambda: frozen)
    with pytest.raises(AssertionError):
        test_clock_gives_the_boot_id_of_confirm_and_open_and_the_uptime(ep)


# ---- §8 channels -----------------------------------------------------------------------------------------------------

def cfg_set(w, ep, *items):
    return w.raw(ep.fns[CFG.name], CFG.op["set"], bytes([len(items)]) + b"".join(items))


def idle_item(ch, mode):
    return m.tlv(__import__("oep_client.config", fromlist=["ITEM"]).ITEM["idle"], struct.pack("<HB", ch, mode))


def test_the_idle_state_is_the_settings_or_hi_z():
    """core §8: a channel's idle state is its idle setting when there is one (an output level driven), else Hi-Z."""
    ep = build()
    w = Wire(ep)
    w.open()
    assert ep.pin_state(30) == "idle hi-z"
    assert cfg_set(w, ep, idle_item(30, CFG.enum["idle_mode"]["output_high"])).succeeded
    assert ep.pin_state(30) == "idle output-high"


def test_a_released_pin_returns_to_idle_whichever_way():
    """core §8, §9: a pin released by plan_release, end, lease expiry or force goes back to its idle state."""
    for way in ("release", "end", "expiry", "force"):
        ep = build()
        w = Wire(ep)
        w.open(1000)
        plan = ep.fns[PLAN.name]
        w.ok(plan, PLAN.op["plan_apply"], assign(4, 1, 20))
        w.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 4))
        assert ep.pin_state(20) == "gpio 4"
        if way == "release":
            w.ok(plan, PLAN.op["plan_release"], b"\x00")
        elif way == "end":
            w.ok(0, m.OP_END)
        elif way == "expiry":
            ep.now.t = 5000
            Wire(ep).raw(0, m.OP_CLOCK, sid=0)
        else:
            Wire(ep, 0x99).open(force=1)
        assert ep.pin_state(20) == "idle hi-z", way


def test_taking_a_pin_keeps_its_idle_state_until_used():
    """core §8: taking a pin changes nothing electrical; it stays idle until its interface starts to use it."""
    ep = build()
    w = Wire(ep)
    w.open()
    w.ok(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 20))
    assert ep.pin_state(20) == "idle hi-z"
    w.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 3))
    assert ep.pin_state(20) == "gpio 3"


def test_taking_an_output_idle_channel_is_unavailable_cause_5():
    """core §8: a request taking a channel whose idle is an output is unavailable cause 5, unless the interface says it
    may be taken."""
    ep = build()
    w = Wire(ep)
    w.open()
    cfg_set(w, ep, idle_item(30, CFG.enum["idle_mode"]["output_low"]))
    r = rejected(w.raw(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(5, 1, 30)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "held_by_settings" and not ep.plan


def test_a_conflicting_take_is_unavailable_and_changes_nothing():
    """core §8.1: whatever takes a resource checks the pins held, the runtime resources and the settings first; a clash
    is unavailable (cause 1 for a pin) and nothing that runs changes."""
    ep = build()
    w = Wire(ep)
    w.open()
    plan = ep.fns[PLAN.name]
    w.ok(plan, PLAN.op["plan_apply"], assign(5, 1, 20))
    before = sorted(ep.plan)
    r = rejected(w.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 20) + assign(4, 1, 22)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "pin_in_use" and sorted(ep.plan) == before


def test_more_than_the_probe_can_run_at_once_is_unavailable_cause_2():
    """core §8.1: an attach past the seats the wire has is unavailable cause 2, before any side effect."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    for pair in ((2, 3), (4, 5)):
        w.ok(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, *pair, 4_000_000))
    conns = dict(ep.conns)
    r = rejected(w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 6, 7, 4_000_000)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "limit" and ep.conns == conns


# ---- §9 resources ----------------------------------------------------------------------------------------------------

def attach(w, pair=(2, 3)):
    return struct.unpack_from("<I", w.ok(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, *pair, 4_000_000)))[0]


def test_end_and_expiry_release_resources_and_subscriptions_and_keep_the_order_state():
    """core §9: end or lease expiry - every runtime resource released, subscriptions ended, the order state kept."""
    for way in ("end", "expiry"):
        ep = build(lambda: vb.p4_bench())
        w = Wire(ep)
        w.open(1000)
        conn = attach(w)
        data = w.bytes(0, m.OP_LOCK_STATE)
        first = ep.handle(data, 0)
        if way == "end":
            w.ok(0, m.OP_END)
        else:
            ep.now.t = 5000
            Wire(ep).raw(0, m.OP_CLOCK, sid=0)
        assert not ep.conns and ep.handle(data, 0) == first
        w2 = Wire(ep, 0x77)
        w2.open()
        rejected(w2.raw(2, RV.op["halt"], struct.pack("<I", conn)), m.NO_RESOURCE)


def test_a_force_open_releases_and_replaces_the_resend_state():
    """core §9: another id's force open releases every resource and subscription; its success starts a new resend
    state."""
    ep = build()
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open()
    a.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
    data = a.bytes(0, m.OP_KEEPALIVE)
    ep.handle(data, 0)
    b.open(force=1)
    assert not ep.subscribed and ep.last == 0x20
    rejected(m.Result.unpack(ep.handle(data, 0)), m.LOCKED)


def test_the_holders_open_again_keeps_resources_subscriptions_and_resend_state():
    """core §9, §6.2: a new open of the holding id keeps the resources, the subscriptions (and their transport) and the
    resend state; the same open sent again changes nothing and gets its recorded answer."""
    ep = build(vb.p4_x035)
    w = Wire(ep)
    w.open()
    plan = ep.fns[PLAN.name]
    w.ok(plan, PLAN.op["plan_apply"], assign(4, 1, 20))
    w.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
    data = w.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    again = w.bytes(0, m.OP_OPEN, struct.pack("<IB", 3000, 0))
    answer = ep.handle(again, 0)
    assert ep.plan and 7 in ep.subscribed and ep.handle(data, 0) == first
    assert ep.handle(again, 0) == answer and ep.plan and 7 in ep.subscribed


def test_a_reboot_invalidates_everything():
    """core §9, §6.5: a restart: a new boot_id; resources, subscriptions, the resend state and the session gone."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    conn = attach(w)
    old = ep.boot_id
    ep.reboot()
    assert ep.boot_id != old and not ep.conns and ep.holder is None
    rejected(w.raw(0, m.OP_KEEPALIVE), m.NO_SESSION)
    w2 = Wire(ep, 0x44)
    w2.open()
    rejected(w2.raw(2, RV.op["halt"], struct.pack("<I", conn)), m.NO_RESOURCE)


def test_resource_numbers_are_never_reused_and_run_out_with_cause_2():
    """core §9: one u32 space from 1, a closed number never given again in the boot; past 0xFFFFFFFF a new resource is
    unavailable cause 2 while the existing ones work."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    a = attach(w)
    w.ok(1, RVSWD.op["detach"], struct.pack("<I", a))
    b = attach(w)
    assert b > a
    ep._next_resource = 0x1_0000_0000
    r = rejected(w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 4, 5, 4_000_000)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "limit"
    assert w.raw(2, RV.op["halt"], struct.pack("<I", b)).succeeded


def test_a_closed_or_unknown_number_is_no_resource_and_another_kind_cause_6():
    """core §9: a closed or unknown number is no_resource; a resource of a kind the op does not take is unavailable
    cause 6."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    a = attach(w)
    con = ep.fns["oep.target.console"]
    stream = struct.unpack_from("<I", w.ok(con, reg.TARGET_CONSOLE.op["open"], struct.pack("<IB", a, ep.mechanism_number("sdi"))))[0]
    r = rejected(w.raw(2, RV.op["halt"], struct.pack("<I", stream)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "wrong_state"
    w.ok(1, RVSWD.op["detach"], struct.pack("<I", a))
    rejected(w.raw(2, RV.op["halt"], struct.pack("<I", a)), m.NO_RESOURCE)
    rejected(w.raw(2, RV.op["halt"], struct.pack("<I", 4321)), m.NO_RESOURCE)


def test_a_failed_creation_returns_no_number():
    """core §9: a request that fails to create gives no usable number (a refused attach, then the next is not past a
    number in use)."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    r = w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 2, 5, 4_000_000))
    assert r.resolution == m.REJECTED and r.payload[:1] != b"\x01"
    assert not ep.conns
    a = attach(w)
    assert ep.conns and a in ep.conns


def test_a_target_is_always_named_by_its_resource_number():
    """core §9: the target is named by fn and resource number in every request - no "current target": two connections
    are reached by their numbers, an unknown one is no_resource."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    a, b = attach(w, (2, 3)), attach(w, (4, 5))
    assert w.raw(2, RV.op["halt"], struct.pack("<I", a)).succeeded
    assert w.raw(2, RV.op["halt"], struct.pack("<I", b)).succeeded
    rejected(w.raw(2, RV.op["halt"], struct.pack("<I", b + 10)), m.NO_RESOURCE)


# ---- §10 notifications -----------------------------------------------------------------------------------------------

def test_notifications_do_not_keep_the_host_past_its_deadline():
    """core §10.1: a probe that keeps pushing cannot hold the host's reading past its deadline."""
    import time

    class Flood(FakeStream):
        def read(self, n=1):
            from oep_client import cobs
            self.incoming += cobs.frame(bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 7, 0, 1))
            return super().read(n)
    lk = link.SerialLink.on_stream(Flood(), framing="cobs", timeout=0.2)
    t0 = time.monotonic()
    lk.pump(0.1)
    assert time.monotonic() - t0 < 1.0
    with pytest.raises(TimeoutError):
        lk._recv_for(9)
    assert time.monotonic() - t0 < 2.0


def test_seq_counts_per_fn_from_0_at_each_subscribe_and_wraps():
    """core §10.2: seq is per fn (data and events alike), 0 at every subscribe, a u16 that wraps."""
    ep, hst, lc, clock = streaming()
    clock.t += 3
    seqs = [struct.unpack_from("<H", f, 3)[0] for f in ep.pushes() if struct.unpack_from("<H", f, 1)[0] == lc.fn]
    assert seqs == list(range(len(seqs))) and seqs
    hst.subscribe(lc.fn)
    assert ep.push_seq[lc.fn] == 0
    ep.push_seq[lc.fn] = 0xFFFF
    assert ep._next_seq(lc.fn) == 0xFFFF and ep._next_seq(lc.fn) == 0


def test_fn_0_sends_no_notifications():
    """core §10.2: fn 0 sends no notifications; its 0x01 / 0x02 are confirm and list, and the host never subscribes it."""
    ep = build()
    w = Wire(ep)
    w.open()
    rejected(w.raw(0, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0)), m.MALFORMED)
    assert all(struct.unpack_from("<H", f, 1)[0] != 0 for f in ep.pushes())
    with pytest.raises(ValueError):
        host_on(ep).subscribe(0)


def test_subscribe_and_unsubscribe_forms_and_lock():
    """core §10.3: subscribe min_bytes(u16) max_delay_ms(u32) [TLV], unsubscribe [TLV]; both need the lock and answer
    no fixed part."""
    ep = build()
    w = Wire(ep)
    rejected(w.raw(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), sid=0), m.SESSION_REQUIRED)
    rejected(w.raw(7, m.OP_UNSUBSCRIBE, sid=0), m.SESSION_REQUIRED)
    w.open()
    rejected(w.raw(7, m.OP_SUBSCRIBE, struct.pack("<H", 0)), m.MALFORMED)
    assert w.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 1, 2) + m.tlv(0x3E, b"")) == b""
    assert ep.subscribed[7] == (1, 2)
    assert w.ok(7, m.OP_UNSUBSCRIBE, m.tlv(0x3E, b"")) == b"" and 7 not in ep.subscribed


def test_only_the_holder_subscribes_and_the_subscription_ends_with_the_lock():
    """core §10.3: only the lock's holder subscribes; the subscription ends with the lock (end, expiry, force)."""
    for way in ("end", "expiry", "force"):
        ep = build()
        w, o = Wire(ep, 0x10), Wire(ep, 0x20)
        w.open(1000)
        rejected(o.raw(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0)), m.LOCKED)
        w.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
        if way == "end":
            w.ok(0, m.OP_END)
        elif way == "expiry":
            ep.now.t = 5000
            Wire(ep).raw(0, m.OP_CLOCK, sid=0)
        else:
            o.open(force=1)
        assert not ep.subscribed, way


def test_unsubscribe_without_a_subscription_succeeds():
    """core §10.3: unsubscribe of an fn with no subscription does nothing and succeeds."""
    ep = build()
    w = Wire(ep)
    w.open()
    assert w.raw(7, m.OP_UNSUBSCRIBE).succeeded and not ep.subscribed


def test_subscribe_again_replaces_atomically():
    """core §10.3: subscribing an fn again replaces its subscription - conditions, transport, seq from 0; its unsent
    notifications are dropped; one subscription per fn."""
    ep, hst, lc, clock = streaming()
    clock.t += 1
    assert frames(ep) and ep.push_seq[lc.fn] > 0
    ep.handle(m.Request(500, lc.fn, m.OP_SUBSCRIBE, struct.pack("<HI", 7, 9), hst.session).pack(), 1, link="other")
    assert ep.subscribed[lc.fn] == (7, 9) and ep.push_link[lc.fn] == "other" and ep.push_seq[lc.fn] == 0
    assert list(ep.subscribed).count(lc.fn) == 1


def test_subscription_conditions_are_kept_as_asked():
    """core §10.3: a subscription's conditions are never rounded silently - what was asked is what holds."""
    ep = build()
    w = Wire(ep)
    w.open()
    w.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0xFFFF, 0xFFFFFFFF))
    assert ep.subscribed[7] == (0xFFFF, 0xFFFFFFFF)


def test_data_waits_for_min_bytes_or_max_delay_and_events_never_wait():
    """core §10.3: data goes when min_bytes are waiting or max_delay_ms after its first byte (0: that condition unused;
    both 0: at once); events go at once whatever these say."""
    assert endpoint.push_due(0, 0, 0, 1, False)
    assert not endpoint.push_due(100, 0, 10**6, 99, False) and endpoint.push_due(100, 0, 0, 100, False)
    assert not endpoint.push_due(0, 50, 49, 1, False) and endpoint.push_due(0, 50, 50, 1, False)
    assert not endpoint.push_due(0, 0, 0, 0, False)
    ep, hst, lc, clock = streaming(min_bytes=60000, max_delay_ms=0)
    clock.t += 1
    assert not frames(ep, m.ROLE_DATA)
    lc.stop()
    assert frames(ep, m.ROLE_EVENT)


def test_notifications_go_to_the_transport_the_subscribe_came_on():
    """core §10.4: an fn's notifications go to the transport of its subscribe."""
    ep, hst, lc, clock = streaming()
    assert ep.push_link[lc.fn] == 0
    ep.handle(m.Request(600, lc.fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), hst.session).pack(), 2, link="tcp-1")
    clock.t += 2
    assert {to for to, _ in ep.pushes_to()} == {"tcp-1"}


# ---- §11 fn 0's ops --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("op, body, locked", [
    (m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", False), (m.OP_LIST, b"\0\0", False),
    (m.OP_DESCRIBE, b"\0\0\0\0", False), (m.OP_CLOCK, b"", False), (m.OP_END, b"", True),
    (m.OP_KEEPALIVE, b"", True), (m.OP_LOCK_STATE, b"", False)],
    ids=["confirm", "list", "describe", "clock", "end", "keepalive", "lock_state"])
def test_fn_0_op_table(ep, op, body, locked):
    """core §11: each fn 0 op with its request, whether it needs the lock, and that it is there (all required)."""
    r = Wire(ep).raw(0, op, body, sid=0)
    if locked:
        rejected(r, m.SESSION_REQUIRED)
    else:
        assert r.succeeded
    assert op in CORE.op.values() and (op in CORE.lock_free) != locked


def test_fn_0_open_row(ep):
    """core §11: open lease_ms(u32) force(u8) [TLV owner] with the id in the header -> lease_ms(u32) boot_id(u32)
    [TLV]; open takes the lock."""
    p = Wire(ep).ok(0, m.OP_OPEN, struct.pack("<IB", 2000, 0) + m.tlv(OWNER, b"o"))
    assert struct.unpack_from("<II", p) == (2000, ep.boot_id) and ep_holder(ep) == SID


# ---- §12 extension rules ---------------------------------------------------------------------------------------------

def test_every_interface_has_a_name_revision_and_op_table():
    """core §12 item 2: each interface defines its name and revision, its op table (number, lock, required or not) and
    each op's TLVs in that op's context."""
    for i in reg.INTERFACES.values():
        names.validate(i.name)
        assert i.revision >= 1 and i.op
        assert i.target is None or isinstance(i.target, str) and i.target           # its target family, or generic
        assert i.lock_free <= set(i.op.values())
        for ctx in i.tlv:
            base = ctx.removesuffix("_answer").removesuffix("_payload")
            assert (base in i.op or base in i.event or ctx in ("describe", "item", "data")
                    or base in ("unavailable", "unsupported")), (i.name, ctx)


def test_an_interfaces_own_values_and_describe_tags_are_in_its_range():
    """core §12 item 2: an interface's own status / reason / cause values and describe tags are 0x40-0x7F."""
    for i in reg.INTERFACES.values():
        for t in i.tlv.get("describe", {}).values():
            assert 0x40 <= t <= 0x7F, i.name
        for ctx in ("unavailable_payload", "unsupported_payload"):
            for t in i.tlv.get(ctx, {}).values():
                assert 0x40 <= t <= 0x7F, i.name


def test_an_interface_with_events_declares_subscribe_and_unsubscribe():
    """core §12 item 2, §10.3: an interface that sends notifications declares subscribe / unsubscribe and its kinds."""
    for i in reg.INTERFACES.values():
        if i.event:
            assert {"subscribe", "unsubscribe"} <= set(i.op), i.name


def test_a_lock_free_op_changes_no_state():
    """core §12 rule 5, §6.3: an op an interface lists as lock-free changes nothing - every one sent with session_id 0
    leaves the lock, the plan, the pins and the subscriptions as they were."""
    ep = build()
    snapshot = lambda: (ep.holder, sorted(ep.plan), dict(ep.subscribed), [ep.pin_state(c) for c in range(55)])  # noqa
    before = snapshot()
    w = Wire(ep)
    for fn, name in ep.names.items():
        i = reg.INTERFACES.get(name)
        for op in (i.lock_free if i else ()):
            for body in (b"", b"\0" * 4, b"\0" * 12):
                r = w.raw(fn, op, body, sid=0)
                assert r.detail != m.SESSION_REQUIRED or r.resolution == m.COMPLETED, (name, op)
    assert snapshot() == before


def test_the_bench_adds_nothing_to_the_projects_interfaces():
    """core §12 rule 7: no ops, tags or values are added to another's interface - the bench's oep. interfaces offer
    only their registry ops; its own experiments live under its own name."""
    for profile in (vb.p4_x035, vb.p4_bench, vb.esp32_v003, vb.rp2350_pins):
        ep = build(lambda p=profile: vb.with_stand_in(p()))
        for fn, name in ep.names.items():
            if fn == m.CORE_FN:
                continue
            if name.startswith("oep."):
                assert ep.ops[fn] <= set(reg.INTERFACES[name].op.values()), name
            else:
                assert not name.startswith("oep.") and name.startswith(vb.NS)


def test_a_reserved_value_in_a_request_is_unsupported():
    """core §12.1, §2.5: a request value a later revision may define is refused unsupported by today's probe - gpio mode
    7 (fixed part: tag 0x00)."""
    ep = build()
    w = Wire(ep)
    w.open()
    w.ok(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 20))
    r = rejected(w.raw(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 7)), m.UNSUPPORTED)
    assert r.payload[:1] == b"\x00"


def test_an_enum_answer_value_never_moves_a_field():
    """core §12.1: an unknown value never changes where fields are - the host reads the rest of an unavailable payload
    the same whatever its cause."""
    for cause in (1, 0x3F, 0x7E):
        una = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x01, bytes([cause])) + m.tlv(0x02, b"\x09\x00")))
        assert una.channels == [9]


# ---- mutants: each breaks the bench or the client the way a rule above forbids; the rule's check then fails ----------

def test_bit_7_alias_in_an_answer_mutant(monkeypatch):
    """core §2.2 mutant: a host that reads an answer's TLV with bit 7 set as its low 7 bits is caught."""
    monkeypatch.setattr(h.Unavailable, "_first",
                        lambda self, tag, size: next((v for t, v in self.tlvs if t & 0x7F == tag), None))
    with pytest.raises(AssertionError):
        test_bit_7_is_the_critical_mark_in_requests_and_an_unknown_tag_in_answers(build())


def test_later_duplicate_unchecked_mutant(monkeypatch):
    """core §2.3 mutant: a probe that checks only the first copy of a non-repeating tag is caught."""
    orig = endpoint.Take.tail

    def first_only(self, known=frozenset(), repeats=frozenset(), defer=False):
        got = orig(self, known, repeats, defer)
        seen = set()
        self.repeated = [(t, v) for t, v in self.repeated if not (t & 0x7F in seen or seen.add(t & 0x7F))]
        return got
    monkeypatch.setattr(endpoint.Take, "tail", first_only)
    with pytest.raises(AssertionError):
        test_a_tag_twice_the_first_is_used_and_the_later_is_checked_too(build())


def test_header_order_mutant(monkeypatch):
    """core §4.3 mutant: a probe that checks session_required before unknown_operation is caught."""
    orig = endpoint.Endpoint._header

    def swapped(self, req):
        if req.corr and req.fn == m.CORE_FN and not req.session and not self._lock_free(req.fn, req.op):
            return m.SESSION_REQUIRED
        return orig(self, req)
    monkeypatch.setattr(endpoint.Endpoint, "_header", swapped)
    with pytest.raises(AssertionError):
        test_the_header_order_corr_fn_op_session(build())


def test_no_order_state_mutant(monkeypatch):
    """core §5.1 mutant: a probe that keeps no last corr runs an old request again - caught."""
    monkeypatch.setattr(endpoint.Endpoint, "_resent", lambda self, req, data: None)
    with pytest.raises(AssertionError):
        test_the_probe_keeps_the_last_corr_and_refuses_one_not_ahead(build())


def test_end_clears_the_record_mutant(monkeypatch):
    """core §5.1, §9 mutant: a probe whose end drops the resend state is caught."""
    orig = endpoint.Endpoint._release_lock

    def clearing(self):
        orig(self)
        self.resend.clear()
        self.newest_corr = None
    monkeypatch.setattr(endpoint.Endpoint, "_release_lock", clearing)
    with pytest.raises(AssertionError):
        test_end_and_expiry_keep_the_order_state_and_the_same_id_open_keeps_it(build())


def test_decision_table_mutant(monkeypatch):
    """core §6.2 mutant: a probe that runs another id's requests while the lock is held is caught."""
    monkeypatch.setattr(endpoint.Endpoint, "_check",
                        lambda self, session: None if self.holder is not None else (m.REJECTED, m.NO_SESSION, b""))
    with pytest.raises(AssertionError):
        test_the_decision_table("full")


def test_owner_changed_by_the_holders_open_mutant(monkeypatch):
    """core §6.4 mutant: a probe that takes the owner of the holder's later open is caught."""
    orig = endpoint.Endpoint._open

    def retitled(self, t, req):
        out = orig(self, t, req)
        if out[0] == m.COMPLETED:
            self.owner = dict(m.split_tlvs(req.payload[5:])).get(OWNER, self.owner)
        return out
    monkeypatch.setattr(endpoint.Endpoint, "_open", retitled)
    with pytest.raises(AssertionError):
        test_the_owner_is_kept_while_the_lock_is_and_shown(build())


def test_resource_number_reused_mutant(monkeypatch):
    """core §9 mutant: a probe that gives a closed number again within the boot is caught."""
    def lowest(self, kind):
        n = 1
        while n in self.conns or n in self.streams:
            n += 1
        self.resources[n] = kind
        return n
    monkeypatch.setattr(endpoint.Endpoint, "_new_resource", lowest)
    with pytest.raises(AssertionError):
        test_resource_numbers_are_never_reused_and_run_out_with_cause_2()


def test_subscription_outlives_the_lock_mutant(monkeypatch):
    """core §10.3 mutant: a probe whose subscriptions survive the end of the lock is caught."""
    orig = endpoint.Endpoint._release_lock

    def keeping(self):
        subs = dict(self.subscribed)
        orig(self)
        self.subscribed.update(subs)
    monkeypatch.setattr(endpoint.Endpoint, "_release_lock", keeping)
    with pytest.raises(AssertionError):
        test_only_the_holder_subscribes_and_the_subscription_ends_with_the_lock()


def test_resubscribe_keeps_seq_mutant(monkeypatch):
    """core §10.3 mutant: a subscribe again that does not start seq from 0 is caught."""
    orig = endpoint.Endpoint._subscription

    def keep_seq(self, fn, op, t):
        seq = self.push_seq.get(fn)
        out = orig(self, fn, op, t)
        if seq is not None:
            self.push_seq[fn] = seq
        return out
    monkeypatch.setattr(endpoint.Endpoint, "_subscription", keep_seq)
    with pytest.raises(AssertionError):
        test_subscribe_again_replaces_atomically()


def test_pin_left_driven_after_release_mutant(monkeypatch):
    """core §8 mutant: a probe that leaves a released pin in its interface's drive is caught."""
    orig = endpoint.Endpoint._release_lock

    def driven(self):
        modes = dict(self.gpio_modes)
        plan = set(self.plan)
        orig(self)
        self.plan |= plan
        self.gpio_modes.update(modes)
    monkeypatch.setattr(endpoint.Endpoint, "_release_lock", driven)
    with pytest.raises(AssertionError):
        test_end_releases_the_lock_and_everything_the_session_made()


def test_lease_clamp_mutant(monkeypatch):
    """core §6.4 mutant: a probe that takes a lease below lease_min_ms as asked is caught."""
    monkeypatch.setitem(reg.LIMITS, "lease_min_ms", 1)
    with pytest.raises(AssertionError):
        test_lease_ms_is_taken_or_rounded_into_range(1, 1000)


def test_session_0_touches_the_lease_mutant(monkeypatch):
    """core §4.1 mutant: a probe that restarts the holder's lease on a session-0 request is caught."""
    orig = endpoint.Endpoint.handle

    def renewing(self, data, transport=0, link=None):
        out = orig(self, data, transport, link)
        if self.holder is not None and len(data) >= 10 and not struct.unpack_from("<I", data, 6)[0]:
            self.expires_ms = self.now() + self.lease_ms
        return out
    monkeypatch.setattr(endpoint.Endpoint, "handle", renewing)
    with pytest.raises(AssertionError):
        test_a_lock_free_op_with_0_touches_no_session_and_with_an_id_is_checked(build())


def test_confirm_transport_mutant():
    """core §7.1 mutant: a probe naming transport 0 whatever it came on is caught."""
    ep = build()
    orig = ep._confirm

    def fixed(t):
        out = orig(t)
        return out[0], out[1], out[2][:-1] + b"\x00"
    ep._confirm = fixed
    with pytest.raises(AssertionError):
        for t in (0, 1):
            p = Wire(ep, transport=t).ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)
            assert m.Tail.parse(p[16:]).get(0x01) == bytes([t])


def test_a_describe_tlv_of_another_length_is_not_used_and_a_tag_twice_uses_the_first():
    """core §2.3: an answer's TLV whose value is not its defined length is broken - the host does not use it (it does
    not fail on it either); of a tag the definition does not repeat, the first copy is used - a broken first copy is
    not used, and a later copy never stands in for it (max_length below)."""
    from oep_client import catalog
    data = (catalog.u32(catalog.MAX_CLOCK_HZ, 1_000_000) + catalog.u32(catalog.MAX_CLOCK_HZ, 2_000_000)
            + catalog.tlv(catalog.MIN_CLOCK_HZ, b"\x01\x02") + catalog.tlv(catalog.MAX_LENGTH, b"\x01")
            + catalog.u16(catalog.MAX_LENGTH, 64))
    d = catalog.decode_description(data)
    assert d.max_clock_hz == 1_000_000 and d.min_clock_hz is None and d.max_length is None
    assert d.broken == [catalog.MIN_CLOCK_HZ, catalog.MAX_LENGTH]


# ---- review 2026-10-15: rules the trace above checked only in part ----------------------------------------------------

def test_an_implemented_tlvs_unused_value_is_unsupported_with_the_tag_as_received():
    """core §2.3: an implemented request TLV is treated alike with and without bit 7 - a wrong length is malformed, a
    value the definition leaves unused (rvswd attach's idle_clock 2) is unsupported naming the tag as received (0x04,
    or 0x84 with bit 7); nothing is made either way."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    body = struct.pack("<BHHI", 1, 2, 3, 4_000_000)
    tag = RVSWD.tlv["attach"]["idle_clock"]
    for critical in (False, True):
        r = rejected(w.raw(1, RVSWD.op["attach"], body + m.tlv(tag, b"\x02", critical=critical)), m.UNSUPPORTED)
        assert r.payload[:1] == bytes([tag | (0x80 if critical else 0)])
        rejected(w.raw(1, RVSWD.op["attach"], body + m.tlv(tag, b"\x00\x00", critical=critical)), m.MALFORMED)
    assert not ep.conns
    assert w.raw(1, RVSWD.op["attach"], body + m.tlv(tag, b"\x01", critical=True)).succeeded


def test_the_client_takes_an_unknown_status_for_a_failure():
    """core §2.4: an interface status the host does not know is a failure - even beside outcome success (the client's
    riscv.check raises, naming it unknown)."""
    from oep_client import riscv
    ok = m.Result(1, m.COMPLETED, m.SUCCESS, b"")
    riscv.check("halt", ok, riscv.OK)
    with pytest.raises(riscv.TargetError, match="unknown status 0x55"):
        riscv.check("halt", ok, 0x55)


def test_the_client_ignores_reserved_flag_bits_and_shows_an_unknown_scan_kind(monkeypatch):
    """core §2.4: reserved bits of an answer's flags are ignored (attach's flags with bits 4-7 set read as the defined
    bits say); an unknown value of an enum that tells no failure (scan's entry kind 0x7E) is shown as it is."""
    from oep_client import riscv
    ep = build(vb.p4_bench)
    hst = host_on(ep)
    hst.open()
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    real = wire._call

    def bent(op, body=b"", **kw):
        r = real(op, body, **kw)
        p = bytearray(r.payload)
        if op == wire.ATTACH:
            p[8] |= 0xF0                                                  # conn(4) status(4) flags(1): bits 4-7
        elif op == wire.SCAN and p[1]:
            p[2] = 0x7E                                                   # tried count, then kind of the first row
        return m.Result(r.corr, r.resolution, r.detail, bytes(p))
    monkeypatch.setattr(wire, "_call", bent)
    found = wire.scan([(2, 3)])
    assert [f.kind for f in found] == [0x7E] and found[0].pins == (2, 3)
    conn, _ = wire.attach(halt=True, pins=(2, 3), max_speed=4_000_000)
    assert conn in ep.conns and wire.halted and wire.flags & 0xF0 == 0xF0


def test_session_0_requests_never_move_the_sessions_corr_half_the_space():
    """core §4.1: the host moves each session_id's corr on by 1-32767; many session_id 0 queries (one counter for every
    session_id) never take the session's next request half the space or more past its last one - it is still taken
    as new, never result_lost."""
    ep = build()
    sent = []
    hst = h.Host(lambda b: sent.append(m.Request.unpack(b)) or ep.handle(b, 0), rng=random.Random(4))
    hst.open()
    for _ in range(40000):
        hst.lock_state()
    hst.keepalive()
    last = None
    for r in sent:
        if r.session == hst.session:
            assert last is None or 1 <= (r.corr - last) % 0x10000 <= 0x7FFF
            last = r.corr
    assert len({r.corr for r in sent[-5:]}) == 5 and 0 not in {r.corr for r in sent}


def test_a_copy_of_a_request_not_yet_answered_is_dropped_and_touches_no_lease():
    """core §4.4: a copy of a request still waiting, running, or answered but not yet sent on the same transport is
    dropped unexamined - not counted, not run, no lease restarted by it; the same for session_id 0."""
    ep = build()
    w = Wire(ep)
    w.open(1000)
    data = w.bytes(0, m.OP_KEEPALIVE)
    assert ep.receive(data) == "queued"
    ep.now.t = 400
    assert ep.receive(data) == "coalesced" and ep.expires_ms == 1000
    before = ep.executions
    ep.process()
    assert ep.expires_ms == 1400 and ep.executions == before + 1
    ep.now.t = 900
    assert ep.receive(data) == "coalesced"                                     # answered, not yet sent
    assert ep.expires_ms == 1400 and ep.credits() == {0: 1}
    ep.sent()
    zero = w.bytes(0, m.OP_CLOCK, sid=0)
    assert ep.receive(zero) == "queued" and ep.receive(zero) == "coalesced" and ep.credits() == {0: 1}


def test_the_holders_open_from_another_transport_keeps_where_the_subscriptions_send():
    """core §6.2, §9: the holder's open again - from another transport too - renews the lease and keeps the resources,
    the resend state and where each subscription sends; a subscribe again moves it (§10.3)."""
    ep, hst, lc, clock = streaming()
    assert ep.push_link[lc.fn] == 0
    again = m.Request(700, 0, m.OP_OPEN, struct.pack("<IB", 3000, 0), hst.session).pack()
    assert m.Result.unpack(ep.handle(again, 2, link="tcp-1")).succeeded
    clock.t += 2
    assert ep.push_link[lc.fn] == 0 and {to for to, _ in ep.pushes_to()} == {0}
    sub = m.Request(701, lc.fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), hst.session).pack()
    assert m.Result.unpack(ep.handle(sub, 2, link="tcp-1")).succeeded
    clock.t += 2
    assert {to for to, _ in ep.pushes_to()} == {"tcp-1"}


def test_repeated_role_channels_of_one_role_are_their_union():
    """core §7.4: role_channels may name the same role more than once - its channels are the union (the host's decode,
    the probe's check of a plan)."""
    rc = (catalog.role_channels(1, [20]), catalog.role_channels(1, [30]))
    assert catalog.decode_description(b"".join(rc)).roles == {1: [20, 30]}
    probe = vb.p4_x035()
    probe.offered = [vb.Offered(o.fn, o.instance, o.name, (o.tlvs[0],) + rc
                                + tuple(t for t in o.tlvs[1:] if t[0] != catalog.ROLE_CHANNELS), o.revision, o.inner)
                     if o.name == GPIO.name else o for o in probe.offered]
    ep = endpoint.Endpoint(probe, Clock())
    w = Wire(ep)
    w.open()
    plan = ep.fns[PLAN.name]
    assert w.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 20) + assign(4, 1, 30)).succeeded
    rejected(w.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 21)), m.UNSUPPORTED)


def spi_both_declared():
    """p4-x035 with its spi-target declaring channel_groups for sck / mosi and role_channels for sck (narrower than the
    groups), miso and cs (core §7.4's example)."""
    probe = vb.p4_x035()
    spi = reg.FIXTURE_SPI_TARGET
    new = (catalog.channel_group(1, [(1, 20), (2, 21)]), catalog.channel_group(2, [(1, 25), (2, 26)]),
           catalog.role_channels(1, [20, 21]), catalog.role_channels(3, [22]), catalog.role_channels(4, [23]))
    probe.offered = [vb.Offered(o.fn, o.instance, o.name, (o.tlvs[0],) + new
                                + tuple(t for t in o.tlvs[1:] if t[0] != catalog.ROLE_CHANNELS), o.revision, o.inner)
                     if o.name == spi.name else o for o in probe.offered]
    return probe


@pytest.mark.parametrize("pins, ok", [
    ([(1, 20), (2, 21), (3, 22), (4, 23)], True),        # group 1 whole, miso / cs among their candidates
    ([(1, 21), (2, 20), (3, 22), (4, 23)], False),       # no group matches whole
    ([(1, 25), (2, 26), (3, 22), (4, 23)], False),       # group 2 whole, but sck 25 is not a role_channels candidate
    ([(1, 20), (2, 21), (3, 30), (4, 23)], False),       # miso outside its role_channels
], ids=["group-and-candidates", "no-group", "group-not-candidate", "role-outside"])
def test_with_both_declared_the_pins_match_a_group_and_the_role_channels(pins, ok):
    """core §7.4: where channel_group and role_channels are both declared, the chosen pins match one group whole and are
    each among role_channels' candidates for the roles it names; a role only role_channels names (miso, cs) is bound by
    it alone."""
    ep = build(spi_both_declared)
    w = Wire(ep)
    w.open()
    fn = ep.fns[reg.FIXTURE_SPI_TARGET.name]
    r = w.raw(ep.fns[PLAN.name], PLAN.op["plan_apply"], b"".join(assign(fn, role, ch) for role, ch in pins))
    if ok:
        assert r.succeeded and {a for a in ep.plan if a[0] == fn} == {(fn, role, ch) for role, ch in pins}
    else:
        rejected(r, m.UNSUPPORTED)
        assert not ep.plan


def test_an_x_unit_id_names_no_probe_and_keeps_no_session(tmp_path):
    """core §7.5: the host does not name a probe by an `x-` unit_id (tcp:x-... / usb:x-... refused before anything is
    opened) nor keep anything across sessions under it (kept_session keeps nothing)."""
    from oep_client import kept_session
    for target in ("tcp:x-esp32", "usb:x-esp32"):
        with pytest.raises(ValueError, match="x- unit_id names no unit"):
            link.open_host(target)
    ep = build(lambda: vb.with_unit_id(vb.p4_x035(), "x-board"))
    hst = host_on(ep)
    kept = kept_session.KeptSession(directory=tmp_path)
    kept.before_open(hst)
    assert "names no unit" in kept.why_not and not list(tmp_path.iterdir())


@pytest.mark.parametrize("held", ["gpio-output", "idle-output-high", "idle-pull-up"])
def test_a_reading_interface_never_changes_a_pins_drive(held):
    """core §8: taking a pin changes nothing electrical, and an interface that only reads pins never changes them -
    logic capture planned, started and stopped on a pin a gpio drives, or whose idle is an output or a pull, leaves its
    state as it was."""
    clock = Clock()
    ep = endpoint.Endpoint(vb.p4_x035(), clock)
    hst = h.Host(lambda b: ep.handle(b, 0), rng=random.Random(3))
    hst.open(3000)
    if held == "gpio-output":
        ch = 20
        core.plan_apply(hst, [(4, 1, ch)])
        hst.call(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", ch, 3))
    else:
        ch = 30
        mode = CFG.enum["idle_mode"]["output_high" if held == "idle-output-high" else "pull_up"]
        hst.call(ep.fns[CFG.name], CFG.op["set"], b"\x01" + idle_item(ch, mode))
    before = ep.pin_state(ch)
    lc = cap.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, ch)] + ([(4, 1, ch)] if held == "gpio-output" else []))
    assert ep.pin_state(ch) == before
    lc.configure(rate=1_000_000, mode=cap.STREAMING)
    lc.subscribe()
    lc.start()
    clock.t += 2
    assert ep.pin_state(ch) == before
    lc.stop()
    assert ep.pin_state(ch) == before


def test_data_due_goes_whole_in_frames_of_max_frame_without_min_bytes_again():
    """core §10.3: once a data condition holds (min_bytes waiting), everything waiting goes, cut into frames within
    max_frame; min_bytes is not applied again to the rest (the last frame is shorter) - it is no minimum frame length."""
    ep, hst, lc, clock = streaming(min_bytes=1000)
    clock.t += 3
    assert not frames(ep, m.ROLE_DATA)                                         # 750 bytes: not yet
    clock.t += 1
    data = frames(ep, m.ROLE_DATA)
    sizes = [struct.unpack_from("<H", f, 13)[0] for f in data]
    assert len(data) >= 2 and sum(sizes) == 1000 and sizes[-1] < 1000
    assert all(len(f) <= ep.probe.max_frame for f in data) and ep.captures[lc.fn].unsent() == 0


def test_the_serial_port_sends_answers_first_and_keeps_notifications_within_twice_max_frame():
    """core §10.4: the probe answers every request that came before it starts a new notification; it keeps at most
    max_frame x 2 bytes of notifications waiting in the transport's buffer and drops the rest before they start (their
    seq is spent: the gap shows it, §10.2). The virtual bench's serial port (virtual_bench_serial)."""
    from oep_client import cobs, virtual_bench_serial
    ep, hst, lc, clock = streaming()
    port = virtual_bench_serial.VirtualSerialPort(ep, 0)

    def out():
        return [cobs.unframe(c) for c in port.output(1 << 20).split(b"\x00") if c]
    clock.t += 20                                                              # far more data than 2 x max_frame
    port.feed(cobs.frame(m.Request(60000, 0, m.OP_CLOCK, b"", 0).pack()))
    port.tick()
    got = out()
    assert got[0][0] == m.ROLE_RESULT and m.Result.unpack(got[0]).corr == 60000   # the answer first
    pushed = got[1:]
    assert pushed and all(f[0] in (m.ROLE_DATA, m.ROLE_EVENT) for f in pushed)
    assert sum(len(cobs.frame(f)) for f in pushed) <= 2 * ep.probe.max_frame and port.dropped_notices >= 1
    last = struct.unpack_from("<H", pushed[-1], 3)[0]
    clock.t += 1
    port.tick()
    later = out()
    assert later and struct.unpack_from("<H", later[0], 3)[0] > last + 1        # the dropped ones' seq: a gap


def test_a_subscribe_again_drops_the_old_subscriptions_unsent_notifications_on_the_transport():
    """core §10.3: replacing or ending a subscription drops what the old one has not sent yet - also what waits in the
    transport's buffer (the virtual bench's serial port), not only in the probe; what the new one makes goes."""
    from oep_client import cobs, virtual_bench_serial
    ep, hst, lc, clock = streaming()
    port = virtual_bench_serial.VirtualSerialPort(ep, 0)
    clock.t += 1
    port.tick()
    assert any(n is not None for n in port.notice)                            # the old subscription's, not sent
    port.feed(cobs.frame(m.Request(hst.next_corr(hst.session), lc.fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), hst.session).pack()))
    clock.t += 1
    port.tick()
    got = [cobs.unframe(c) for c in port.output(1 << 20).split(b"\x00") if c]
    assert m.Result.unpack(got[0]).corr == hst._corr and m.Result.unpack(got[0]).succeeded
    assert got[1:] and struct.unpack_from("<H", got[1], 3)[0] == 0          # only the new one's, seq from 0
    clock.t += 1
    port.tick()
    assert any(n is not None for n in port.notice)                            # the new one's, not sent yet
    port.feed(cobs.frame(m.Request(hst.next_corr(hst.session), lc.fn, m.OP_UNSUBSCRIBE, b"", hst.session).pack()))
    got = [cobs.unframe(c) for c in port.output(1 << 20).split(b"\x00") if c]
    assert [f[0] for f in got] == [m.ROLE_RESULT]


# ---- second review 2026-10-15 (core §1-§6): rules the trace above checked only in part -----------------------------

def test_the_booleans_the_probe_sends_are_0_or_1_and_the_host_sends_force_0_or_1(ep):
    """core §2.1: a sender puts 0 or 1 in a boolean u8 - the probe's lock_state locked and describe's more (0 / 1 only),
    the host's open force (0 without force, 1 with)."""
    w = Wire(ep)
    assert w.raw(0, m.OP_LOCK_STATE, sid=0).payload[0] == 0
    assert Wire(ep, 0x33).open(tail=m.tlv(OWNER, b"x")).succeeded
    assert w.raw(0, m.OP_LOCK_STATE, sid=0).payload[0] == 1
    first = 0
    while True:
        more = w.ok(0, m.OP_DESCRIBE, struct.pack("<HH", 0, first), sid=0)
        assert more[0] in (0, 1)
        if not more[0]:
            break
        first += len(m.split_tlvs(more[1:]))
    for force, byte in ((False, 0), (True, 1)):
        hst = h.Host(scripted(completed(struct.pack("<II", 3000, 1))), rng=random.Random(1))
        hst.revision, hst._describes[0] = 1, [(CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 1000))]
        hst.open(force=force)
        assert hst.send.sent[-1][m.REQUEST_HEADER + 4] == byte


def test_list_names_are_shown_with_control_characters_replaced():
    """core §2.1: every answer text the host shows is cleaned first - a list entry's name too (the capability dump
    prints it): C0 controls, 0x7F and invalid UTF-8 replaced. (Before: the name was shown as the probe sent it.)"""
    from oep_client import dump
    bad = b"io.x\x1b[2J\x07\x7f\xff"
    payload = struct.pack("<HB", 1, 1) + struct.pack("<HHBB", 5, 0, 1, len(bad)) + bad
    _, (entry,) = catalog.unpack_list_result(payload)
    assert entry.name == "io.x�[2J���" and entry.fn == 5
    hst = h.Host(scripted(completed(payload)))
    hst.revision = 1
    (got,) = core.list_entries(hst)
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in got.name)
    caps = dump.Capabilities(1, 64, requests={"list": 1, "describe": 1})
    caps.offers.append(dump.Offer(got, catalog.decode_description(b"")))
    assert "\x1b" not in dump.to_text(caps) and "\x07" not in dump.to_text(caps)


def test_a_tlv_numbered_0_breaks_an_answer_with_or_without_bit_7():
    """core §2.2: TLV number 0 in an answer is a broken TLV - 0x00 as well as 0x80 - the host never takes it for a
    value."""
    for zero in (b"\x00\x00\x00", b"\x80\x00\x00", b"\x00\x01\x00x"):
        with pytest.raises(m.ProtocolError):
            m.Tail.parse(zero)
        with pytest.raises(m.ProtocolError):
            h.Host(scripted(completed(struct.pack("<IQ", 1, 2) + zero))).clock()


def test_the_host_reads_data_and_events_with_tlvs_after_their_fixed_part():
    """core §2.3, §10.2: a data frame or an event is its fixed part then TLVs, as an answer - the host reads the same
    data and the same event when a TLV it does not know follows the fixed part (the data is not taken longer, the event
    is not refused)."""
    ep, hst, lc, clock = streaming()
    clock.t += 3
    data = frames(ep, m.ROLE_DATA)
    lc.stop()
    stopped = [f for f in ep.pushes() if f[0] == m.ROLE_EVENT and f[5] == cap.EVENT_STOPPED]
    assert data and stopped
    extra = m.tlv(0x7E, b"later")
    plain = lc.stream(serial_link(*data), seconds=0.01)
    tailed = lc.stream(serial_link(*[d + extra for d in data]), seconds=0.01)
    assert bytes(tailed.data) == bytes(plain.data) and tailed.seq_lost == plain.seq_lost == 0
    want = cap.unpack_event(stopped[0])
    got = cap.unpack_event(stopped[0] + extra)
    assert (got.kind, got.seq, got.generation, got.reason) == (want.kind, want.seq, want.generation, want.reason)


def test_one_corr_counter_for_every_session_id_never_reuses_a_corr():
    """core §4.1: on one transport no corr is used again while a request may be unresolved or its answer still come,
    whatever its session_id - the host's requests (session 0 and the session's, interleaved) take one cyclic counter:
    each corr is the last + 1 (65535 then 1), never 0, so none repeats within the 65535 before it."""
    ep = build()
    sent = []
    hst = h.Host(lambda b: sent.append(m.Request.unpack(b)) or ep.handle(b, 0), rng=random.Random(8))
    hst._corr = 65530
    hst.open()
    for _ in range(4):
        hst.lock_state()
        hst.keepalive()
        hst.clock()
    corrs = [r.corr for r in sent]
    assert {r.session for r in sent} == {0, hst.session}
    assert 0 not in corrs and all(b == a % 0xFFFF + 1 for a, b in zip(corrs, corrs[1:]))
    assert len(set(corrs)) == len(corrs)


def test_a_dropped_copy_shares_the_one_answer_of_its_request():
    """core §4.2, §4.4: one answer per request - a copy of a request not yet answered, dropped on the same transport,
    gets no answer of its own: the one answer of the request stands for both."""
    ep = build()
    w = Wire(ep)
    w.open()
    data = w.bytes(0, m.OP_KEEPALIVE)
    assert ep.receive(data) == "queued" and ep.receive(data) == "coalesced"
    answer = ep.process()
    assert m.Result.unpack(answer).corr == m.Request.unpack(data).corr
    assert [e[1] for e in ep.routes[0]] == [answer]                           # one answer waits to go, not two
    ep.sent()
    assert not ep.credits()


def test_no_resource_the_host_makes_the_resource_again():
    """core §4.3 no_resource: the probe answers it for a number it does not know (a connection gone with a restart); the
    host takes it as NoResource - not a failure of the op - and makes the resource again, which then works."""
    ep = build(vb.p4_bench)
    hst = host_on(ep)
    hst.open()
    attach_body = struct.pack("<BHHI", 1, 2, 3, 4_000_000)
    conn = struct.unpack_from("<I", hst.call(1, RVSWD.op["attach"], attach_body).payload)[0]
    ep.reboot()
    hst.open()                                                                # a new session after the restart
    with pytest.raises(h.NoResource):
        hst.call(2, RV.op["halt"], struct.pack("<I", conn))
    again = struct.unpack_from("<I", hst.call(1, RVSWD.op["attach"], attach_body).payload)[0]
    assert again in ep.conns
    assert hst.call(2, RV.op["halt"], struct.pack("<I", again)).succeeded


def test_a_record_half_the_space_old_is_not_used(ep):
    """core §5.1: a byte record half the space or more behind the last corr is dropped and never matched - the very
    same bytes again are result_lost, not the recorded answer, and nothing runs."""
    w = Wire(ep)
    w.open()
    data = w.bytes(0, m.OP_KEEPALIVE, corr=100)
    first = ep.handle(data, 0)
    assert ep.handle(data, 0) == first                                        # recorded
    assert w.raw(0, m.OP_KEEPALIVE, corr=100 + 0x4000).succeeded
    assert w.raw(0, m.OP_KEEPALIVE, corr=100 + 0x8000).succeeded             # the record of 100 is half the space old
    rejected(m.Result.unpack(ep.handle(data, 0)), m.RESULT_LOST)


def test_a_recorded_answer_changes_neither_the_lease_nor_where_notifications_go():
    """core §5.1: the recorded answer to a resend runs nothing again and moves neither the lease nor the notifications'
    transport - a subscribe sent again (here from another transport, the table being the probe's one) keeps both."""
    ep, hst, lc, clock = streaming()
    sub = m.Request(hst.next_corr(hst.session), lc.fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), hst.session).pack()
    first = ep.handle(sub, 0)
    assert m.Result.unpack(first).succeeded and ep.push_link[lc.fn] == 0
    expires, runs = ep.expires_ms, ep.executions
    clock.t += 700
    assert ep.handle(sub, 2, link="tcp-1") == first
    assert ep.push_link[lc.fn] == 0 and ep.expires_ms == expires and ep.executions == runs


def test_a_refused_open_of_another_id_keeps_the_resend_state(ep):
    """core §5.1, §6.4: only a new id's open that succeeds replaces the resend state - another id's open refused locked
    leaves it: the holder's request sent again still gets its recorded answer."""
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open()
    data = a.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    rejected(b.open(), m.LOCKED)
    assert ep.handle(data, 0) == first and ep_holder(ep) == 0x10
    rejected(a.raw(0, m.OP_KEEPALIVE, corr=0xF000), m.RESULT_LOST)            # the order state too


def test_keepalive_changes_nothing_but_the_lease():
    """core §6.4: keepalive only extends the lease - the plan, the pins' state, the subscriptions and where they send,
    the connections and the owner are all as before."""
    ep = build()
    w = Wire(ep)
    w.open(2000, tail=m.tlv(OWNER, b"k"))
    conn = attach(w, (2, 54))
    plan = ep.fns[PLAN.name]
    w.ok(plan, PLAN.op["plan_apply"], assign(4, 1, 20))
    w.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 4))
    w.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))

    def snapshot():
        return (dict(ep.plan) if isinstance(ep.plan, dict) else list(ep.plan), [ep.pin_state(c) for c in range(55)],
                dict(ep.subscribed), dict(ep.push_link), sorted(ep.conns), w.lock_state()[2], ep.holder)
    ep.now.t = 1500
    before = snapshot()
    assert w.raw(0, m.OP_KEEPALIVE).succeeded
    assert snapshot() == before and conn in ep.conns
    assert w.lock_state()[:2] == (1, 2000)


# ---- second review of §7-§13 (docs/trace/review2/core.7-13.json) ----------------------------------------------------

def test_instances_are_unique_u16s_with_gaps_and_not_the_fn_order():
    """core §7.2: instance is a u16 unique within its (name, revision) - 0 may be used, gaps are allowed, it is not
    computed from the fn order or the list position: the bench and both checkers take instances 0 and 7 given against
    the fn order, and refuse an instance repeated within one (name, revision)."""
    import test_conformance_interfaces as tci
    gpio = "oep.fixture.gpio"
    sparse = vb.VirtualProbe("x", 256, [vb.Offered(0, 0, vb.CORE_NAME), vb.Offered(1, 7, gpio), vb.Offered(2, 0, gpio),
                                        vb.Offered(3, 7, gpio, revision=2)])
    assert sparse.instance_errors() == []
    twice = vb.VirtualProbe("x", 256, [vb.Offered(0, 0, vb.CORE_NAME), vb.Offered(1, 3, gpio), vb.Offered(2, 3, gpio)])
    assert twice.instance_errors()
    for instances, ok in (({9: 0, 2: 7}, True), ({9: 4, 2: 4}, False)):
        model, c, runner = tci.setup()                                     # fns 9 and 2 share (name, revision 1)
        listing = model.extension.list_page

        def page(first, limit, listing=listing, instances=instances):
            out, at = bytearray(listing(first, limit)), 3
            for _ in range(out[2]):
                fn = struct.unpack_from("<H", out, at)[0]
                if fn in instances:
                    struct.pack_into("<H", out, at + 2, instances[fn])
                at += 6 + out[at + 5]
            return bytes(out)
        model.extension.list_page = page
        report = runner.run()
        failed = [row["id"] for row in report["checks"] if row["status"] != "passed"]
        entries = c.list_snapshot()
        if ok:
            assert report["status"] == "passed" and not failed, failed
            for e in entries:
                c.interface(e, entries)                                     # conformance.Checks' own check
        else:
            assert failed and all(i.startswith("IF-INSTANCE-") for i in failed), failed
            with pytest.raises(Exception, match="duplicate instance"):
                for e in entries:
                    c.interface(e, entries)


def test_the_host_pages_describe_until_more_0_and_list_until_total():
    """core §7.3, §7.2: the host asks describe again with first moved by the TLVs it got while more = 1 and stops at
    more = 0; list has no more - the host stops once it has total entries."""
    def describe_page(data):
        first = struct.unpack_from("<H", data, m.REQUEST_HEADER + 2)[0]
        return completed(b"\x01" + m.tlv(0x40, b"a") + m.tlv(0x41, b"b")) if first == 0 else \
            completed(b"\x00" + m.tlv(0x42, b"c"))
    hst = h.Host(scripted(describe_page, describe_page))
    assert [t for t, _ in core.describe(hst, 5)] == [0x40, 0x41, 0x42]
    firsts = [struct.unpack_from("<HH", d, m.REQUEST_HEADER) for d in hst.send.sent]
    assert firsts == [(5, 0), (5, 2)]                                       # first += the TLVs received; then stop
    one = lambda fn, name: catalog.pack_entry(catalog.ListEntry(fn, 0, 1, name))          # noqa: E731
    hst = h.Host(scripted(completed(struct.pack("<HB", 3, 2) + one(1, "a.b") + one(2, "a.c")),
                          completed(struct.pack("<HB", 3, 1) + one(3, "a.d"))))
    assert [e.fn for e in core.list_entries(hst)] == [1, 2, 3]
    assert [struct.unpack_from("<H", d, m.REQUEST_HEADER)[0] for d in hst.send.sent] == [0, 2]   # no third request


def test_a_released_pin_returns_to_its_configured_idle_whichever_way():
    """core §8, §9: a pin released by plan_release, end, lease expiry or force goes back to the idle state its setting
    defines (here a pull-up), not to Hi-Z and not left as the interface drove it."""
    for way in ("release", "end", "expiry", "force"):
        ep = build()
        w = Wire(ep)
        w.open(1000)
        assert cfg_set(w, ep, idle_item(20, CFG.enum["idle_mode"]["pull_up"])).succeeded
        assert ep.pin_state(20) == "idle pull-up"
        plan = ep.fns[PLAN.name]
        w.ok(plan, PLAN.op["plan_apply"], assign(4, 1, 20))
        w.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 4))
        assert ep.pin_state(20) == "gpio 4"
        if way == "release":
            w.ok(plan, PLAN.op["plan_release"], b"\x00")
        elif way == "end":
            w.ok(0, m.OP_END)
        elif way == "expiry":
            ep.now.t = 5000
            Wire(ep).raw(0, m.OP_CLOCK, sid=0)
        else:
            Wire(ep, 0x99).open(force=1)
        assert ep.pin_state(20) == "idle pull-up", way


def test_at_boot_the_saved_idles_are_applied_and_a_disabled_channel_is_left_alone():
    """core §8: at boot, before the first request, every channel but the probe's own and those the settings forbid is
    in its idle state - the saved idle (output high) where one is set, Hi-Z elsewhere; a disabled channel stays as the
    reset left it (never parked), the probe's own are not touched."""
    ep = build()
    w = Wire(ep)
    w.open()
    disable = m.tlv(__import__("oep_client.config", fromlist=["ITEM"]).ITEM["disable"], struct.pack("<H", 31))
    assert cfg_set(w, ep, idle_item(30, CFG.enum["idle_mode"]["output_high"]), disable).succeeded
    w.ok(ep.fns[CFG.name], CFG.op["save"])
    ep.reboot()
    assert ep.pin_state(30) == "idle output-high" and ep.pin_state(31) == "reset"
    assert 31 not in ep.parked and not set(ep.parked) & ep.own_channels
    for ch in set(range(ep.channels)) - ep.own_channels - {30, 31}:
        assert ep.pin_state(ch) == "idle hi-z", ch


def test_a_take_clashing_with_a_runtime_resource_or_a_setting_changes_nothing():
    """core §8.1: whatever takes pins checks every hold first - a connection's pins against the plan's and the plan's
    against a live connection's are unavailable cause 1 (pin_in_use) with the channel, a disabled channel cause 5; the
    refused request changes nothing (no connection, no assignment, every pin's state as it was)."""
    ep = build(vb.rp2350_pins)
    w = Wire(ep)
    w.open()
    plan = ep.fns[PLAN.name]
    state = lambda: ([ep.pin_state(c) for c in range(ep.channels)], sorted(ep.plan), sorted(ep.conns))   # noqa: E731
    w.ok(plan, PLAN.op["plan_apply"], assign(4, 1, 0))
    before = state()
    r = rejected(w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 0, 1, 4_000_000)), m.UNAVAILABLE)
    assert (h.Unavailable(r).cause, h.Unavailable(r).channels) == ("pin_in_use", [0]) and state() == before
    w.ok(plan, PLAN.op["plan_release"], b"\x00")
    attach(w, (0, 1))
    before = state()
    r = rejected(w.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 1)), m.UNAVAILABLE)
    assert (h.Unavailable(r).cause, h.Unavailable(r).channels) == ("pin_in_use", [1]) and state() == before
    ep = build()
    w = Wire(ep)
    w.open()
    assert cfg_set(w, ep, m.tlv(__import__("oep_client.config", fromlist=["ITEM"]).ITEM["disable"], struct.pack("<H", 21))).succeeded
    before = ([ep.pin_state(c) for c in range(55)], sorted(ep.plan))
    r = rejected(w.raw(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 20) + assign(4, 1, 21)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "held_by_settings" and ([ep.pin_state(c) for c in range(55)], sorted(ep.plan)) == before


def test_a_refused_force_open_releases_nothing_and_keeps_the_resend_state():
    """core §9: another id's force open releases the old session's resources and replaces the resend state only when
    it succeeds - one refused (an owner past 32 bytes: malformed) leaves the plan, the pins, the subscription, the lock
    and the record as they were."""
    ep = build()
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open()
    a.ok(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 20))
    a.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 4))
    a.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
    data = a.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    rejected(b.open(force=1, tail=m.tlv(OWNER, b"x" * 33)), m.MALFORMED)
    assert ep.holder == 0x10 and ep.plan and 7 in ep.subscribed and ep.pin_state(20) == "gpio 4"
    assert ep.handle(data, 0) == first and ep.last == 0x10


def test_a_resent_open_not_kept_is_result_lost_and_changes_nothing(ep):
    """core §9: the same open sent again changes nothing - its recorded answer when it is kept, result_lost when the
    record has no room for it; the lock, the lease and (on the full bench) the plan stay."""
    w = Wire(ep)
    o = w.bytes(0, m.OP_OPEN, struct.pack("<IB", 3000, 0))
    assert m.Result.unpack(ep.handle(o, 0)).succeeded
    full = not ep.probe.core_contract
    if full:
        w.ok(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 20))
    ep.remember_max = 9                                                      # nothing kept from here on
    o2 = w.bytes(0, m.OP_OPEN, struct.pack("<IB", 3000, 0))
    assert m.Result.unpack(ep.handle(o2, 0)).succeeded
    ep.now.t = 500
    before = w.lock_state()
    rejected(m.Result.unpack(ep.handle(o2, 0)), m.RESULT_LOST)
    assert w.lock_state() == before and ep_holder(ep) == SID
    if full:
        assert ep.plan


def test_event_bytes_are_not_counted_toward_min_bytes():
    """core §10.3: events are never batched and their bytes do not count toward min_bytes - an event goes at once
    while 750 bytes of data (min_bytes 1000) still wait, even though data and event together pass 1000."""
    ep, hst, lc, clock = streaming(min_bytes=1000)
    clock.t += 3                                                               # 750 bytes of data waiting
    assert not frames(ep, m.ROLE_DATA) and ep.captures[lc.fn].unsent() == 750
    ep._events(lc.fn, [bytes([0x7E]) + bytes(400)])
    pushed = ep.pushes()
    assert [f[0] for f in pushed] == [m.ROLE_EVENT] and ep.captures[lc.fn].unsent() == 750


# ---- third review of §0-§5 (docs/trace/review3/core-a.json) -----------------------------------------------------------

def test_the_preflight_reports_describe_text_with_control_characters_replaced(monkeypatch, tmp_path):
    """core §2.1: the hardware preflight's report shows fn 0's describe text (firmware, model, unit_id, chip) - with C0
    controls, 0x7F and invalid UTF-8 replaced first. (Before: decoded strictly - bad UTF-8 failed the probe and 0x7F
    went into the report as it came.)"""
    from oep_client import hardware
    from test_hardware import physical_equipment, simulated_transport
    hst, _, _ = simulated_transport(monkeypatch)
    real = core.describe
    fw = CORE.tlv["describe"]["firmware"]

    def bent(h_, fn=0):
        tlvs = real(h_, fn)
        return [(t, b"fw\x1b[2J\x7f\xff" if t == fw else v) for t, v in tlvs] if fn == 0 else tlvs
    monkeypatch.setattr(core, "describe", bent)
    lock = tmp_path / "shared.lock"
    lock.touch()
    report = hardware.physical_preflight(physical_equipment(), lock_path=lock)
    got = report["probes"][0]
    assert report["status"] == "passed", got
    assert got["firmware"] == "fw�[2J��"
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for v in got.values() if isinstance(v, str) for c in v)


def _data(fn, seq, data, tail=b""):
    return bytes([m.ROLE_DATA]) + struct.pack("<HHQH", fn, seq, 0, len(data)) + data + tail


def test_a_notification_with_a_tlv_past_its_end_or_numbered_0_is_broken():
    """core §2.2: in an event or a data frame too, a TLV whose len runs past the frame, or a TLV numbered 0 (0x00 or
    0x80), makes it broken - the host reads nothing from it; the same frame without it reads normally."""
    gen = reg.FIXTURE_LOGIC.tlv["data"]["generation"]
    good = _data(7, 1, b"abc", m.tlv(gen, struct.pack("<I", 5)))
    assert cap._unpack_push(good)[3:5] == (b"abc", 5)
    stopped = bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 7, 2, cap.EVENT_STOPPED) + struct.pack("<BBI", 1, 0, 5)
    assert cap.unpack_event(stopped).generation == 5
    for bad in (b"\x7e\x05\x00ab", b"\x00\x00\x00", b"\x80\x01\x00x"):
        with pytest.raises(m.ProtocolError):
            cap._unpack_push(good + bad)
        with pytest.raises(m.ProtocolError):
            cap.unpack_event(stopped + bad)


def test_answers_and_notifications_carry_no_bit_7_and_the_host_skips_one_that_does():
    """core §2.2: bit 7 is the critical mark of requests only - the probe's answers (confirm, lock_state after an owner
    sent critical, locked, unavailable, unsupported, describe of every fn), events and data frames carry their TLVs
    with bit 7 clear; a host reading a data frame or an event skips a TLV with bit 7 set as an unknown tag."""
    ep, hst, lc, clock = streaming()
    seen = []
    w = Wire(ep, hst.session)
    w.corr = hst._corr + 10
    seen += m.split_tlvs(w.ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)[16:])
    for fn in [0] + [e.fn for e in entries(ep)[1]]:
        seen += describe(ep, fn)
    hst.end()
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.corr = b.corr = hst._corr + 50
    assert a.open(tail=m.tlv(OWNER, b"me", critical=True)).succeeded
    seen += m.split_tlvs(a.lock_state() and a.raw(0, m.OP_LOCK_STATE, sid=0).payload[5:])
    seen += m.split_tlvs(rejected(b.open(), m.LOCKED).payload[4:])
    plan = ep.fns[PLAN.name]
    seen += m.split_tlvs(rejected(a.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 2, critical=True)), m.UNSUPPORTED)
                         .payload[1:])
    assert a.raw(plan, PLAN.op["plan_apply"], assign(5, 1, 20)).succeeded
    seen += m.split_tlvs(rejected(a.raw(plan, PLAN.op["plan_apply"], assign(4, 1, 20)), m.UNAVAILABLE).payload)
    assert (OWNER, b"me") in seen and len(seen) > 20
    assert all(t < 0x80 for t, _ in seen), [hex(t) for t, _ in seen if t >= 0x80]
    ep2, hst2, lc2, clock2 = streaming()
    clock2.t += 3
    data = frames(ep2, m.ROLE_DATA)
    lc2.stop()
    events = frames(ep2, m.ROLE_EVENT)
    assert data and events
    for f in data:
        n = struct.unpack_from("<H", f, 13)[0]
        assert all(t < 0x80 for t, _ in m.split_tlvs(f[15 + n:]))
    for f in events:
        if f[5] == cap.EVENT_STOPPED:
            assert all(t < 0x80 for t, _ in m.split_tlvs(f[12:]))
    gen = reg.FIXTURE_LOGIC.tlv["data"]["generation"]
    assert cap._unpack_push(_data(7, 1, b"x", m.tlv(gen | 0x80, struct.pack("<I", 9))))[4] is None
    assert cap._unpack_push(_data(7, 1, b"x", m.tlv(gen | 0x80, struct.pack("<I", 9))
                                  + m.tlv(gen, struct.pack("<I", 3))))[4] == 3
    stopped = bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 7, 2, cap.EVENT_STOPPED) + struct.pack("<BBI", 1, 0, 5)
    assert cap.unpack_event(stopped + m.tlv(0x81, b"\x01")).generation == 5


def test_a_describe_tag_means_what_the_described_fns_definition_says():
    """core §2.2: describe's tags are in the space of the fn described - 0x40 is fn 0's firmware text and
    oep.probe.plan's plan_roles (u32): the host reads plan_roles from the plan's describe only, and fn 0's max_op_ms
    tag in another fn's describe is not fn 0's max_op_ms."""
    ep = build()
    hst = host_on(ep)
    plan = core.plan_fn(hst)
    fw = dict(core.describe(hst, 0))[0x40]
    roles = dict(core.describe(hst, plan))[0x40]
    assert fw == b"3.0.0" and len(roles) == 4 and core.plan_roles(hst) == struct.unpack("<I", roles)[0]
    tag = CORE.tlv["describe"]["max_op_ms"]
    hst2 = h.Host(lambda b: b)
    hst2._describes = {0: [], plan: [(tag, struct.pack("<I", 1234))]}
    assert hst2.max_op_ms() is None


def test_an_answer_goes_back_on_the_transport_its_request_came_on():
    """core §4.2: the one answer to a request goes back on the transport the request came on, with its corr - requests
    on transports 0 and 2 each get their answer in their own route, none in the other."""
    ep = build()
    w = Wire(ep)
    a, b = w.bytes(0, m.OP_CLOCK, sid=0), w.bytes(0, m.OP_LOCK_STATE, sid=0)
    assert ep.receive(a, 0) == "queued" and ep.receive(b, 2) == "queued"
    ans_b, ans_a = ep.process(2), ep.process(0)
    assert [e[1] for e in ep.routes[2]] == [ans_b] and [e[1] for e in ep.routes[0]] == [ans_a]
    assert m.Result.unpack(ans_b).corr == m.Request.unpack(b).corr and len(ans_b) == 5 + 5
    assert m.Result.unpack(ans_a).corr == m.Request.unpack(a).corr and len(ans_a) == 5 + 12


def test_unsupported_names_the_element_after_its_tag():
    """core §4.3 unsupported: after the tag (as received) a TLV in unavailable's tag space may say which element - a
    plan_apply naming a channel gpio does not offer for the role: tag 0x90 (as received), then channel (0x02, u16);
    the host reads both."""
    ep = build()
    w = Wire(ep)
    w.open()
    r = rejected(w.raw(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 2, critical=True)), m.UNSUPPORTED)
    assert r.payload == bytes([0x90]) + m.tlv(UNA["channel"], struct.pack("<H", 2))
    u = h.Unsupported(r)
    assert u.tag == 0x90 and u.tlvs == [(UNA["channel"], b"\x02\x00")] and not ep.plan


def test_the_policy_check_comes_after_the_header_and_before_the_record_and_the_session():
    """core §4.3 order 1 and the optional policy, §5.1: the header's refusals come first (corr 0, unknown fn, an op the
    fn's ops do not set - even one that needs the lock, with session_id 0); then the policy - unavailable cause 7 for
    another id's request (not locked) and for the holder's resend (not its recorded answer); nothing changes, and fn
    0's confirm / list / describe / clock / lock_state are never refused by it. Allowed again, the record answers."""
    ep = build(vb.esp32_v003)
    w = Wire(ep)
    w.open()
    data = w.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    ep.policy = lambda req: False
    before = (ep.holder, ep.expires_ms, ep.newest_corr, dict(ep.resend), ep.executions)
    rejected(w.raw(0, m.OP_KEEPALIVE, corr=0), m.MALFORMED)
    rejected(w.raw(99, 0x10), m.UNKNOWN_FUNCTION)
    rejected(w.raw(ep.fns[RV.name], RV.op["step"], struct.pack("<I", 1), sid=0), m.UNKNOWN_OPERATION)
    ep.now.t = 500
    for r in (Wire(ep, 0x77).raw(0, m.OP_KEEPALIVE), m.Result.unpack(ep.handle(data, 0))):
        assert h.Unavailable(rejected(r, m.UNAVAILABLE)).cause == "policy_denied"
    assert (ep.holder, ep.expires_ms, ep.newest_corr, dict(ep.resend), ep.executions) == before
    for op, body in ((m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01"), (m.OP_LIST, b"\0\0"), (m.OP_DESCRIBE, b"\0\0\0\0"),
                     (m.OP_CLOCK, b""), (m.OP_LOCK_STATE, b"")):
        assert w.raw(0, op, body, sid=0).succeeded, op
    ep.policy = None
    assert ep.handle(data, 0) == first


def test_a_message_of_max_frame_bytes_is_taken_whatever_its_frame_wraps_around_it():
    """core §4.4: max_frame counts the message from its role on - the 10-byte header included, the frame's wrapping
    (CRC, COBS, the 0x00s) not: on the serial port of a 64-byte probe a 64-byte request is answered though its frame is
    longer; a 65-byte one is dropped unanswered."""
    from test_trace_transports import answers, req, serial_port
    from oep_client import cobs
    ep, clock, port = serial_port(vb.esp32_v003_64)
    assert ep.probe.max_frame == 64
    msg = req(1, m.OP_CLOCK, m.tlv(0x3E, bytes(64 - m.REQUEST_HEADER - 3)))
    assert len(msg) == 64 and len(cobs.frame(msg)) > 64
    port.feed(cobs.frame(msg))
    assert answers(port) == [1]
    port.feed(cobs.frame(req(2, m.OP_CLOCK, m.tlv(0x3E, bytes(64 - m.REQUEST_HEADER - 2)))))
    assert answers(port) == []


def test_repeated_confirms_are_new_requests_and_a_late_answer_to_an_earlier_one_is_read_past():
    """core §4.4 (port_speed's repeated confirms, transports §4): each repeated confirm is a new request with a new corr,
    not a resend - the next goes before the first's floor has passed, and the first's answer arriving late is read past
    while the host waits for its own."""
    from oep_client import cobs
    written = []
    answer = m.CONFIRM_RESULT + struct.pack("<BHIBI", 1, 64, 64, 1, 3)

    class Stream(FakeStream):
        def write(self, data):
            req = m.Request.unpack(cobs.unframe(bytes(data)[1:-1]))
            written.append(req)
            if len(written) == 2:                                            # the first's answer, late, then its own
                for r in written:
                    self.incoming += cobs.frame(m.Result(r.corr, m.COMPLETED, m.SUCCESS, answer).pack())
            return len(data)
    lk = link.SerialLink.on_stream(Stream(), framing="cobs", timeout=0.2)
    assert lk._confirm_within(1.0, 0.05)
    assert [r.op for r in written] == [m.OP_CONFIRM] * 2 and written[0].corr != written[1].corr
    assert lk.stale == 1


def test_on_tcp_a_resend_given_up_closes_and_opens_the_connection_and_confirms_first():
    """core §5.1: a request and its resend unanswered - its outcome is unknown; on TCP the host closes the connection and
    opens it again, confirming there (the boot_id seen) before the next request goes out. (Before: the connection was
    kept and the next request went straight out on it.)"""
    from test_trace_transports import TcpStream, confirming, req
    old, new = TcpStream(lambda msg: []), TcpStream(confirming())
    lk = link.SerialLink.on_stream(old, "length", 0.1)
    lk.wait_add_s = 0.0
    lk.reopener = lambda: new
    boots = []
    lk.on_boot_id = boots.append
    with pytest.raises(link.TransportFailed):
        lk.send(req(7, m.OP_KEEPALIVE, session=5))
    assert len(old.writes) == 2 and len(set(old.writes)) == 1                   # the request and its one resend
    assert m.Result.unpack(lk.send(req(8))).corr == 8
    assert old.closed and len(old.writes) == 2 and boots == [0x11]
    assert [m.Request.unpack(x[2:]).op for x in new.writes] == [m.OP_CONFIRM, m.OP_CLOCK]


# ---- third review (docs/trace/review3/core-b.json): core §6-§13 ------------------------------------------------------

def test_the_lease_restarts_at_the_answers_generation_not_its_write():
    """core §6.1 (a09fa45): the holder's answer restarts lease_ms when it is generated - the write waiting, completing
    later, or the route closing and dropping the answer change nothing; a copy still waiting on the route (coalesced)
    neither pauses nor renews it; the lease then runs out lease_ms after the generation."""
    ep = build()
    w = Wire(ep)
    w.open(1000)
    ep.now.t = 300
    ep.receive(w.bytes(0, m.OP_KEEPALIVE))
    ep.process()
    assert ep.expires_ms == 1300                                              # generated at 300
    ep.now.t = 900
    ep.sent()
    assert ep.expires_ms == 1300                                              # the write completing: nothing
    data = w.bytes(0, m.OP_KEEPALIVE)
    assert ep.receive(data) == "queued"
    ep.now.t = 1000
    assert ep.receive(data) == "coalesced" and ep.expires_ms == 1300          # a copy still waiting: nothing
    ep.process()
    assert ep.expires_ms == 2000
    ep.now.t = 1500
    ep.close_route(0)                                                         # the answer dropped unsent
    assert ep.expires_ms == 2000 and ep.holder == SID
    ep.now.t = 1999
    assert Wire(ep).lock_state()[:2] == (1, 1)
    ep.now.t = 2000
    assert Wire(ep).lock_state() == (0, 0, None) and ep.holder is None


def test_lease_renewed_at_the_write_mutant(monkeypatch):
    """core §6.1 mutant: a probe that restarts the lease when the answer's write completes (the rule before a09fa45) is
    caught by the check above."""
    orig = endpoint.Endpoint.sent

    def renewing(self, route=0):
        orig(self, route)
        if self.holder is not None:
            self.expires_ms = self.now() + self.lease_ms
    monkeypatch.setattr(endpoint.Endpoint, "sent", renewing)
    with pytest.raises(AssertionError):
        test_the_lease_restarts_at_the_answers_generation_not_its_write()


def test_a_new_owners_lease_starts_at_its_open_answer_and_an_ended_session_is_never_renewed():
    """core §6.1: a new owner's lease starts when its open's answer is generated (a force open that runs 500 ms: from
    the end of that); after end the old session's ids renew nothing - their requests are no_session and the lock stays
    free."""
    ep = build()
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open(1000)
    ep.now.t = 200
    ep.receive(b.bytes(0, m.OP_OPEN, struct.pack("<IB", 2000, 1)))
    ep.process(spend=lambda: setattr(ep.now, "t", ep.now.t + 500))
    assert ep.holder == 0x20 and ep.expires_ms == 2700                        # its answer at 700
    ep.now.t = 800
    assert b.raw(0, m.OP_END).succeeded
    for w in (a, b):
        rejected(w.raw(0, m.OP_KEEPALIVE), m.NO_SESSION)
    assert Wire(ep).lock_state() == (0, 0, None) and ep.expires_ms == 2700


def test_an_expired_lease_is_cleaned_up_before_the_next_request_even_a_resend(ep):
    """core §6.1: the cleanup of a lease past its time comes before the next request's processing, its resend check
    included - the expired holder's request sent again still gets its recorded answer (§9), and by then the lock is
    free and (on the full bench) the plan, the pin and the subscription are released."""
    w = Wire(ep)
    w.open(1000)
    full = not ep.probe.core_contract
    if full:
        w.ok(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 20))
        w.ok(4, GPIO.op["set"], b"\x01" + struct.pack("<HB", 20, 4))
        w.ok(7, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
    data = w.bytes(0, m.OP_KEEPALIVE)
    first = ep.handle(data, 0)
    ep.now.t = 5000
    assert ep.handle(data, 0) == first
    assert ep_holder(ep) is None
    if full:
        assert not ep.plan and not ep.subscribed and ep.pin_state(20) == "idle hi-z"


def test_the_host_draws_a_new_session_id_for_each_session():
    """core §6.1: the host draws each session's id anew (a u32 from 1 - never 0, never a fixed value): an open after end
    draws again and uses the new id."""
    ids = iter([0x1111, 0x2222])

    class Rng:
        def __init__(self):
            self.calls = []

        def randrange(self, a, b):
            self.calls.append((a, b))
            return next(ids)
    ep = build()
    rng = Rng()
    hst = h.Host(lambda b: ep.handle(b, 0), rng=rng)
    hst.open()
    assert ep.holder == 0x1111
    hst.end()
    hst.open()
    assert rng.calls == [(1, 1 << 32)] * 2 and hst.session == 0x2222 == ep.holder


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_the_decision_table_whatever_force_says(profile):
    """core §6.2: force changes no row but the one it names - on a free lock an open with force takes it as one without
    does (lease, owner); the holder's open with force renews lease_ms and keeps the owner (and, on the full bench, the
    plan); another id's other request is locked with the remaining ms and the owner; the last ended id's open is
    no_session with force too."""
    ep = build(PROFILES[profile])
    s, o = Wire(ep, 0x10), Wire(ep, 0x20)
    assert s.open(2000, force=1, tail=m.tlv(OWNER, b"S")).succeeded
    assert s.lock_state() == (1, 2000, b"S") and ep_holder(ep) == 0x10
    full = not ep.probe.core_contract
    if full:
        s.ok(ep.fns[PLAN.name], PLAN.op["plan_apply"], assign(4, 1, 20))
    ep.now.t = 700
    r = rejected(o.raw(0, m.OP_KEEPALIVE), m.LOCKED)
    assert struct.unpack_from("<I", r.payload)[0] == 1300 and m.Tail.parse(r.payload[4:]).get(OWNER) == b"S"
    assert s.open(4000, force=1, tail=m.tlv(OWNER, b"T")).succeeded
    assert s.lock_state() == (1, 4000, b"S") and ep_holder(ep) == 0x10
    if full:
        assert ep.plan
    assert s.raw(0, m.OP_END).succeeded
    rejected(s.open(force=1), m.NO_SESSION)
    assert s.lock_state() == (0, 0, None)


def test_the_owner_comes_from_the_force_open_and_goes_at_expiry(ep):
    """core §6.4: an owner outside 1-32 bytes is malformed and changes nothing; a force open takes the lock and its
    owner replaces the old one (shown by lock_state and rejected locked); the lease running out forgets it."""
    a, b = Wire(ep, 0x10), Wire(ep, 0x20)
    a.open(1000, tail=m.tlv(OWNER, b"old"))
    for bad in (b"", b"x" * 33):
        rejected(b.open(force=1, tail=m.tlv(OWNER, bad)), m.MALFORMED)
        assert a.lock_state()[2] == b"old" and ep_holder(ep) == 0x10
    assert b.open(1000, force=1, tail=m.tlv(OWNER, b"new")).succeeded
    assert a.lock_state()[2] == b"new"
    assert m.Tail.parse(rejected(a.open(), m.LOCKED).payload[4:]).get(OWNER) == b"new"
    ep.now.t = 5000
    assert a.lock_state() == (0, 0, None)


def test_max_length_keeps_a_whole_request_and_its_answer_within_max_frame():
    """core §7.4 (max_length): the request and the answer still fit max_frame - every profile's riscv-dm declares a
    max_length whose write_block request and read_block answer fit its max_frame; on the bench both run at it."""
    for profile in (vb.p4_x035, vb.p4_bench, vb.esp32_v003, vb.esp32_v003_64, vb.rp2350_pins):
        ep = build(profile)
        dm = ep.fns[RV.name]
        words = struct.unpack("<H", next(v for t, v in describe(ep, dm) if t == catalog.MAX_LENGTH))[0] // 4
        write = m.Request(1, dm, RV.op["write_block"], struct.pack("<IIH", 1, 0, words) + bytes(4 * words), SID).pack()
        assert len(write) <= ep.probe.max_frame and m.RESULT_HEADER + 3 + 4 * words <= ep.probe.max_frame, profile
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    conn = attach(w)
    w.ok(2, RV.op["halt"], struct.pack("<I", conn))
    words = ep.block_max[2] // 4
    body = struct.pack("<IIH", conn, 0x2000_0000, words)
    assert w.raw(2, RV.op["write_block"], body + bytes(4 * words)).succeeded
    out = ep.handle(w.bytes(2, RV.op["read_block"], body), 0)
    assert m.Result.unpack(out).succeeded and len(out) <= ep.probe.max_frame


def test_a_reboot_drops_the_subscriptions_and_the_resend_state():
    """core §9 (the restart row), §6.5: after a restart the subscriptions are gone, nothing is pushed, and the resend
    state is gone too - the old session's request sent again gets no recorded answer (no_session)."""
    ep, hst, lc, clock = streaming()
    data = m.Request(hst.next_corr(hst.session), 0, m.OP_KEEPALIVE, b"", hst.session).pack()
    assert m.Result.unpack(ep.handle(data, 0)).succeeded and ep.subscribed and ep.resend
    ep.reboot()
    assert not ep.subscribed and not ep.resend
    rejected(m.Result.unpack(ep.handle(data, 0)), m.NO_SESSION)
    clock.t += 5
    assert ep.pushes() == []


def test_resource_number_0xffffffff_is_given_and_only_then_cause_2():
    """core §9: the u32 space is used to its end - 0xFFFFFFFF itself is given; only after it is a new resource refused
    unavailable cause 2 (the one given still works)."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    ep._next_resource = 0xFFFFFFFF
    assert attach(w) == 0xFFFFFFFF
    r = rejected(w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 4, 5, 4_000_000)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "limit"
    assert w.raw(2, RV.op["halt"], struct.pack("<I", 0xFFFFFFFF)).succeeded


def test_an_attach_refused_for_spent_numbers_touches_nothing_first():
    """core §9, §4.3 order 4: with the u32 space spent, an attach is refused unavailable cause 2 before anything changes -
    no DMI bring-up on the target, its debug module as it was (review 4: the bench brought the target up, then found no
    number to give)."""
    ep = build(vb.p4_bench)
    w = Wire(ep)
    w.open()
    ep._next_resource = 0x1_0000_0000
    tg = ep._target(1, (4, 5))
    before = (list(tg.bringup), dict(tg.dmi), tg.halted, tg.havereset)
    r = rejected(w.raw(1, RVSWD.op["attach"], struct.pack("<BHHI", 1, 4, 5, 4_000_000)), m.UNAVAILABLE)
    assert h.Unavailable(r).cause == "limit"
    assert (list(tg.bringup), dict(tg.dmi), tg.halted, tg.havereset) == before


def test_an_answer_on_another_transport_does_not_wait_for_the_old_ones_frames():
    """core §10.3: a subscribe again from another transport is answered there at once - it does not wait for the frames
    the old transport still has queued; those, the old subscription's unsent ones, are dropped, and the new
    subscription's go to the new transport from seq 0."""
    from oep_client import cobs, virtual_bench_serial
    ep, hst, lc, clock = streaming()
    old, new = virtual_bench_serial.VirtualSerialPort(ep, 0), virtual_bench_serial.VirtualSerialPort(ep, 3)

    def out(port):
        return [cobs.unframe(c) for c in port.output(1 << 20).split(b"\x00") if c]
    clock.t += 1
    old.tick()
    assert any(n is not None for n in old.notice)                             # the old one's frames wait there
    corr = hst.next_corr(hst.session)
    new.feed(cobs.frame(m.Request(corr, lc.fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), hst.session).pack()))
    got = out(new)
    assert [f[0] for f in got] == [m.ROLE_RESULT] and m.Result.unpack(got[0]).corr == corr
    assert out(old) == []
    clock.t += 1
    new.tick()
    pushed = out(new)
    assert pushed and struct.unpack_from("<H", pushed[0], 3)[0] == 0


def test_every_request_that_came_is_answered_before_a_new_notification():
    """core §10.4 rule 1: every request that has come in is answered before a new notification frame starts - two
    requests arriving together on the bench's serial port, both answers first, then the notifications."""
    from oep_client import cobs, virtual_bench_serial
    ep, hst, lc, clock = streaming()
    port = virtual_bench_serial.VirtualSerialPort(ep, 0)
    clock.t += 20
    port.feed(cobs.frame(m.Request(60001, 0, m.OP_CLOCK, b"", 0).pack())
              + cobs.frame(m.Request(60002, 0, m.OP_LOCK_STATE, b"", 0).pack()))
    port.tick()
    got = [cobs.unframe(c) for c in port.output(1 << 20).split(b"\x00") if c]
    assert [m.Result.unpack(f).corr for f in got[:2]] == [60001, 60002]
    assert got[2:] and all(f[0] in (m.ROLE_DATA, m.ROLE_EVENT) for f in got[2:])


def test_every_describe_tlv_fits_the_frame_and_a_page_never_splits_one():
    """core §12 item 2 (and §7.3): what an interface declares fits every transport - each describe TLV value is at most
    max_frame - 9 bytes - and describe's pages carry whole TLVs (each page parses alone, more = 1 never on an empty
    page), for fn 0 and every listed fn of every profile."""
    for profile in (vb.p4_x035, vb.p4_bench, vb.esp32_v003, vb.esp32_v003_64, vb.rp2350_pins, vb.core_v1):
        ep = build(profile)
        for fn in [0] + [e.fn for e in entries(ep)[1]]:
            w, first = Wire(ep), 0
            while True:
                raw = ep.handle(w.bytes(0, m.OP_DESCRIBE, struct.pack("<HH", fn, first), sid=0), 0)
                assert len(raw) <= ep.probe.max_frame
                r = m.Result.unpack(raw)
                page = m.split_tlvs(r.payload[1:])                            # a cut TLV raises
                assert all(len(v) <= ep.probe.max_frame - 9 for _, v in page), (profile.__name__, fn)
                first += len(page)
                if not r.payload[0]:
                    break
                assert page


def test_the_family_an_interface_is_for_is_in_the_registry():
    """core §12 rule 8 (and rule 1): an `oep.` interface for one target family says so in the registry's `target`
    (the wires and target operations of one architecture), a generic one names none; the name says neither "generic"
    nor which families."""
    specific = {"oep.wire.rvswd", "oep.wire.swio", "oep.wire.swd", "oep.target.riscv-dm", "oep.target.arm-adi"}
    assert specific <= set(reg.INTERFACES)
    for i in reg.INTERFACES.values():
        assert (i.target is not None) == (i.name in specific), i.name
        assert not {"generic", "any", "all", "common"} & set(i.name.replace("-", ".").split(".")), i.name


def test_the_revision_a_confirm_chose_is_kept_per_transport_until_the_next():
    """core §7.1: the revision a confirm chose holds on the transport it came on (a TCP connection is one) until the
    next confirm there - each records revision 1, a later confirm on one transport leaves the others as they were."""
    ep = build()
    Wire(ep, transport=0).ok(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01", sid=0)
    assert ep.revision_in_use == {0: 1}
    ep.handle(m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x05", 0).pack(), 2, link="tcp-a")
    assert ep.revision_in_use == {0: 1, "tcp-a": 1}
    rejected(m.Result.unpack(ep.handle(m.Request(2, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x02\x05", 0).pack(), 2,
                                       link="tcp-b")), m.UNSUPPORTED)
    assert ep.revision_in_use == {0: 1, "tcp-a": 1}                           # no revision chosen there


def test_a_label_set_in_the_settings_is_not_in_describe():
    """core §7.5 (label), §7.3: describe's label is the firmware's fixed name - a label set in the settings is read by
    probe.config's op and never shows in fn 0's describe (asked afresh, not from a host's cache)."""
    ep = build()
    w = Wire(ep)
    w.open()
    before = describe(ep)
    item = m.tlv(__import__("oep_client.config", fromlist=["ITEM"]).ITEM["label"], struct.pack("<H", 20) + b"from-settings")
    assert cfg_set(w, ep, item).succeeded
    assert describe(ep) == before
    assert all(b"from-settings" not in v for _, v in describe(ep))


@pytest.mark.parametrize("min_bytes, max_delay_ms", [(0, 0), (0, 5), (100, 0)])
def test_answers_go_before_notifications_whichever_condition_makes_data_due(min_bytes, max_delay_ms):
    """core §10.3, §10.4 rule 1: data made due by max_delay_ms (or min_bytes, or at once) still starts only after every
    request that came is answered - max_delay_ms does not overrule the answers going first."""
    from oep_client import cobs, virtual_bench_serial
    ep, hst, lc, clock = streaming(min_bytes=min_bytes, max_delay_ms=max_delay_ms)
    port = virtual_bench_serial.VirtualSerialPort(ep, 0)
    clock.t += 20
    port.tick()                                                                # the max_delay_ms timer starts
    port.output(1 << 20)
    clock.t += 5                                                               # data due under every condition
    port.feed(cobs.frame(m.Request(60001, 0, m.OP_CLOCK, b"", 0).pack())
              + cobs.frame(m.Request(60002, 0, m.OP_LOCK_STATE, b"", 0).pack()))
    port.tick()
    got = [cobs.unframe(c) for c in port.output(1 << 20).split(b"\x00") if c]
    assert [m.Result.unpack(f).corr for f in got[:2]] == [60001, 60002]
    assert any(f[0] == m.ROLE_DATA for f in got[2:]) and all(f[0] in (m.ROLE_DATA, m.ROLE_EVENT) for f in got[2:])


def _describing(per_fn):
    """A host whose probe answers describe with `per_fn[fn]` (the TLV bytes, one page) - nothing else."""
    def send(raw):
        req = m.Request.unpack(raw)
        assert (req.fn, req.op) == (0, m.OP_DESCRIBE)
        fn, first = struct.unpack_from("<HH", req.payload)
        tlvs = catalog.split_tlv(per_fn[fn])[first:]
        return m.Result(req.corr, m.COMPLETED, m.SUCCESS, b"\x00" + b"".join(m.tlv(t, v) for t, v in tlvs)).pack()
    return h.Host(send, rng=random.Random(7))


def test_the_host_uses_the_first_copy_of_a_describe_tag_that_does_not_repeat():
    """core §2.3: a tag the definition does not repeat, sent twice: the reader uses the first. ops (0x07) and max_op_ms
    (0x4D) are such tags (core §7.4, §7.5): a second ops never adds ops, a second max_op_ms never stands in for a
    broken first one."""
    max_op = reg.CORE.tlv["describe"]["max_op_ms"]
    fn0 = (m.tlv(m.TAG_OPS, catalog.pack_ops(m.CORE_OPS)) + m.tlv(m.TAG_OPS, catalog.pack_ops({0x20}))
           + m.tlv(max_op, b"\x10\x27\x00") + m.tlv(max_op, struct.pack("<I", 5000)))
    fn3 = m.tlv(m.TAG_OPS, catalog.pack_ops({0x10})) + m.tlv(m.TAG_OPS, catalog.pack_ops({0x11}))
    hst = _describing({0: fn0, 3: fn3})
    assert core.ops(hst, 3) == {0x10} and not core.offers(hst, 3, 0x11)
    assert core.ops(hst, 0) == set(m.CORE_OPS)
    assert hst.max_op_ms() is None and core.max_op_ms(hst) == core.FALLBACK_MAX_OP_MS   # the first is broken: unused
    d = catalog.decode_description(fn3 + m.tlv(catalog.MAX_LENGTH, b"\x01") + m.tlv(catalog.MAX_LENGTH, b"\x08\x00"))
    assert d.ops == {0x10} and d.max_length is None
    from oep_client import dump
    missing = dump.required_missing(b"", catalog.split_tlv(m.tlv(m.TAG_OPS, catalog.pack_ops({1, 2, 3, 4}))
                                                           + m.tlv(m.TAG_OPS, catalog.pack_ops(m.CORE_OPS))))
    assert any(x.startswith("describe of fn 0: ops ") for x in missing)        # the second ops does not count


def test_the_hosts_clock_waits_as_long_as_any_quick_core_op():
    """core §7.6 with §4.4: clock is one of fn 0's quick ops - its answer is waited for min(max_op_ms,
    core_quick_op_max_ms) (core_quick_op_max_ms before max_op_ms is known) + host_wait_add_ms + the transfer time, as
    for confirm, list or describe; the host's clock sends it with that processing bound, never with none."""
    seen = []
    ep = build()

    def send(raw):
        seen.append(hst.expect_ms)
        return ep.handle(raw, 0)
    hst = h.Host(send, rng=random.Random(7))
    hst.clock()
    assert seen[-1] == reg.TIMING["core_quick_op_max_ms"]
    hst._describes[0] = [(reg.CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 300))]
    hst.clock()
    assert seen[-1] == 300 and hst.expect_ms == 0
