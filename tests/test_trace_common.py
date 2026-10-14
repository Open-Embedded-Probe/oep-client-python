"""Rule-by-rule trace of oep-spec a09fa45 interfaces/oep-if-common.ja.md (docs/trace/common.json): positional streams
(§1), the debug connection (§2) and the status of wire and target operations (§3), checked on the virtual bench and the
client where no other test pins a rule down."""

import random
import struct

import pytest

from oep_client import console, core, endpoint, fixture, host as h, message as m, registry as reg, riscv, virtual_bench

CON = reg.TARGET_CONSOLE
MARK = {**reg.COMMON.enum["mark_kind"], **CON.enum["mark_kind"]}


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


@pytest.fixture
def bench():
    """p4-bench, a session open, an rvswd connection and a console stream on it."""
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), clock)
    hst = h.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=10000)
    conn, _ = riscv.Wire(hst).attach(halt=True, pins=ep.pairs[1][0])
    con = console.Console(hst)
    sid = con.open(conn)
    return ep, clock, hst, conn, con, sid


def raw_read(ep, sid, frm, arg=0, mx=0xFFFF, session=0, fn=3):
    r = m.Result.unpack(ep.handle(m.Request(0x7000, fn, CON.op["read"], struct.pack("<IBQH", sid, frm, arg, mx),
                                            session).pack()))
    return r


# ---- §1.1 position ----------------------------------------------------------------------------------------------

def test_a_stream_made_as_a_resource_starts_at_0_and_a_new_one_has_a_new_number(bench):
    """common §1.1: positions are u64 and never wrap; a stream with a resource number starts at 0 when made; making it
    again takes a new number (and starts at 0 again)."""
    ep, clock, hst, conn, con, sid = bench
    ep.emit(sid, b"abc")
    assert con.read(con.FROM_OLDEST).start == 0 and con.read(con.FROM_NOW, 0, 0).start == 3
    con.close()
    again = con.open(conn)
    assert again != sid and con.read(con.FROM_NOW, 0, 0).start == 0
    ep.streams[again].base = (1 << 40) + 5                        # far past u32: still a position, no wrap
    ep.emit(again, b"xy")
    assert tuple(con.read(con.FROM_POSITION, (1 << 40) + 6)) == ((1 << 40) + 6, False, False, b"y")


def test_reading_consumes_nothing_and_a_target_reset_drops_nothing(bench):
    """common §1.1: a read leaves the bytes; they go only by overflow, clear, the stream's release or the probe's
    restart - a target reset keeps them (the reset mark is the console's)."""
    ep, clock, hst, conn, con, sid = bench
    ep.emit(sid, b"before reset")
    assert con.read(con.FROM_OLDEST).data == con.read(con.FROM_OLDEST).data == b"before reset"
    riscv.RiscvDm(hst, conn).reset()                              # ndmreset: a target reset
    assert con.read(con.FROM_OLDEST).data == b"before reset"
    assert [(mk.kind, mk.position) for mk in con.marks()] == [(MARK["reset"], 12)]


def test_clear_release_and_restart_drop_the_bytes(bench):
    """common §1.1 / §1.4: clear (it needs the lock) drops the bytes kept and leaves a clear mark at the position; a
    released stream and a restarted probe keep nothing."""
    ep, clock, hst, conn, con, sid = bench
    ep.emit(sid, b"abc")
    r = m.Result.unpack(ep.handle(m.Request(0x7001, 3, CON.op["clear"], struct.pack("<I", sid)).pack()))
    assert r.detail == m.SESSION_REQUIRED                        # every state-changing op needs the lock
    con.clear()
    assert tuple(con.read(con.FROM_OLDEST)) == (3, False, False, b"")
    assert [(mk.kind, mk.position, mk.detail) for mk in con.marks()] == [(MARK["clear"], 3, 0)]
    con.close()
    with pytest.raises(h.NoResource):
        con.read()
    sid2 = con.open(conn)
    ep.emit(sid2, b"kept")
    ep.reboot()
    assert ep.streams == {}


# ---- §1.2 read --------------------------------------------------------------------------------------------------

