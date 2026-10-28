"""oep.target.console (oep-spec a09fa45 interfaces/oep-if-console.ja.md) on the virtual bench: the rules
tests/test_interfaces.py, tests/test_console_queue_and_reset_settle.py and the console checkers leave to the trace -
the mechanisms declaration and the refusals of open, the resource numbers, streams' order, what releases a stream,
a fn without marks, the reset mark; and the dmseq checker's first synchronisation (no restart mark)."""

import struct

import pytest

from oep_client import catalog, console, core, endpoint, riscv, virtual_bench, host as h, message as m, registry as reg
from oep_client.conformance_dmseq import DmseqChecks

from test_console_queue_and_reset_settle import Clock, bench
from test_conformance_dmseq import setup as dmseq_setup

CON = reg.TARGET_CONSOLE
READ_FROM = reg.COMMON.enum["read_from"]
MECHANISM = CON.tlv["describe"]["mechanism"]
STANDARD = list(CON.names["mechanism"])                      # sdi, dmdata, dmseq


def call(ep, hst, op, payload, locked=True):
    """A console request as sent; its result whatever it is."""
    return m.Result.unpack(ep.handle(m.Request(hst.next_corr(), ep.fns[CON.name], op, payload,
                                               hst.session if locked else 0).pack(), 1))


def test_describe_declares_the_mechanisms_as_a_named_list():
    """console §1, core §7.7: describe tag 0x40 mechanism, one name per mechanism, at least one, no name twice."""
    clock, ep, hst = bench()
    names = [v.decode() for t, v in core.describe(hst, ep.fns[CON.name]) if t & 0x7F == MECHANISM]
    assert names and len(set(names)) == len(names) and set(names) <= set(STANDARD)


def test_a_mechanism_not_declared_or_unknown_is_unsupported_0x00():
    """console §1, core §7.7: the mechanism is its place in describe's list (from 1); 0 and a number past the list are
    rejected unsupported with tag 0x00 - nothing is opened."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    for mech in (0, len(ep.mech_names) + 1, 0xFF):
        r = call(ep, hst, CON.op["open"], struct.pack("<IB", conn, mech))
        assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, b"\x00") and not ep.streams
    assert call(ep, hst, CON.op["open"], struct.pack("<IB", conn, ep.mech_names.index("dmseq") + 1)).succeeded


def test_a_probes_own_mechanism_takes_its_place_in_the_named_list(monkeypatch):
    """core §7.7, §12: another party adds its own choice to a named list under a reverse-DNS name; on the wire a choice
    is its place (from 1), which the host finds by name - a name the probe does not list is 0."""
    listed = [(MECHANISM, b"sdi"), (MECHANISM, b"io.example.mbox"), (MECHANISM, b"dmseq")]
    monkeypatch.setattr(console, "describe", lambda host, fn: listed)
    con = console.Console.__new__(console.Console)
    con.host, con.fn = None, 3
    assert con.mechanisms() == ["sdi", "io.example.mbox", "dmseq"]
    assert [con.mechanism_number(n) for n in ("io.example.mbox", "dmseq", "dmdata")] == [2, 3, 0]


def test_unknown_numbers_no_resource_and_another_kinds_number_cause_6():
    """console §1: an unknown stream is no_resource; a connection's number given as a stream, or a stream's as the
    connection of an open, is unavailable cause 6."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    sid = console.Console(hst).open(conn, console.Console.DMSEQ)
    for op, payload in ((CON.op["read"], struct.pack("<IBQH", conn, 1, 0, 16)), (CON.op["close"], struct.pack("<I", conn)),
                        (CON.op["open"], struct.pack("<IB", sid, ep.mechanism_number("dmseq")))):
        r = call(ep, hst, op, payload, locked=op != CON.op["read"])
        assert r.detail == m.UNAVAILABLE and dict(m.split_tlvs(r.payload))[1] == b"\x06"
    for op, payload in ((CON.op["read"], struct.pack("<IBQH", 0x7777, 1, 0, 16)), (CON.op["close"], struct.pack("<I", 0x7777)),
                        (CON.op["open"], struct.pack("<IB", 0x7777, 2))):
        assert call(ep, hst, op, payload, locked=op != CON.op["read"]).detail == m.NO_RESOURCE
    assert list(ep.streams) == [sid]


