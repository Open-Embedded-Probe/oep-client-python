"""oep.wire.rvswd / swio / swd, oep.target.riscv-dm (oep-spec a09fa45 interfaces/oep-if-debug.ja.md) on the virtual
bench and through the client: the rules tests/test_debug_attach.py, tests/test_interfaces.py, tests/test_target_parts.py
and the debug checkers leave to the trace - the wires' mandatory ops and the lock, max_connections, max_speed against
min_clock_hz, the target_id values that mean none, detach and a probe restart, the connections list's order and pages,
attach method 1 and the pending havereset, swio's fixed speed, resume's answer, the dmi step kinds, and the client's
abstract command sequences in one dmi request."""

import struct

import pytest

from oep_client import catalog, console, core, endpoint, riscv, virtual_bench, host as h, message as m, registry as reg

from test_virtual_bench_spec import Clock, Host

W = reg.WIRE_RVSWD.op
RV = reg.TARGET_RISCV_DM.op
STEP = reg.TARGET_RISCV_DM.enum["dmi_step"]
UNA = reg.CORE.tlv["unavailable_payload"]
T_ATTACH = reg.WIRE_RVSWD.tlv["attach"]
T_SCAN = reg.WIRE_RVSWD.tlv["scan"]
T_ANSWER = reg.WIRE_RVSWD.tlv["attach_answer"]
FLAGS = reg.WIRE_RVSWD.enum["attach_flags"]
WIRES = ("oep.wire.rvswd", "oep.wire.swio", "oep.wire.swd")


def bench(probe=None):
    ep = endpoint.Endpoint(probe or virtual_bench.p4_bench(), Clock())
    hv = Host(ep)
    assert hv.open().succeeded
    return ep, hv


def fixed(pair, hz=4_000_000, method=0):
    return struct.pack("<BHHI", method, *pair, hz)


def attach(hv, pair, *tlvs, hz=4_000_000, method=0, fn=1):
    r = hv.raw(fn, W["attach"], fixed(pair, hz, method) + b"".join(tlvs))
    assert r.succeeded, r.describe()
    return r


def rows(payload):
    """connections' answer: more, count, entries (connection, swdio, swclk, speed_hz, tid_scheme, tid)."""
    rd = m.Reader(payload)
    more, count = rd.take("BB")
    out = []
    for _ in range(count):
        conn, swdio, swclk, speed, scheme, n = rd.take("IHHIBB")
        out.append((conn, (swdio, swclk), speed, scheme, rd.bytes(n)))
    return more, out


def wire_with(probe, name, change):
    """`probe` with the describe TLVs of its `name` fn passed through `change` (a list of TLVs -> a list)."""
    return virtual_bench._again(probe, [virtual_bench._with(o, change(list(o.tlvs))) if o.name == name else o
                                        for o in probe.offered])


# ---- §0: the wires' ops; the target interfaces before an attach; the lock ---------------------------------------

@pytest.mark.parametrize("profile", sorted(virtual_bench.PROFILES))
def test_every_wire_offers_attach_detach_connections_and_declares_scan_in_its_ops(profile):
    """debug §0 rule 1: scan 0x10, attach 0x11, detach 0x12, connections 0x13; attach / detach / connections mandatory,
    scan optional and declared in ops (core §7.4); the target interfaces are listed before any attach."""
    ep, hv = bench(virtual_bench.PROFILES[profile]())
    assert (W["scan"], W["attach"], W["detach"], W["connections"]) == (0x10, 0x11, 0x12, 0x13)
    for name in WIRES:
        assert reg.INTERFACES[name].op == W                        # the three wires share their numbers
    hst = h.Host(lambda b: ep.handle(b, 1))
    names = {e.name for e in core.list_entries(hst)}
    wires = [fn for fn, n in ep.names.items() if n in WIRES]
    assert wires and "oep.target.riscv-dm" in names and not ep.conns   # listed before an attach
    for fn in wires:
        ops = core.ops(hst, fn)
        assert {W["attach"], W["detach"], W["connections"]} <= ops
        pair = ep.pairs[fn][0] if ep.pairs.get(fn) else ep._role_pairs(fn)[0]
        r = hv.raw(fn, W["scan"], b"\x01" + struct.pack("<HH", *pair))
        assert (r.detail == m.UNKNOWN_OPERATION) == (W["scan"] not in ops)   # scan answers where ops declares it


def test_target_ops_need_a_connection_and_every_op_but_connections_the_lock():
    """debug (the interfaces' rules before §0): a target request naming no connection is no_resource; every op but
    connections needs the lock (session_required without it); connections answers without it."""
    ep, hv = bench()
    r = hv.raw(2, RV["halt"], struct.pack("<I", 0x7777))
    assert (r.resolution, r.detail) == (m.REJECTED, m.NO_RESOURCE)
    pair = ep.pairs[1][0]
    for fn, op, payload in ((1, W["attach"], fixed(pair)), (1, W["scan"], b"\x01" + struct.pack("<HH", *pair)),
                            (1, W["detach"], struct.pack("<I", 1)), (2, RV["dmi"], struct.pack("<IH", 1, 0))):
        r = hv.raw(fn, op, payload, session=False)
        assert (r.resolution, r.detail) == (m.REJECTED, m.SESSION_REQUIRED), (fn, op)
    assert not ep.conns
    cid = struct.unpack_from("<I", attach(hv, pair).payload)[0]
    other = Host(ep, session=0x77)
    r = other.raw(1, W["connections"], b"\x00", session=False)    # lock-free, while 0x51 holds the lock
    assert r.succeeded and rows(r.payload)[1][0][0] == cid


# ---- §1: max_connections, max_speed, target_id ------------------------------------------------------------------