def test_read_from_a_position_the_oldest_now_and_the_last_mark(bench):
    """common §1.2 from 0-3: from position arg; from the oldest byte kept; from now (nothing yet); from the last mark of
    kind arg (0: any kind) - and from now when no such mark is kept."""
    ep, clock, hst, conn, con, sid = bench
    ep.emit(sid, b"ab")
    con.mark(1)
    ep.emit(sid, b"cd")
    con.clear()                                                   # bytes 0-3 go, a clear mark at 4
    ep.emit(sid, b"ef")
    con.mark(2)
    ep.emit(sid, b"gh")
    assert tuple(con.read(con.FROM_POSITION, 5)) == (5, False, False, b"fgh")
    assert tuple(con.read(con.FROM_OLDEST)) == (4, False, False, b"efgh")
    assert tuple(con.read(con.FROM_NOW)) == (8, False, False, b"")
    assert tuple(con.read(con.FROM_MARK, MARK["host"])) == (6, False, False, b"gh")      # the last host mark
    assert tuple(con.read(con.FROM_MARK, MARK["clear"])) == (4, False, False, b"efgh")
    assert tuple(con.read(con.FROM_MARK, 0)) == (6, False, False, b"gh")                 # any kind: the last one
    assert tuple(con.read(con.FROM_MARK, MARK["lost"])) == (8, False, False, b"")        # none: from now


def test_read_from_a_mark_without_marks_is_unsupported(bench, monkeypatch):
    """common §1: a stream that offers no marks refuses read from 3 rejected unsupported, fixed-part mark 0."""
    ep, clock, hst, conn, con, sid = bench
    offers = ep.offers
    monkeypatch.setattr(ep, "offers", lambda fn, op: op != CON.op["marks"] and offers(fn, op))
    r = raw_read(ep, sid, 3, 0)
    assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, b"\x00")


def test_start_moves_on_with_gap_over_bytes_gone_and_a_future_position_reads_nothing(bench):
    """common §1.2: a position no longer kept starts at the oldest kept with gap (the difference is what was lost); a
    position past the write position answers start = the write position, len 0, flags 0."""
    ep, clock, hst, conn, con, sid = bench
    ep.emit(sid, b"abc")
    con.clear()
    ep.emit(sid, b"de")
    assert tuple(con.read(con.FROM_POSITION, 1)) == (3, False, True, b"de")
    r = raw_read(ep, sid, 0, 100)
    assert r.payload == struct.pack("<QBH", 5, 0, 0)


def test_len_stays_within_max_and_max_frame_and_more_says_bytes_are_left():
    """common §1.2: len <= max and what fits max_frame; more is set while bytes are left after this answer."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003_64(), Clock())
    hst = h.Host(ep.handle, rng=random.Random(2))
    hst.open()
    core.plan_apply(hst, [(5, 1, 21)])
    uart = fixture.FixtureUart(hst, 5)
    uart.configure(115200)
    ep.uart_rx(5, bytes(range(100)))
    c = uart.read(uart.FROM_OLDEST, 0, 3)
    assert (c.start, c.more, c.data) == (0, True, bytes(range(3)))
    raw = ep.handle(m.Request(hst.next_corr(), 5, reg.FIXTURE_UART.op["read"], struct.pack("<BQH", 1, 0, 0xFFFF)).pack())
    r = m.Result.unpack(raw)
    n = struct.unpack_from("<H", r.payload, 9)[0]
    assert len(raw) <= 64 and 0 < n < 100 and r.payload[8] & 1           # cut to max_frame, more set
    c = uart.read(uart.FROM_POSITION, 90, 1000)
    assert (c.start, c.more, len(c.data)) == (90, False, 10)


def test_read_and_marks_need_no_lock_even_while_another_session_holds_it(bench):
    """common §1.2 / §1.3: read and marks are lock-free - session_id 0 reads while a session holds the lock, and the
    probe keeps no reader's state."""
    ep, clock, hst, conn, con, sid = bench
    ep.emit(sid, b"data")
    con.mark(9)
    r = raw_read(ep, sid, 1)
    assert r.succeeded and r.payload[11:] == b"data"
    r = m.Result.unpack(ep.handle(m.Request(0x7002, 3, CON.op["marks"], struct.pack("<II", sid, 0)).pack()))
    assert r.succeeded and r.payload[:2] == b"\x00\x01"
    assert raw_read(ep, sid, 1).payload == raw_read(ep, sid, 1).payload        # no reader state, nothing consumed


