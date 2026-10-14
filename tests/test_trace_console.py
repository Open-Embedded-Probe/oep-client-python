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
MECHANISMS = CON.tlv["describe"]["mechanisms"]


def call(ep, hst, op, payload, locked=True):
    """A console request as sent; its result whatever it is."""
    return m.Result.unpack(ep.handle(m.Request(hst.next_corr(), ep.fns[CON.name], op, payload,
                                               hst.session if locked else 0).pack(), 1))


def test_describe_declares_the_mechanisms_counted_without_duplicates():
    """console §1: describe tag 0x40 mechanisms - n(u8) then n mechanisms (u8), n >= 1, no duplicate - always there."""
    clock, ep, hst = bench()
    values = [v for t, v in core.describe(hst, ep.fns[CON.name]) if t & 0x7F == MECHANISMS]
    assert len(values) == 1
    v = values[0]
    assert v[0] >= 1 and len(v) == 1 + v[0] and len(set(v[1:])) == v[0] and set(v[1:]) <= set(CON.enum["mechanism"].values())


def test_a_mechanism_not_declared_or_unknown_is_unsupported_0x00():
    """console §1: the mechanism number fixes the mechanism; an unknown one and one describe's mechanisms leaves out
    are rejected unsupported with tag 0x00 - nothing is opened."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    ep.mechanisms = {CON.enum["mechanism"]["dmseq"]}                  # a probe that opens dmseq alone
    for mech in (CON.enum["mechanism"]["sdi"], CON.enum["mechanism"]["dmdata"], 3, 0xFF):
        r = call(ep, hst, CON.op["open"], struct.pack("<IB", conn, mech))
        assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, b"\x00") and not ep.streams
    assert call(ep, hst, CON.op["open"], struct.pack("<IB", conn, 2)).succeeded


def test_unknown_numbers_no_resource_and_another_kinds_number_cause_6():
    """console §1: an unknown stream is no_resource; a connection's number given as a stream, or a stream's as the
    connection of an open, is unavailable cause 6."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    sid = console.Console(hst).open(conn, console.Console.DMSEQ)
    for op, payload in ((CON.op["read"], struct.pack("<IBQH", conn, 1, 0, 16)), (CON.op["close"], struct.pack("<I", conn)),
                        (CON.op["open"], struct.pack("<IB", sid, 2))):
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
    assert a.streams() == [console.StreamInfo(sa, two, 1), console.StreamInfo(sb, one, 0)]
    a.close()
    assert a.streams() == [console.StreamInfo(sb, one, 0)]
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
        virtual_bench.Offered(o.fn, o.instance, o.name, (catalog.tlv(virtual_bench.MECHANISMS, b"\x03\x00\x01\x02"),)
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
    r = call(ep, hst, CON.op["read"], struct.pack("<IBQH", sid, 3, 0, 16), locked=False)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    assert call(ep, hst, CON.op["read"], struct.pack("<IBQH", sid, 1, 0, 16), locked=False).succeeded
    assert con.streams() == [console.StreamInfo(sid, conn, 2)]


def test_a_reset_op_marks_reset_detail_1_once_and_no_restart_for_it():
    """console §2 (the mark table): the riscv-dm reset op's ndmreset on the stream's connection records one reset mark
    (kind 0x40, detail 1 ndmreset) and no restart mark for that same reset; the output and the send queue stay."""
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    con = console.Console(hst)
    sid = con.open(conn, console.Console.DMSEQ)
    ep.emit(sid, b"before")
    riscv.RiscvDm(hst, conn).reset()
    marks = con.marks()
    assert [(mk.kind, mk.detail) for mk in marks] == [(console.MARK_KIND["reset"], 1)]
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