def test_max_connections_is_a_u8_and_one_when_not_declared():
    """debug §1: describe's max_connections (tag 0x40, u8) bounds the wire's connections; absent, it is 1 - the next
    attach is unavailable cause 2 and no connection is closed to make room."""
    assert virtual_bench.MAX_CONNECTIONS == 0x40
    ep, hv = bench()
    (v,) = [v for t, v in ep.decl[1] if t == virtual_bench.MAX_CONNECTIONS]
    assert len(v) == 1 and v[0] == 2
    probe = wire_with(virtual_bench.p4_bench(), "oep.wire.rvswd",
                      lambda tlvs: [t for t in tlvs if t[0] != virtual_bench.MAX_CONNECTIONS])
    ep, hv = bench(probe)
    first = struct.unpack_from("<I", attach(hv, ep.pairs[1][0]).payload)[0]
    r = hv.raw(1, W["attach"], fixed(ep.pairs[1][1]))
    assert r.detail == m.UNAVAILABLE and dict(m.split_tlvs(r.payload)) == {UNA["cause"]: b"\x02"}
    assert list(ep.conns) == [first]


def test_max_speed_under_min_clock_is_unsupported_attach_fixed_part_scan_the_tag():
    """debug §1: a max_speed under the wire's min_clock_hz is unsupported - attach with the fixed part's mark 0x00, scan
    with the max_speed tag as received; 0 is malformed in both; the speed chosen is at most max_speed."""
    probe = wire_with(virtual_bench.p4_bench(), "oep.wire.rvswd",
                      lambda tlvs: tlvs + [catalog.u32(catalog.MIN_CLOCK_HZ, 100_000)])
    ep, hv = bench(probe)
    pair = ep.pairs[1][0]
    r = hv.raw(1, W["attach"], fixed(pair, 99_999))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00") and not ep.conns
    assert hv.raw(1, W["attach"], fixed(pair, 0)).detail == m.MALFORMED
    for raw in (T_SCAN["max_speed"] | 0x80, T_SCAN["max_speed"]):
        r = hv.raw(1, W["scan"], b"\x01" + struct.pack("<HH", *pair) + m.tlv(raw, struct.pack("<I", 99_999)))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([raw]))
        r = hv.raw(1, W["scan"], b"\x01" + struct.pack("<HH", *pair) + m.tlv(raw, bytes(4)))
        assert r.detail == m.MALFORMED
    r = attach(hv, pair, hz=100_000)
    assert struct.unpack_from("<IIBI", r.payload)[3] <= 100_000


@pytest.mark.parametrize("value,reported", [(0, False), (0xFFFFFFFF, False), (0x035E0601, True)])
def test_target_id_scheme_1_is_not_reported_for_its_none_values(value, reported):
    """debug §1, §3: attach may report target_id (TLV 0x10: scheme(u8) value); scheme 1 is the u32 at DMI 0x7F, length
    4, and 0 / 0xFFFFFFFF mean none - not reported, and connections says tid_scheme 0, tid_len 0."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    ep.targets[(1, pair)].target_id = value
    r = attach(hv, pair)
    tail = dict(m.split_tlvs(r.payload[13:]))
    want = bytes([reg.COMMON.enum["target_id_scheme"]["dmi_7f"]]) + struct.pack("<I", value)
    assert tail.get(T_ANSWER["target_id"]) == (want if reported else None)
    (row,) = rows(hv.raw(1, W["connections"], b"\x00", session=False).payload)[1]
    assert row[3:] == ((1, struct.pack("<I", value)) if reported else (0, b""))


# ---- §2: detach, a probe restart -------------------------------------------------------------------------------

def test_detach_answers_nothing_and_releases_the_connection_and_its_streams():
    """debug §2, §3: detach connection(u32) [TLV] -> no payload; the connection and its console stream go, the pins
    back to their idle state; a request naming either is no_resource."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", attach(hv, pair).payload)[0]
    con = reg.TARGET_CONSOLE
    sid = struct.unpack_from("<I", hv.ok(3, con.op["open"], struct.pack("<IB", cid, console.Console.DMSEQ)))[0]
    r = hv.raw(1, W["detach"], struct.pack("<I", cid))
    assert r.succeeded and r.payload == b""
    assert not ep.conns and not ep.streams and rows(hv.raw(1, W["connections"], b"\x00", session=False).payload)[1] == []
    assert all(ep.pin_state(ch).startswith("idle") for ch in pair)
    assert hv.raw(2, RV["halt"], struct.pack("<I", cid)).detail == m.NO_RESOURCE
    assert hv.raw(3, con.op["read"], struct.pack("<IBQH", sid, 1, 0, 16), session=False).detail == m.NO_RESOURCE