# ---- §1.3 marks -------------------------------------------------------------------------------------------------

def test_mark_kinds_and_their_spaces():
    """common §1.3: lost 0x01 (detail mark_detail_lost: 1 overflow, 2 framing, 3 parity), clear 0x02, host 0x03 in the
    standard space 0x01-0x3F; an interface's own kinds (the console's reset / restart) and details from 0x40."""
    assert reg.COMMON.enum["mark_kind"] == {"lost": 1, "clear": 2, "host": 3}
    assert reg.COMMON.enum["mark_detail_lost"] == {"overflow": 1, "framing": 2, "parity": 3}
    assert all(0x01 <= v <= 0x3F for v in reg.COMMON.enum["mark_kind"].values())
    assert all(0x40 <= v <= 0x7F for v in CON.enum["mark_kind"].values())
    assert all(v >= 0x40 for v in CON.enum["mark_detail_lost"].values())        # the console's lost detail


def test_a_host_mark_carries_the_value_at_the_write_position_and_the_probe_clock(bench):
    """common §1.3 / §1.4: mark (it needs the lock) puts kind host with detail = value at the write position when it is
    made; time_ns is the probe's clock (ns since boot); every byte from position on came after it."""
    ep, clock, hst, conn, con, sid = bench
    ep.emit(sid, b"abc")
    clock.ms = 1234
    con.mark(0x5A)
    ep.emit(sid, b"de")
    (mk,) = con.marks()
    assert (mk.kind, mk.detail, mk.position) == (MARK["host"], 0x5A, 3)
    assert mk.time_ns == ep.now_ns() == 1234 * 1_000_000
    assert con.read(con.FROM_MARK, MARK["host"]).data == b"de"
    r = m.Result.unpack(ep.handle(m.Request(0x7003, 3, CON.op["mark"], struct.pack("<IB", sid, 1)).pack()))
    assert r.detail == m.SESSION_REQUIRED


def test_marks_page_by_serial(bench):
    """common §1.3 paging: serials per stream, in order, from_serial included; from next: none and more 0; a serial
    not kept (not given yet, pushed out, another boot's) from the oldest kept; more 1: the next page from the last
    serial + 1 - several marks at one position, none lost or repeated."""
    ep, clock, hst, conn, con, sid = bench
    s = ep.streams[sid]
    s.serial = 0xFFFFFFFE                                         # the serials wrap (core §2.6)
    for v in range(7):
        con.mark(v)
    page, more = con.marks_page(0xFFFFFFFE)
    assert [mk.serial for mk in page] == [0xFFFFFFFE, 0xFFFFFFFF, 0] and more
    page, more = con.marks_page(0xFFFFFFFF)
    assert page[0].serial == 0xFFFFFFFF                           # inclusive
    assert [mk.serial for mk in con.marks(0xFFFFFFFE)] == [0xFFFFFFFE, 0xFFFFFFFF, 0, 1, 2, 3, 4]
    assert all(mk.position == 0 for mk in con.marks(0xFFFFFFFE))
    assert con.marks_page(5) == ([], False)                       # next: nothing yet, more 0
    for gone in (6, 0x12345678, 0xFFFFFFF0):                      # not given yet / not kept: from the oldest
        assert con.marks_page(gone)[0][0].serial == 0xFFFFFFFE
    con.close()
    con.open(conn)
    assert con.marks_page(0) == ([], False)                       # none kept: as from next


def test_the_host_counts_lost_marks_by_the_forward_serial_jump():
    """common §1.3: marks are a small ring of their own; the host knows marks pushed out by the jump of the serials -
    the jump is how many went (paging 3, within one stream)."""
    assert m.serials_lost(10, 14) == 3 and m.serials_lost(0xFFFFFFFF, 2) == 2       # forward, across the wrap
    assert m.serials_lost(10, 14, same_stream=False) is None and m.serials_lost(14, 10) is None
    assert m.serial_page_start([5, 6, 7], 8, 2) == 0 and m.serial_page_start([5, 6, 7], 8, 6) == 1
    assert m.serial_page_start([5, 6, 7], 8, 8) == 3                       # next: nothing