def test_stream_numbers_are_never_used_again_in_a_boot():
    """console §1, §2 (core §9): a new open gets a new u32 number - never a closed stream's, never a connection's -
    and the closed one's buffer and marks do not come back."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    seen = {conn}
    for _ in range(4):
        con = console.Console(hst)
        sid = con.open(conn, console.Console.DMSEQ)
        assert sid not in seen
        seen.add(sid)
        ep.emit(sid, b"old")
        con.mark(1)
        con.close()
        r = call(ep, hst, CON.op["read"], struct.pack("<IBQH", sid, 1, 0, 16), locked=False)
        assert r.detail == m.NO_RESOURCE
    con = console.Console(hst)
    sid = con.open(conn, console.Console.DMSEQ)
    chunk = con.read(console.Console.FROM_OLDEST, 0, 64)
    assert chunk.start == 0 and chunk.data == b"" and con.marks() == []


def test_streams_lists_the_live_streams_in_the_order_made():
    """console §1: streams returns the live streams only, in the order they were opened; a closed one is gone and a
    request naming it is no_resource."""
    clock, ep, hst = bench(virtual_bench.p4_bench)
    wire = riscv.Wire(hst)
    one, _ = wire.attach(halt=False, pins=(4, 5))
    two, _ = wire.attach(halt=False, pins=(2, 3))
    a, b = console.Console(hst), console.Console(hst)
    sa = a.open(two, console.Console.DMDATA)
    sb = b.open(one, console.Console.SDI)
    assert a.streams() == [console.StreamInfo(sa, two, "dmdata"), console.StreamInfo(sb, one, "sdi")]
    a.close()
    assert a.streams() == [console.StreamInfo(sb, one, "sdi")]
    with pytest.raises(h.NoResource):
        a.read()


@pytest.mark.parametrize("how", ["close", "detach", "end"])
def test_close_the_connections_release_and_the_sessions_end_free_the_stream_and_its_queue(how):
    """console §2: close, the connection's release and the session's end free the stream with its buffer and send
    queue."""
    clock, ep, hst = bench()
    wire = riscv.Wire(hst)
    conn, _ = wire.attach(halt=True)                                   # halted: the queue is not handed on
    con = console.Console(hst)
    sid = con.open(conn, console.Console.DMDATA)
    ep.emit(sid, b"output")
    assert con.write(b"queued") == 6 and len(ep.streams[sid].queue) == 6
    {"close": con.close, "detach": lambda: wire.detach(conn), "end": hst.end}[how]()
    assert sid not in ep.streams and sid not in ep.resources
    if how != "end":
        with pytest.raises(h.NoResource):
            con.write(b"x")


def test_a_fn_without_marks_takes_no_from_3_and_has_no_mark_ops():
    """console §1, §2: marks is optional - a fn without it answers no marks / mark (unknown_operation) and read's
    from 3 is unsupported; open / read / close / streams are its minimum."""
    probe = virtual_bench.p4_x035()
    probe = virtual_bench._again(probe, [
        virtual_bench.Offered(o.fn, o.instance, o.name, tuple(catalog.tlv(virtual_bench.MECHANISM, n) for n in (b"sdi", b"dmdata", b"dmseq"))
                              + virtual_bench.ops_of(CON.name, "marks", "mark", "clear", "write"), inner=o.inner)
        if o.name == CON.name else o for o in probe.offered])
    clock = Clock()
    ep = endpoint.Endpoint(probe, clock)
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(10000)
    conn, _ = riscv.Wire(hst).attach(halt=False)
    con = console.Console(hst)
    sid = con.open(conn, console.Console.DMSEQ)
    assert call(ep, hst, CON.op["marks"], struct.pack("<II", sid, 0), locked=False).detail == m.UNKNOWN_OPERATION
    assert call(ep, hst, CON.op["mark"], struct.pack("<IB", sid, 1)).detail == m.UNKNOWN_OPERATION
    r = call(ep, hst, CON.op["read"], struct.pack("<IBQH", sid, READ_FROM["last_mark"], 0, 16), locked=False)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    assert call(ep, hst, CON.op["read"], struct.pack("<IBQH", sid, READ_FROM["oldest"], 0, 16), locked=False).succeeded
    assert con.streams() == [console.StreamInfo(sid, conn, "dmseq")]


def test_a_reset_op_marks_reset_detail_1_once_and_no_restart_for_it():
    """console §2 (the mark table): the riscv-dm reset op's ndmreset on the stream's connection records one reset mark
    (kind 0x40, detail 1 ndmreset) and no restart mark for that same reset; the output and the send queue stay."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    con = console.Console(hst)
    sid = con.open(conn, console.Console.DMSEQ)
    ep.emit(sid, b"before")
    dm = riscv.RiscvDm(hst, conn)
    dm.reset()
    marks = con.marks()
    assert [(mk.kind, mk.detail) for mk in marks] == [(console.MARK_KIND["reset"], 1)]
    dm.halt()                                                         # the ops after it: still no restart for it
    assert [(mk.kind, mk.detail) for mk in con.marks()] == [(console.MARK_KIND["reset"], 1)]
    assert con.read(console.Console.FROM_OLDEST, 0, 64).data == b"before"


def first_sync_with_restart_mark(model):
    """A dmseq model that (wrongly) marks restart on the first synchronisation from unsynced."""
    install = model.install

    def faulty_install():
        install()
        e = model.extension
        poll = e.poll

        def broken(rid):
            synced = e.protocols[rid]["synced"]
            poll(rid)
            if not synced and e.protocols[rid]["synced"]:
                e.streams[rid][1].mark(0x41, 2)
        e.poll = broken
    model.install = faulty_install