def test_a_probe_restart_drops_the_connections_and_leaves_the_hart_as_it_was():
    """debug §2's table: a probe restart - the connections and streams are gone (no number names them), the procedure
    neither resets nor resumes the hart (a halted hart stays halted)."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", attach(hv, pair, method=1).payload)[0]
    tg = ep.targets[(1, pair)]
    assert tg.halted
    dpc = tg.dpc
    ep.reboot()
    assert not ep.conns and not ep.streams and (tg.halted, tg.dpc) == (True, dpc)
    hv = Host(ep)
    assert hv.open().succeeded
    assert rows(hv.raw(1, W["connections"], b"\x00", session=False).payload)[1] == []
    assert hv.raw(2, RV["halt"], struct.pack("<I", cid)).detail == m.NO_RESOURCE


@pytest.mark.parametrize("how", ["detach", "end", "lapse", "force"])
def test_closing_a_connection_or_ending_the_session_leaves_a_halted_hart_halted(how):
    """debug §2 (the table, the safety note): detach, end, lease expiry and force release the connection and its stream
    and touch no hart - one halted stays halted where it was (the probe neither resets nor resumes it)."""
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), clock)
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(1000)
    pair = ep.pairs[1][0]
    conn, _ = riscv.Wire(hst).attach(halt=True, pins=pair)
    sid = console.Console(hst).open(conn, console.Console.DMSEQ)
    tg = ep.targets[(1, pair)]
    state = (tg.halted, tg.dpc, tg.haltreq, dict(tg.regs))
    assert tg.halted
    if how == "detach":
        riscv.Wire(hst).detach(conn)
    elif how == "end":
        hst.end()
    elif how == "lapse":
        clock.t = 5000
        ep.tick()
    else:
        assert Host(ep, session=0x99).open(force=1).succeeded
    assert conn not in ep.conns and sid not in ep.streams
    assert (tg.halted, tg.dpc, tg.haltreq, dict(tg.regs)) == state


# ---- §2.1: connections ------------------------------------------------------------------------------------------

def many_seats(seats=6, max_frame=64):
    """rp2350-pins (any pair the host names) with `seats` connections and a small frame."""
    p = wire_with(virtual_bench.rp2350_pins(), "oep.wire.rvswd",
                  lambda tlvs: [catalog.u8(virtual_bench.MAX_CONNECTIONS, seats) if t[0] == virtual_bench.MAX_CONNECTIONS
                                else t for t in tlvs])
    return virtual_bench.VirtualProbe(p.label, max_frame, p.offered, own_channels=p.own_channels)


def test_connections_lists_the_live_connections_in_creation_order_a_frame_at_a_time():
    """debug §2.1: the wire's live connections in the order they were made, from the first-th, as many as one frame
    holds; more = 1 when some are left and the host asks again with first + the count it got. Lock-free."""
    ep, hv = bench(many_seats())
    pairs = [(2 * i, 2 * i + 1) for i in range(6)]
    made = []
    for p in pairs:
        ep._target(1, p).present = True
        made.append(struct.unpack_from("<I", attach(hv, p).payload)[0])
    hv.raw(1, W["detach"], struct.pack("<I", made.pop(2)))           # a gap: the live ones only
    got, first = [], 0
    while True:
        r = hv.raw(1, W["connections"], bytes([first]), session=False)
        assert r.succeeded and len(r.payload) + m.RESULT_HEADER <= ep.probe.max_frame
        more, page = rows(r.payload)
        assert page
        got += page
        first += len(page)
        if not more:
            break
        assert first < len(made)
    assert first > len(rows(hv.raw(1, W["connections"], b"\x00", session=False).payload)[1])   # it took pages
    assert [g[0] for g in got] == made and [g[1] for g in got] == [p for i, p in enumerate(pairs) if i != 2]
    assert rows(hv.raw(1, W["connections"], bytes([len(made)]), session=False).payload) == (0, [])


# ---- §3: method, flags, havereset --------------------------------------------------------------------------------

def test_method_1_only_where_attach_halt_is_declared_and_method_0_leaves_the_hart_running():
    """debug §3: method 0 is mandatory; method 1 only on a wire declaring features bit0 attach_halt - elsewhere, and any
    undefined method, unsupported with the fixed part's mark; method 1 succeeds with hart 0 halted (flags bit3, dpc
    TLV); method 0 asks no halt (bit3 clear, no dpc)."""
    halt_bit = reg.WIRE_RVSWD.enum["features"]["attach_halt"]
    ep, hv = bench()
    p0, p1 = ep.pairs[1][:2]
    r = attach(hv, p0)
    assert not struct.unpack_from("<IIBI", r.payload)[2] & FLAGS["halted"] and not ep.targets[(1, p0)].halted
    assert T_ANSWER["dpc"] not in dict(m.split_tlvs(r.payload[13:]))
    r = attach(hv, p1, method=1)
    assert struct.unpack_from("<IIBI", r.payload)[2] & FLAGS["halted"] and ep.targets[(1, p1)].halted
    assert dict(m.split_tlvs(r.payload[13:]))[T_ANSWER["dpc"]] == struct.pack("<I", ep.targets[(1, p1)].dpc)
    probe = wire_with(virtual_bench.p4_bench(), "oep.wire.rvswd",
                      lambda tlvs: [t for t in tlvs if t[0] != catalog.FEATURES])
    ep, hv = bench(probe)
    assert not ep.features(1) & halt_bit
    for method in (1, 2, 0xFF):
        r = hv.raw(1, W["attach"], fixed(ep.pairs[1][0], method=method))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00") and not ep.conns
    attach(hv, ep.pairs[1][0])


def test_attach_acknowledges_a_pending_havereset_and_says_so():
    """debug §3 flags bit0 / §4.6: attach acknowledges a pending havereset first (flags bit0); with none pending the bit
    is clear. bit1 is unassigned and stays 0 on these wires."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    tg = ep.targets[(1, pair)]
    tg.havereset = True
    r = attach(hv, pair)
    cid, _, flags, _ = struct.unpack_from("<IIBI", r.payload)
    assert flags & FLAGS["havereset_acked"] and not flags & 0b10 and not tg.havereset
    hv.ok(1, W["detach"], struct.pack("<I", cid))
    flags = struct.unpack_from("<IIBI", attach(hv, pair).payload)[2]
    assert not flags & FLAGS["havereset_acked"]


# ---- §3.2: swio's fixed speed ----------------------------------------------------------------------------------

def test_swio_declares_its_fixed_speed_as_min_clock_hz_and_attach_runs_at_it():
    """debug §3.2: swio's bit times are fixed, the probe chooses no speed - describe's min_clock_hz is the speed of a 0
    slot (1 / (its low + its high), 840-1060 ns + 240-270 ns), attach's speed_hz is that value, and a max_speed under it
    is unsupported (§1); a swio pair has swclk 0xFFFF."""
    ep, hv = bench(virtual_bench.esp32_v003())
    fn = ep.fns["oep.wire.swio"]
    hz = ep._min_clock(fn)
    assert 1e9 / (1060 + 270) <= hz <= 1e9 / (840 + 240)
    pair = ep.pairs[fn][0]
    assert pair[1] == 0xFFFF
    for max_speed in (hz, 4_000_000):
        r = attach(hv, pair, hz=max_speed, fn=fn)
        cid, _, _, speed = struct.unpack_from("<IIBI", r.payload)
        assert speed == hz
        (row,) = rows(hv.raw(fn, W["connections"], b"\x00", session=False).payload)[1]
        assert row[2] == hz
        hv.ok(fn, W["detach"], struct.pack("<I", cid))
    r = hv.raw(fn, W["attach"], fixed(pair, hz - 1))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    declared = core.describe(h.Host(lambda b: ep.handle(b, 1)), fn)
    assert catalog.decode_description(b"".join(catalog.tlv(t, v) for t, v in declared)).min_clock_hz == hz