# ---- §1.4 write -------------------------------------------------------------------------------------------------

def test_write_accepted_is_success_partial_or_failed():
    """common §1.4: accepted is what went into the probe's send queue from data's start, min(count, its room) - all:
    completed success, some: completed partial, none of count > 0: completed failed; delivery is not implied."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(ep.handle, rng=random.Random(4))
    hst.open()
    core.plan_apply(hst, [(5, 1, 21), (5, 2, 22)])
    fixture.FixtureUart(hst, 5).configure(115200)
    for room, data, detail, took in ((10, b"abc", m.SUCCESS, 3), (2, b"abc", m.PARTIAL, 2), (0, b"abc", m.FAILED, 0)):
        ep.uart_accept = room
        r = hst.request(5, reg.FIXTURE_UART.op["write"], struct.pack("<H", len(data)) + data)
        assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, detail, struct.pack("<H", took))
    assert bytes(ep.uarts[5].written) == b"abcab"


# ---- §1.5 notification data ---------------------------------------------------------------------------------------

# (common §1.5 is a paragraph, not a unit; capture's data frames follow core §10.2 - test_virtual_bench_capture.)


# ---- §2 the debug connection --------------------------------------------------------------------------------------

def test_a_connection_is_made_by_attach_named_first_by_target_ops_and_never_reused(bench):
    """common §2: attach makes a connection (u32) of the session on the pins and target named; a target op starts with
    connection(u32); numbers follow core §9 - not used again in one boot_id, a stream's number not a connection."""
    ep, clock, hst, conn, con, sid = bench
    dm = riscv.RiscvDm(hst, conn)
    dm.halt()
    assert ep.requests[-1].payload[:4] == struct.pack("<I", conn)
    seen = {conn, sid}
    w = riscv.Wire(hst)
    for _ in range(3):
        c, _ = w.attach(halt=False, pins=ep.pairs[1][1])
        assert c not in seen
        seen.add(c)
        w.detach(c)
    with pytest.raises(h.Unavailable) as e:
        riscv.RiscvDm(hst, sid).halt()                            # a stream's number: the wrong kind, cause 6
    assert e.value.cause == "wrong_state"
    with pytest.raises(h.NoResource):
        riscv.RiscvDm(hst, 0x7777).halt()                         # none: no_resource
    with pytest.raises(h.NoResource):
        riscv.RiscvDm(hst, c).halt()                              # closed: no_resource, not the last attached


@pytest.mark.parametrize("how", ["detach", "end", "lost", "cleanup"])
def test_a_connection_closes_on_detach_session_end_lost_line_or_failed_cleanup_streams_first(bench, how):
    """common §2: detach, the session's end, a lost line, or a cleanup the target op defines that fails close the
    connection; a stream on it goes first."""
    ep, clock, hst, conn, con, sid = bench
    if how == "detach":
        riscv.Wire(hst).detach(conn)
    elif how == "end":
        hst.end()
    elif how == "lost":
        ep.lose(conn)
    else:
        ep._target_of(conn).restore_fails = True
        with pytest.raises(riscv.TargetError):
            riscv.RiscvDm(hst, conn).write_block(0x20000000, struct.pack("<2I", 7, 8))
    assert conn not in ep.conns and sid not in ep.streams


# ---- §3 status --------------------------------------------------------------------------------------------------

def test_the_status_values_and_an_unknown_one_is_a_failure():
    """common §3: ok 0, wait 1, line 2, fault 3, timeout 4, state 5; 0x06-0x3F reserved, 0x40-0x7F the interface's -
    a value the host does not know is a failure; only ok is success."""
    assert reg.STATUS == {"ok": 0, "wait": 1, "line": 2, "fault": 3, "timeout": 4, "state": 5}
    for name, value in reg.STATUS.items():
        r = m.Result(1, m.COMPLETED, m.SUCCESS if name == "ok" else m.FAILED, bytes([value]))
        if name == "ok":
            riscv.check("halt", r, value)
        else:
            with pytest.raises(riscv.TargetError, match=name):
                riscv.check("halt", r, value)
    for odd in (0x06, 0x3F, 0x40, 0x7F):
        with pytest.raises(riscv.TargetError, match=f"unknown status 0x{odd:02x}"):
            riscv.check("halt", m.Result(1, m.COMPLETED, m.SUCCESS, bytes([odd])), odd)