def test_dmseq_first_synchronisation_marks_no_restart():
    """console §2 (the dmseq marks): the first synchronisation from unsynced records no restart mark - the checker's
    SYN / UNSYNCED / VECTOR cases pass on the model, and fail on a model that marks one."""
    model, c, a = dmseq_setup()
    r = DmseqChecks(c, a).run()
    assert all(x["status"] == "passed" for x in r["checks"] if x["id"] in ("OEP-DMSEQ-VECTOR", "OEP-DMSEQ-SYN", "OEP-DMSEQ-UNSYNCED"))
    model, c, a = dmseq_setup()
    first_sync_with_restart_mark(model)
    r = DmseqChecks(c, a).run()
    rows = {x["id"]: x for x in r["checks"]}
    assert all(rows[k]["status"] == "failed" for k in ("OEP-DMSEQ-VECTOR", "OEP-DMSEQ-SYN", "OEP-DMSEQ-UNSYNCED")), r["checks"]


def test_the_interface_row_lists_oep_target_console_at_revision_1():
    """console preamble (the interface row): the probe lists oep.target.console by that name at revision 1."""
    clock, ep, hst = bench()
    entries = [e for e in core.list_entries(hst) if e.name == CON.name]
    assert [(e.fn, e.revision) for e in entries] == [(ep.fns[CON.name], 1)] and CON.revision == 1


@pytest.mark.parametrize("op, locked", [("open", True), ("read", False), ("marks", False), ("clear", True),
                                        ("mark", True), ("write", True), ("close", True), ("streams", False)])
def test_the_lock_column_of_the_op_table(op, locked):
    """console §1 (the op table, lock column): read, marks and streams need no lock (answered with session_id 0);
    open, clear, mark, write and close need it - session_required with 0, and the stream, its buffer, marks and send
    queue stay as they were."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=True)
    con = console.Console(hst)
    sid = con.open(conn, console.Console.DMDATA)
    ep.emit(sid, b"out")
    payload = {"open": struct.pack("<IB", conn, ep.mechanism_number("dmseq")), "read": struct.pack("<IBQH", sid, READ_FROM["oldest"], 0, 16),
               "marks": struct.pack("<II", sid, 0), "clear": struct.pack("<I", sid), "mark": struct.pack("<IB", sid, 1),
               "write": struct.pack("<IH", sid, 1) + b"x", "close": struct.pack("<I", sid),
               "streams": struct.pack("<H", 0)}[op]
    assert (CON.op[op] not in CON.lock_free) == locked
    snap = lambda: (list(ep.streams), con.read(console.Console.FROM_OLDEST, 0, 64).data, con.marks(),
                    bytes(ep.streams[sid].queue))
    before = snap()
    r = call(ep, hst, CON.op[op], payload, locked=False)
    if locked:
        assert r.detail == m.SESSION_REQUIRED and snap() == before
    else:
        assert r.succeeded


def test_a_write_of_count_0_is_success_with_accepted_0():
    """console §2 (the send queue): a write of count 0 is completed success with accepted 0 - even with the queue
    full, where a count above 0 is completed failed with accepted 0."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=True)                       # halted: nothing is handed on
    con = console.Console(hst)
    sid = con.open(conn, console.Console.DMDATA)
    for full in (False, True):
        if full:
            assert con.write(b"x" * virtual_bench.CONSOLE_SEND_QUEUE) == virtual_bench.CONSOLE_SEND_QUEUE
        r = call(ep, hst, CON.op["write"], struct.pack("<IH", sid, 0))
        assert (r.resolution, r.detail) == (m.COMPLETED, m.SUCCESS) and m.Reader(r.payload).u16() == 0
    r = call(ep, hst, CON.op["write"], struct.pack("<IH", sid, 1) + b"y")
    assert (r.resolution, r.detail) == (m.COMPLETED, m.FAILED) and m.Reader(r.payload).u16() == 0


def test_a_fn_without_marks_still_synchronises_and_drops_duplicates_on_dmseq():
    """console §2: a fn that does not offer marks still keeps dmseq's synchronisation and drops duplicate frames -
    the dmseq checker on a model whose console answers marks / mark unknown_operation passes its synchronisation,
    duplicate and transition cases (only the cases that read marks back fail)."""
    from oep_client.conformance import Checks
    model, c, a = dmseq_setup()
    handle = model.handle

    def without_marks(b, *rest):
        q = m.Request.unpack(b)
        if q.fn == 4 and q.op in (CON.op["marks"], CON.op["mark"]):
            return m.Result(q.corr, m.REJECTED, m.UNKNOWN_OPERATION, b"").pack()
        return handle(b, *rest)
    r = DmseqChecks(Checks(without_marks, c.reg, "virtual-resource-1"), a).run()
    rows = {x["id"]: x["status"] for x in r["checks"]}
    kept = ["SYN", "UNSYNCED", "LENGTHS", "CRC", "DUP-PLAIN", "DUP-SYN", "DUP-TO", "SAME-S-SYN", "NEW-S-SYN",
            "IDENTICAL-NEW", "DUP-ACK", "ACK-EMPTY"]
    assert all(rows["OEP-DMSEQ-" + k] == "passed" for k in kept), rows
    assert rows["OEP-DMSEQ-MARK-RESYNC-NEW"] == "failed"                    # the marks really are gone