# ---- §4: resume's answer; the dmi steps ------------------------------------------------------------------------

def test_resume_answers_status_alone():
    """debug §4: resume 0x12 - connection(u32) -> status(u8) [TLV]; ok once the hart left debug mode."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", attach(hv, pair, method=1).payload)[0]
    r = hv.raw(2, RV["resume"], struct.pack("<I", cid))
    assert r.succeeded and r.payload == bytes([reg.STATUS["ok"]]) and not ep.targets[(1, pair)].halted


def test_each_dmi_step_kind_takes_its_arguments_and_adds_its_value():
    """debug §4.1: 0x01 write address(u8) value(u32), no value; 0x02 read address(u8) -> the value; 0x03 poll by reads
    address mask value max_reads(u16) -> the last value read; 0x04 wait wait_us(u32), no value; 0x05 poll by time
    address mask value max_us(u32) -> the last value read."""
    assert STEP == {"write": 1, "read": 2, "poll_reads": 3, "wait_us": 4, "poll_us": 5}
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", attach(hv, pair, method=1).payload)[0]
    tg = ep.targets[(1, pair)]
    steps = (struct.pack("<BBI", 1, 0x05, 0x12345678) + struct.pack("<BB", 2, 0x05)
             + struct.pack("<BBIIH", 3, 0x05, 0xFF, 0x78, 3) + struct.pack("<BI", 4, 10)
             + struct.pack("<BBIII", 5, 0x05, 0xFF00, 0x5600, 100))
    r = hv.raw(2, RV["dmi"], struct.pack("<IH", cid, 5) + steps)
    assert r.succeeded
    done, status, nvals = struct.unpack_from("<HBH", r.payload)
    assert (done, status, nvals) == (5, reg.STATUS["ok"], 3)
    assert struct.unpack_from("<3I", r.payload, 5) == (0x12345678,) * 3 and tg.dmi[0x05] == 0x12345678
    for short in (b"\x01\x05\x00", b"\x03\x05" + bytes(9), b"\x04\x00", b"\x05\x05" + bytes(11)):
        assert hv.raw(2, RV["dmi"], struct.pack("<IH", cid, 1) + short).detail == m.MALFORMED, short


def test_the_client_puts_each_abstract_command_sequence_in_one_dmi_request():
    """debug §4.1: a host with the connection's console stream open puts an abstract command's sequence (DATA0 write,
    command, DATA0 read) in one dmi request - the client's read_register / write_register do, whether a stream is open
    or not."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    sent = []

    def link(b):
        sent.append(m.Request.unpack(b))
        return ep.handle(b, 1)
    hst = h.Host(link)
    hst.open(3000)
    conn, _ = riscv.Wire(hst).attach(halt=True, pins=ep.pairs[1][0])
    console.Console(hst).open(conn, console.Console.DMSEQ)
    dm = riscv.RiscvDm(hst, conn)
    ep._target_of(conn).regs[0x100A] = 0xCAFE
    for read, action in ((True, lambda: dm.read_register(0x100A)), (False, lambda: dm.write_register(0x100B, 7))):
        before = len(sent)
        got = action()
        dmi = [r for r in sent[before:] if r.fn == ep.fns["oep.target.riscv-dm"] and r.op == RV["dmi"]]
        commands = [r for r in dmi if bytes([STEP["write"], 0x17]) in r.payload]
        assert commands
        for r in commands:                                        # DATA0 and the command and the wait in the same one
            p = r.payload
            assert bytes([STEP["write"], 0x04]) in p and bytes([STEP["poll_reads"], 0x16]) in p
            if read:                                              # ... and the DATA0 read after the command's wait
                assert p.rfind(bytes([STEP["read"], 0x04])) > p.find(bytes([STEP["write"], 0x17]))
        if read:
            assert got == 0xCAFE
    assert ep._target_of(conn).regs[0x100B] == 7


# ---- §5: the host powers the debug domain up ---------------------------------------------------------------------

def test_the_host_powers_the_debug_domain_up_with_dp_writes():
    """debug §5: the probe's attach does not power the target's debug up - the host does, with DP writes: CTRL/STAT's
    CDBGPWRUPREQ / CSYSPWRUPREQ (bits 28 / 30), then reads it until both acks (bits 29 / 31) are set."""
    from oep_client import arm
    from test_target_parts import FNS, ScriptedHost, ok

    dp, written = {arm.DP_CTRL_STAT: 0}, []

    def transfer(p):
        n, at = struct.unpack_from("<H", p, 4)[0], 6
        values = []
        for _ in range(n):
            req, at = p[at], at + 1
            addr = (req >> 2 & 3) << 2
            assert not req & 1                                   # DP only
            if req & 2:
                values.append(dp[addr])
                if addr == arm.DP_CTRL_STAT and dp[addr] & 0x50000000 == 0x50000000:
                    dp[addr] |= 0xA0000000                       # acks follow the requests
            else:
                (value,), at = struct.unpack_from("<I", p, at), at + 4
                written.append((addr, value))
                dp[addr] = value
        return ok(struct.pack(f"<HBBH{len(values)}I", n, 0, 1, len(values), *values))

    hst = ScriptedHost({(FNS["oep.target.arm-adi"], arm.ArmAdi.TRANSFER): transfer})
    cs = arm.ArmAdi(hst, 1).power_up()
    assert (arm.DP_CTRL_STAT, 0x50000000) in written and cs & 0xF0000000 == 0xF0000000
    assert all(fn == FNS["oep.target.arm-adi"] for fn, _, _ in hst.log if fn)   # nothing asked of the wire


# ---- the spec's debug vectors under ids without spaces (tools/check_trace.py reads node ids split at whitespace) ----

import test_spec_vectors as sv                                            # noqa: E402 (called, not collected here)

DEBUG_PREFIXES = ("rvswd ", "swio ", "swd ", "riscv-dm ", "arm-adi ")


def _slug(case):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in case["name"])