def test_the_bench_answers_line_and_state_and_timeout_where_they_happen(bench):
    """common §3: line - the line does not answer; state - not in the state the op needs (a running hart); timeout -
    a wait or run reached its limit. Each a completed failed or partial with the success shape."""
    ep, clock, hst, conn, con, sid = bench
    dm = riscv.RiscvDm(hst, conn)
    dm.resume()
    with pytest.raises(riscv.TargetError) as e:
        dm.step()
    assert e.value.status == reg.STATUS["state"] and e.value.result.resolution == m.COMPLETED
    dm.halt()
    ep.target.dmi[0x16] = 0x1000                                  # abstractcs busy for good
    with pytest.raises(riscv.StepListError) as e:
        dm.dmi([dm.step_read(0x11), dm.step_poll(0x16, 0x1000, 0, 3)])
    assert e.value.status == reg.STATUS["timeout"] and e.value.result.detail == m.PARTIAL and e.value.done == 1
    ep.target.fail_write.add(0x10)
    with pytest.raises(riscv.StepListError) as e:
        dm.dmi([dm.step_write(0x10, 1)])
    assert e.value.status == reg.STATUS["line"] and e.value.result.detail == m.FAILED and e.value.done == 0


def test_a_failed_attach_answers_status_alone_and_a_bad_form_is_malformed():
    """common §3: an op whose success shape has no done / status (scan, attach, detach) fails completed failed with
    payload status(u8) [TLV]; a request of the wrong form is rejected malformed."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(ep.handle, rng=random.Random(6))
    hst.open()
    pair = ep.pairs[1][0]
    ep._target(1, pair).present = False                           # nobody on the line
    W = reg.WIRE_RVSWD.op
    r = hst.request(1, W["attach"], struct.pack("<BHHI", 0, *pair, 1_000_000))
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, bytes([reg.STATUS["line"]]))
    with pytest.raises(h.Rejected, match="malformed"):
        hst.call(1, W["attach"], struct.pack("<BHH", 0, *pair))


def test_the_client_never_runs_a_failed_target_write_again_by_itself(bench):
    """common §3: failed or done 0 is no proof the write did not reach the target - the client raises it and sends no
    new corr to run it again."""
    ep, clock, hst, conn, con, sid = bench
    dm = riscv.RiscvDm(hst, conn)
    ep.target.fail_write.add(0x10)
    before = len(ep.requests)
    with pytest.raises(riscv.StepListError):
        dm.dmi([dm.step_write(0x10, 1)])
    assert len(ep.requests) == before + 1


# ---- review additions (independent review of the trace) ------------------------------------------------------------

def test_mark_serials_are_counted_per_stream(bench):
    """common §1.3: serial is a count of marks per stream - marks on the console stream do not move the fixture UART's
    serials, nor a second console stream's; each goes 0, 1, ... on its own."""
    ep, clock, hst, conn, con, sid = bench
    for v in range(3):
        con.mark(v)
    assert [mk.serial for mk in con.marks()] == [0, 1, 2]
    core.plan_apply(hst, [(5, 1, 21)])
    uart = fixture.FixtureUart(hst, 5)
    uart.configure(115200)
    uart.mark(7)
    assert [(mk.serial, mk.detail) for mk in uart.marks()] == [(0, 7)]
    other = console.Console(hst)
    c2, _ = riscv.Wire(hst).attach(halt=False, pins=ep.pairs[1][1])
    other.open(c2)
    other.mark(9)
    assert [(mk.serial, mk.detail) for mk in other.marks()] == [(0, 9)]
    con.mark(3)
    assert [mk.serial for mk in con.marks()] == [0, 1, 2, 3]