@pytest.mark.parametrize("case", [c for c in sv.OPS if sv.followed(c) and c["name"].startswith(DEBUG_PREFIXES)], ids=_slug)
def test_debug_ops_vector_on_the_bench(case):
    """oep-if-debug §1-§4 (ops.json): the bench answers the case's request with the case's bytes."""
    sv.test_ops_vectors_from_the_virtual_bench_byte_for_byte(case)


@pytest.mark.parametrize("case", sv.DEBUG_CLIENT, ids=_slug)
def test_debug_vector_through_the_client(case):
    """oep-if-debug §1-§6 (ops.json): the client sends the case's request and reads its answer."""
    sv.test_debug_vectors_as_the_client_sends_and_reads_them(case)


@pytest.mark.parametrize("case", [c for c in sv.INTERFACE_REFUSALS if c["name"].startswith(DEBUG_PREFIXES)], ids=_slug)
def test_debug_refusal_vector_on_the_bench(case):
    """oep-if-debug §4.3 (refusals.json): the bench refuses as the case says."""
    sv.test_interface_refusals_from_the_virtual_bench(case)


def test_the_documents_interfaces_are_these_names_at_revision_1():
    """debug (the table before §0): oep.wire.rvswd / swio / swd and oep.target.riscv-dm / arm-adi, revision 1, in the
    registry and as the client's own interface classes use them."""
    from oep_client import arm
    names = ("oep.wire.rvswd", "oep.wire.swio", "oep.wire.swd", "oep.target.riscv-dm", "oep.target.arm-adi")
    assert all(reg.INTERFACES[n].revision == 1 for n in names)
    assert riscv.WireBase.REVISION == riscv.RiscvDm.REVISION == arm.ArmAdi.REVISION == 1
    assert arm.ArmAdi.NAME == "oep.target.arm-adi"


# ---- review 2026-10-15: rules the trace named no test for --------------------------------------------------------

def test_connections_tell_a_targetsel_of_0_from_none_with_page_relative_indexes():
    """debug §2.1: an swd entry with a TARGETSEL carries selected_target (entry_index(u8) targetsel(u32)), entry_index
    counted in its own page; a selector of 0 is told from none. The logical swd model lists 0 as a selection and the
    checker reads it so; the client reads each page's indexes from that page's first entry."""
    from test_conformance_wire_options import setup as wire_setup
    from oep_client.conformance_wire_options import WireOptionsChecks
    _, c, a = wire_setup()
    checks = WireOptionsChecks(c, a)
    assert checks.run()["status"] == "passed"
    checks.fresh()
    with c.holding(3000) as sid:
        zero, _ = checks.attached(sid, 3, targetsel=0)                 # listing() requires {0: 0}, not {}
        rows = checks.listing(3)
        assert [r[0][0] for r in rows] == [zero]
        checks.detach(sid, 3, zero)
        none, _ = checks.attached(sid, 3)                              # listing() requires no selected_target
        checks.detach(sid, 3, none)
    from oep_client import arm
    from test_target_parts import ScriptedHost, ok
    entry = lambda conn: struct.pack("<IHHIBB", conn, 10, 11, 1_000_000, 2, 4) + struct.pack("<I", 0x2BA01477)
    sel = lambda i, v: m.tlv(reg.WIRE_SWD.tlv["connections_answer"]["selected_target"], struct.pack("<BI", i, v))
    pages = {0: ok(b"\x01\x02" + entry(7) + entry(8) + sel(1, 0)), 2: ok(b"\x00\x01" + entry(9) + sel(0, 5))}
    hst = ScriptedHost({(4, riscv.Wire.CONNECTIONS): lambda p: pages[p[0]]})
    got = arm.SwdWire(hst).connections()
    assert [(r.connection, r.targetsel) for r in got] == [(7, None), (8, 0), (9, 5)]


def test_the_client_puts_a_haltreq_sequence_in_one_dmi_request():
    """debug §4.1: a host with the connection's console stream open puts a sequence that handles haltreq in one dmi
    request - the client's uiapduino.run_payload resumes into its payload and drops haltreq in one (stream or not)."""
    from oep_client import uiapduino
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    sent = []

    def link(b):
        sent.append(m.Request.unpack(b))
        return ep.handle(b, 1)
    hst = h.Host(link)
    hst.open(3000)
    wire = riscv.Wire(hst)
    wire_pins = ep.pairs[1][0]
    attach_ = wire.attach
    wire.attach = lambda **kw: attach_(pins=wire_pins, **kw)
    uiapduino.run_payload(hst, wire, [0x0000006F])               # a jump to itself
    dmi = [r for r in sent if r.fn == ep.fns["oep.target.riscv-dm"] and r.op == RV["dmi"]]
    control = [r for r in dmi if bytes([STEP["write"], 0x10]) in r.payload]
    assert len(control) == 1                                      # resumereq ... haltreq dropped: one request
    p = control[0].payload
    assert p.count(bytes([STEP["write"], 0x10])) == 3 and p.endswith(bytes([STEP["write"], 0x10]) + struct.pack("<I", 1))


def test_a_reset_op_acknowledges_havereset_and_clears_haltreq():
    """debug §4.3 / §4 table: after the reset the probe acknowledges havereset and clears haltreq (mode 2 stays
    halted) - DMSTATUS read with dmi afterwards shows no allhavereset / anyhavereset."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", attach(hv, pair).payload)[0]
    tg = ep.targets[(1, pair)]
    for mode in (0, 2):
        r = hv.raw(2, RV["reset"], struct.pack("<IB", cid, mode))
        assert r.succeeded and struct.unpack("<BBI", r.payload)[:2] == (reg.STATUS["ok"], 1)
        done, status, nvals, dmstatus = struct.unpack("<HBHI", hv.ok(2, RV["dmi"], struct.pack("<IHBB", cid, 1, 2, 0x11)))
        assert not dmstatus & 0xC0000 and not tg.havereset, mode     # havereset acknowledged
        assert not tg.haltreq and tg.halted == (mode == 2)


def test_high_level_ops_acknowledge_a_pending_havereset_and_raw_dmi_does_not():
    """debug §4.6: the automatic havereset acknowledgement is the high-level ops' (and the console read's): a halt on
    a target that reset by itself acknowledges it and the connection's console stream gets a restart mark (kind 0x41,
    detail 1 havereset); a raw dmi request adds no acknowledgement - DMSTATUS still shows it, no mark."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", attach(hv, pair).payload)[0]
    con = reg.TARGET_CONSOLE
    sid = struct.unpack_from("<I", hv.ok(3, con.op["open"], struct.pack("<IB", cid, console.Console.DMSEQ)))[0]
    tg = ep.targets[(1, pair)]
    tg.havereset = True                                            # the target reset itself
    read = struct.pack("<IHBB", cid, 1, 2, 0x11)
    assert struct.unpack("<HBHI", hv.ok(2, RV["dmi"], read))[3] & 0xC0000 == 0xC0000
    assert tg.havereset and ep.streams[sid].marks == []           # raw dmi: nothing added
    assert hv.raw(2, RV["halt"], struct.pack("<I", cid)).succeeded
    assert not tg.havereset and struct.unpack("<HBHI", hv.ok(2, RV["dmi"], read))[3] & 0xC0000 == 0
    kinds = con.enum["mark_kind"]
    assert [(mk[2], mk[4]) for mk in ep.streams[sid].marks] == [(kinds["restart"], con.enum["mark_detail_restart"]["havereset"])]


def test_regno_is_the_abstract_register_number():
    """debug §4.4: run's regno is the RISC-V abstract register number - a0 is 0x100A (x10): the client's a0 and the
    regno its run request carries (the logical run checker, test_conformance_run, holds the probe side: 0x100A is x10)."""
    assert riscv.REG_A0 == 0x100A and riscv.REG_A1 == 0x100B
    assert riscv.RiscvDm.run_body(0x20000000, [(riscv.REG_A0, 7)], 100)[-3:] == b"\x01" + struct.pack("<H", 0x100A)


@pytest.mark.parametrize("max_frame", [64, 120, 1024])
def test_the_host_sizes_a_block_by_max_length_and_its_routes_max_frame(max_frame):
    """debug §4.5 / §6: the host's read_block count is at most min(max_length, max_frame - 8) / 4, write_block's
    min(max_length, max_frame - 20) / 4 (answer header 5 + done 2 + status 1; request header 10 + connection 4 +
    address 4 + count 2): a route smaller than max_length is not sent a block it cannot carry."""
    from oep_client import arm
    from test_target_parts import ScriptedHost, FakeAdi, ok
    confirm = struct.pack("<4sBHIBI", b"OEP!", 1, max_frame, 4096, 8, 0x1234)
    model = FakeAdi()
    counts = []

    def count(handler):
        def run(p):
            counts.append((p[0:0] or handler.__name__, struct.unpack_from("<H", p, 8)[0]))
            return handler(p)
        return run
    hst = ScriptedHost({(0, m.OP_CONFIRM): lambda p: ok(confirm),
                        (5, arm.ArmAdi.TRANSFER): model.transfer, (5, arm.ArmAdi.READ_BLOCK): count(model.read_block),
                        (5, arm.ArmAdi.WRITE_BLOCK): count(model.write_block)}, max_length=1000)
    read_words, write_words = min(1000, max_frame - 8) // 4, min(1000, max_frame - 20) // 4
    dm = riscv.RiscvDm(hst, 1)
    assert (dm.read_words, dm.write_words, dm.max_words) == (read_words, write_words, write_words)
    adi = arm.ArmAdi(hst, 1, adiv6=True)
    ap = arm.MemAp(adi, 0x2000)
    ap.write_block(0x20000000, list(range(300)))
    assert ap.read_block(0x20000000, 300) == list(range(300))
    reads = [n for name, n in counts if name == "read_block"]
    writes = [n for name, n in counts if name == "write_block"]
    assert max(reads) == min(read_words, 300) and sum(reads) == 300
    assert max(writes) == min(write_words, 300) and sum(writes) == 300


def test_scan_leaves_the_target_as_it_was():
    """debug §1 (scan が書くもの): scan writes only the wake / set-up steps and dmactive, never the scratch (PROGBUF0),
    ABSTRACTAUTO nor anything a host left there, and leaves dmactive set - on the bench every DMI register but DMCONTROL's
    dmactive, every GPR, memory word and the hart's run state are as they were; a DMCONTROL that read dmactive 0 reads
    1 after the scan, one that read 1 is not written."""
    import copy
    ep, hv = bench()
    pairs = ep.pairs[1]
    for i, p in enumerate(pairs):
        tg = ep._target(1, p)
        tg.dmi[0x20], tg.dmi[0x18], tg.mem[0x2000_0000] = 0xDEADBEEF, 0x10000, 0x12345678   # PROGBUF0, ABSTRACTAUTO
        tg.dmi[0x10] = 0 if i == 0 else 1                                                     # dmactive 0 on the first
    before = {p: copy.deepcopy((ep._target(1, p).dmi, ep._target(1, p).regs, ep._target(1, p).mem,
                                ep._target(1, p).halted)) for p in pairs}
    r = hv.raw(1, W["scan"], bytes([len(pairs)]) + b"".join(struct.pack("<HH", *p) for p in pairs))
    assert r.succeeded
    setup = [(0x7E, 0x5AA50400), (0x7D, 0x5AA50400)] * 2
    for i, p in enumerate(pairs):
        tg = ep._target(1, p)
        dmi, regs, mem, halted = before[p]
        assert (tg.dmi, tg.regs, tg.mem, tg.halted) == ({**dmi, 0x10: 1}, regs, mem, halted), p   # dmactive left set
        assert tg.bringup == setup + ([(0x10, 1)] if i == 0 else []), p                         # nothing else written


# ---- review 2 (2026-10-15): rules the first review passed without a test that asserts them ------------------------

import copy                                                                 # noqa: E402

SETUP = [(0x7E, 0x5AA50400), (0x7D, 0x5AA50400)] * 2                        # rvswd / swio set-up pair, twice (§3)
HARTSEL = 0x3FF << 16 | 0x3FF << 6


def test_every_op_but_connections_needs_the_lock_and_two_connections_share_one_lock():
    """debug (before §0) / §1 同時に持てる接続の数: every op of the wire and of riscv-dm but connections needs the lock -
    each sent with session_id 0 is session_required; the probe keeps one lock however many connections there are:
    another session is refused locked on either connection (nothing runs), and its connections answers lock-free."""
    ep, hv = bench()
    p0, p1 = ep.pairs[1][:2]
    c0 = struct.unpack_from("<I", attach(hv, p0).payload)[0]
    c1 = struct.unpack_from("<I", attach(hv, p1).payload)[0]
    for op in W.values():
        if op != W["connections"]:
            r = hv.raw(1, op, fixed(ep.pairs[1][2]), session=False)
            assert (r.resolution, r.detail) == (m.REJECTED, m.SESSION_REQUIRED), op
    for op in RV.values():
        r = hv.raw(2, op, struct.pack("<I", c0) + bytes(12), session=False)
        assert (r.resolution, r.detail) == (m.REJECTED, m.SESSION_REQUIRED), op
    other = Host(ep, session=0x77)
    for cid in (c0, c1):
        for op in (RV["halt"], RV["dmi"]):
            r = other.raw(2, op, struct.pack("<I", cid) + (struct.pack("<HBBI", 1, 1, 0x10, 1 << 31) if op == RV["dmi"]
                                                          else b""))
            assert (r.resolution, r.detail) == (m.REJECTED, m.LOCKED), (cid, op)
    assert not ep.targets[(1, p0)].halted and not ep.targets[(1, p1)].halted
    r = other.raw(1, W["connections"], b"\x00", session=False)
    assert r.succeeded and [row[0] for row in rows(r.payload)[1]] == [c0, c1]


def test_each_target_request_picks_its_connection_by_the_number_it_starts_with():
    """debug (before §0): a connection keeps the wire's pin / target choice and each target request picks it by the
    connection number it starts with; the wire's fn and the target interface's fn need not be the same number."""
    ep, hv = bench()
    wire_fn, dm_fn = ep.fns["oep.wire.rvswd"], ep.fns["oep.target.riscv-dm"]
    assert wire_fn != dm_fn
    p0, p1 = ep.pairs[wire_fn][:2]
    c0 = struct.unpack_from("<I", attach(hv, p0, fn=wire_fn).payload)[0]
    c1 = struct.unpack_from("<I", attach(hv, p1, fn=wire_fn).payload)[0]
    t0, t1 = ep.targets[(wire_fn, p0)], ep.targets[(wire_fn, p1)]
    assert hv.raw(dm_fn, RV["halt"], struct.pack("<I", c1)).succeeded
    assert t1.halted and not t0.halted
    hv.ok(dm_fn, RV["dmi"], struct.pack("<IHBBI", c0, 1, STEP["write"], 0x04, 0xA5A5))
    assert t0.dmi[0x04] == 0xA5A5 and 0x04 not in t1.dmi


@pytest.mark.parametrize("wire", ["oep.wire.rvswd", "oep.wire.swio"])
def test_attach_writes_only_the_setup_dmactive_and_the_scratch_and_gives_progbuf0_back(wire):
    """debug §1 (速さを確かめる前の書き込み / 速さの確かめ方), §3 (wake / 設定の手順, scratch), §3.2 (the check at the fixed
    speed): before the speed is checked the probe writes only the set-up pair (DMI 0x7E, 0x7D = 0x5AA50400, twice) and
    dmactive when DMCONTROL does not read it; the check writes ABSTRACTAUTO = 0 (not given back) and PROGBUF0, which it
    read first and gives its value back after; nothing else of the target changes. A DMCONTROL already reading
    dmactive is not written (its hartsel stays: method 0 changes no hart selection)."""
    swio = wire == "oep.wire.swio"
    ep, hv = bench(virtual_bench.esp32_v003() if swio else None)
    fn = ep.fns[wire]
    pair = ep.pairs[fn][0]
    tg = ep.targets[(fn, pair)]
    tg.havereset = False
    tg.dmi.update({0x10: 0, 0x18: 0x10000, 0x20: 0xDEADBEEF, 0x21: 0x1234, 0x04: 7})
    tg.regs[0x100A], tg.mem[0x2000_0000] = 5, 9
    dmi, regs, mem, halted = copy.deepcopy((tg.dmi, tg.regs, tg.mem, tg.halted))
    cid = struct.unpack_from("<I", attach(hv, pair, fn=fn).payload)[0]
    assert tg.bringup[:4] == SETUP and tg.bringup[4] == (0x10, 1)             # before the check: these only
    check = tg.bringup[5:]
    assert check[0] == (0x18, 0) and [a for a, _ in check[1:]] == [0x20, 0x20]
    assert check[1][1] != 0xDEADBEEF and check[2] == (0x20, 0xDEADBEEF)       # written, then its value back
    assert (tg.dmi, tg.regs, tg.mem, tg.halted) == ({**dmi, 0x10: 1, 0x18: 0}, regs, mem, halted)
    hv.ok(fn, W["detach"], struct.pack("<I", cid))
    tg.bringup.clear()
    tg.dmi[0x10] = 1 | 1 << 16                                                 # dmactive, hartsel 1
    attach(hv, pair, fn=fn)
    assert all(a != 0x10 for a, _ in tg.bringup) and tg.dmi[0x10] == 1 | 1 << 16


def test_attach_fails_when_the_scratch_does_not_read_back_and_makes_nothing():
    """debug §1 速さの確かめ方: a speed whose write does not read back is not used, and a scratch whose value cannot be
    given back ends the attach completed failed with no connection made; the pins it tried are free again."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    tg = ep.targets[(1, pair)]
    tg.fail_write.add(0x20)
    r = hv.raw(1, W["attach"], fixed(pair))
    assert (r.resolution, r.detail, r.payload[:1]) == (m.COMPLETED, m.FAILED, bytes([reg.STATUS["line"]]))
    assert not ep.conns and all(ep.pin_state(ch).startswith("idle") for ch in pair)
    tg.fail_write.clear()
    attach(hv, pair)


def test_the_wires_ids_are_dmstatus_scan_kind_1_and_a_swio_connection_takes_the_console():
    """debug §3: on rvswd and swio a scan entry's kind is 1 (riscv-dm) and its id DMSTATUS, attach's id is DMSTATUS
    too; the connection is used by oep.target.riscv-dm and oep.target.console (a swio one included)."""
    for probe, wire in ((virtual_bench.p4_bench(), "oep.wire.rvswd"), (virtual_bench.esp32_v003(), "oep.wire.swio")):
        ep, hv = bench(probe)
        fn = ep.fns[wire]
        pair = ep.pairs[fn][0]
        tg = ep.targets[(fn, pair)]
        r = hv.ok(fn, W["scan"], b"\x01" + struct.pack("<HH", *pair))
        assert struct.unpack_from("<BBBHHI", r) == (1, 1, 1, *pair, tg.dmstatus()), wire
        r = attach(hv, pair, fn=fn)
        cid, ident = struct.unpack_from("<II", r.payload)
        assert ident == tg.dmstatus(), wire
        assert hv.raw(ep.fns["oep.target.riscv-dm"], RV["halt"], struct.pack("<I", cid)).succeeded
        con = reg.TARGET_CONSOLE
        assert hv.raw(ep.fns["oep.target.console"], con.op["open"],
                      struct.pack("<IB", cid, console.Console.DMSEQ)).succeeded, wire


def test_method_1_selects_hart_0_drops_haltreq_and_succeeds_only_with_hart_0_halted():
    """debug §3: method 1 sets hartsel to 0 and succeeds only when hart 0 halted and haltreq could be dropped - on a
    hart that never halts it fails (status timeout), makes no connection and leaves haltreq down; method 0 asks no
    halt and succeeds there."""
    ep, hv = bench()
    p0, p1 = ep.pairs[1][:2]
    t0, t1 = ep.targets[(1, p0)], ep.targets[(1, p1)]
    t0.dmi[0x10] = 1 | 3 << 16                                                 # dmactive, hartsel 3
    attach(hv, p0, method=1)
    assert t0.halted and not t0.dmi[0x10] & HARTSEL and not t0.dmi[0x10] & 1 << 31 and not t0.haltreq
    first = list(ep.conns)
    t1.halt_stuck = True
    r = hv.raw(1, W["attach"], fixed(p1, method=1))
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, bytes([reg.STATUS["timeout"]]))
    assert list(ep.conns) == first and not t1.halted and not t1.haltreq
    r = attach(hv, p1)
    assert not struct.unpack_from("<IIBI", r.payload)[2] & FLAGS["halted"] and not t1.halted


HIGH_LEVEL = {"halt": b"", "resume": b"", "reset": b"\x00", "step": b"",
              "read_block": struct.pack("<IH", 0x2000_0000, 1), "write_block": struct.pack("<IHI", 0x2000_0000, 1, 5),
              "run": riscv.RiscvDm.run_body(0x2000_0000, [], 100, ())}


@pytest.mark.parametrize("op", sorted(HIGH_LEVEL))
def test_a_high_level_op_leaves_hartsel_0_and_raw_dmi_keeps_the_hosts(op):
    """debug §4 (dmi 以外の op の範囲): a high-level op works on hart 0 - it sets DMCONTROL.hartsel to 0 and returns it 0;
    the hartsel the host chose with dmi is that dmi request's alone (raw dmi itself neither restores nor changes it)."""
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", attach(hv, pair, method=1).payload)[0]
    read = struct.pack("<IHBB", cid, 1, STEP["read"], 0x10)
    hv.ok(2, RV["dmi"], struct.pack("<IHBBI", cid, 1, STEP["write"], 0x10, 1 | 1 << 16))
    assert struct.unpack("<HBHI", hv.ok(2, RV["dmi"], read))[3] & HARTSEL == 1 << 16      # the host's, kept
    r = hv.raw(2, RV[op], struct.pack("<I", cid) + HIGH_LEVEL[op])
    assert r.resolution == m.COMPLETED, r.describe()
    assert struct.unpack("<HBHI", hv.ok(2, RV["dmi"], read))[3] & HARTSEL == 0, op


def _counting_client():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    sent = []

    def link(b):
        sent.append(m.Request.unpack(b))
        return ep.handle(b, 1)
    hst = h.Host(link)
    hst.open(3000)
    conn, _ = riscv.Wire(hst).attach(halt=True, pins=ep.pairs[1][0])
    return ep, hst, sent, riscv.RiscvDm(hst, conn), ep._target_of(conn)


@pytest.mark.parametrize("stopped", [True, False])
def test_the_client_never_sends_a_run_again_by_itself(stopped):
    """debug §4.4: the host does not take a dpc alone for "not run" - a run that stopped (or was stopped at the limit)
    where it began is answered to the caller as it is; the client sends the run request once and never again."""
    ep, hst, sent, dm, tg = _counting_client()
    tg.run_hook = lambda pc, regs: (stopped, pc, 7)                            # halted at its start
    res = dm.run(0x2000_0000, [], timeout_ms=100)
    assert res.dpc == 0x2000_0000 and res.stopped == stopped
    assert len([r for r in sent if r.fn == ep.fns["oep.target.riscv-dm"] and r.op == RV["run"]]) == 1


def test_the_host_waits_max_op_ms_for_run_and_dmi_not_their_own_times():
    """debug §4.4 / §4.1: the host's wait follows core §4.4 - the whole request's bound max_op_ms - and is not taken from
    run's timeout_ms nor dmi's wait_us / max_us alone."""
    ep, hst, sent, dm, tg = _counting_client()
    seen = []
    send = hst.send
    hst.send = lambda b: (seen.append((b[5], hst.expect_ms)), send(b))[1]
    dm.run(0x2000_0000, [], timeout_ms=5)
    dm.dmi([dm.step_delay(10), dm.step_read(0x11)])
    assert [ms for op, ms in seen if op in (RV["run"], RV["dmi"])][-2:] == [virtual_bench.MAX_OP_MS] * 2
    assert {op for op, _ in seen} >= {RV["run"], RV["dmi"]}
