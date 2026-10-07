"""The console's per-stream send queue (console §2, §3: its size is the probe's, not declared), the wait for a target
that restarts by itself after a reset (debug §3, §4.3: within max_op_ms, the argument time of core §4.4), an item of
another length (probe-config §1, core §2.3) and oep.link source's len (oep-if-link §2) - oep-spec 7688c49..0f455a0."""

import random
import struct

import pytest

from oep_client import config, console, core, endpoint, virtual_bench, host as h, message as m, registry as reg, riscv

MAX_OP_MS = virtual_bench.MAX_OP_MS


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench(profile=virtual_bench.p4_x035):
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    hst = h.Host(lambda b: ep.handle(b, 1), rng=random.Random(9))
    hst.open(10000)
    return clock, ep, hst


# ---- R-A: the console's send queue ----------------------------------------------------------------------------------

def test_the_send_queue_is_not_declared():
    """console §1 / §2: describe carries mechanisms alone (0x41 reserved); the queue's size is the probe's."""
    assert "send_queue" not in reg.TARGET_CONSOLE.tlv["describe"]
    assert [t[0] for t in virtual_bench._console(3).tlvs] == [reg.TARGET_CONSOLE.tlv["describe"]["mechanisms"]]
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    con = console.Console(hst)
    con.open(conn, con.DMSEQ)
    assert con.write(b"x" * 300) == virtual_bench.CONSOLE_SEND_QUEUE       # what the probe's own queue takes (partial)


def test_the_queue_is_handed_on_a_poll_at_a_time_and_survives_resets_and_restarts():
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    con = console.Console(hst)
    sid = con.open(conn, con.DMDATA)
    assert con.write(b"abcdefghij") == 10
    clock.ms = 1
    con.read()                                                     # any request: the probe polled once
    assert bytes(ep.streams[sid].written) == b"abc" and bytes(ep.streams[sid].queue) == b"defghij"
    riscv.RiscvDm(hst, conn).reset(confirm=False)                  # a reset: the queue stays (console §2)
    assert bytes(ep.streams[sid].queue) == b"defghij"
    clock.ms = 3
    con.read()
    assert bytes(ep.streams[sid].written) == b"abcdefghi"         # 2 polls x 3 bytes
    con.close()
    assert ep.streams[sid].closed and ep.streams[sid].queue == b""  # the queue goes with the stream


def test_dmseq_hands_on_2_bytes_a_poll_and_sdi_accepts_nothing():
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    con = console.Console(hst)
    sid = con.open(conn, con.DMSEQ)
    assert con.write(b"hello") == 5
    clock.ms = 2
    con.read()
    assert bytes(ep.streams[sid].written) == b"hell"
    con.close()
    riscv.Wire(hst).detach(conn, force=True)
    conn, _ = riscv.Wire(hst).attach(halt=False)
    sdi = console.Console(hst)
    sdi.open(conn, sdi.SDI)
    with pytest.raises(h.Failed):
        sdi.write(b"x")                                            # accepted 0 (console §3.1)


# ---- the wait for a silent DM after a reset: within max_op_ms ----------------------------------------------------------

@pytest.mark.parametrize("restart_ms, ok", [(0, True), (MAX_OP_MS, True), (MAX_OP_MS + 1, False)])
def test_reset_waits_out_a_self_restarting_target_within_max_op_ms(restart_ms, ok):
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    ep.target.restart_ms = restart_ms
    dm = riscv.RiscvDm(hst, conn)
    n = len(ep.requests)
    if ok:
        flags, _ = dm.reset(confirm=False)
        assert flags & 1
    else:
        with pytest.raises(riscv.TargetError) as e:
            dm.reset(confirm=False)
        assert e.value.status == reg.STATUS["line"]
        r = m.Result.unpack(ep.handle(m.Request(999, dm.fn, dm.RESET, struct.pack("<HB", conn, 0), hst.session).pack(), 1))
        assert struct.unpack("<BBI", r.payload) == (reg.STATUS["line"], 0, 0)   # status flags pc (debug §4.3)
        assert conn in ep.conns                                    # the connection kept
    assert ep.settle_log[0] == min(restart_ms, MAX_OP_MS)
    assert ep.requests[n].op == dm.RESET
    assert dm.reset_ms() == core.max_op_ms(hst) == MAX_OP_MS       # the argument time is max_op_ms (core §4.4)


def test_attach_with_the_reset_tlv_waits_too_and_keeps_an_existing_connection():
    clock, ep, hst = bench(virtual_bench.esp32_v003)                        # swio, NRST on 23
    wire = riscv.Wire(hst, "oep.wire.swio")
    ep.target.restart_ms = 300
    conn, dpc = wire.attach_under_reset(23, hold_ms=20)
    assert ep.settle_log == [300] and ep.target.halted
    ep.target.restart_ms = MAX_OP_MS + 100
    with pytest.raises(h.Failed):
        wire.attach(halt=False, reset=(23, 20))                    # status line within max_op_ms (debug §3)
    assert conn in ep.conns and ep.settle_log[-1] == MAX_OP_MS
    assert wire.attach_ms((23, 20)) == wire.attach_ms() == wire.scan_ms() == core.max_op_ms(hst)   # core §4.4


# ---- an item of another length; source's len -------------------------------------------------------------------

@pytest.mark.parametrize("critical", [False, True])
def test_an_item_of_another_length_is_malformed_with_or_without_bit_7(critical):
    """probe-config §1, core §2.3: an implemented item whose value is longer or shorter than its form is malformed
    whatever bit 7 says - nothing applied."""
    clock, ep, hst = bench(virtual_bench.p4_bench)
    cfg = config.ProbeConfig(hst)
    for value in (struct.pack("<HIB", 5, 115200, 0) + b"\x00", struct.pack("<HI", 5, 115200)):
        with pytest.raises(h.Rejected) as e:
            hst.call(cfg.fn, cfg.SET, m.tlv(config.ITEM["uart"], value, critical=critical)
                     + config.item(config.Label(channel=20, text="ok")))
        assert e.value.result.detail == m.MALFORMED
    assert cfg.items() == []
    hst.call(cfg.fn, cfg.SET, config.item(config.Label(channel=21, text="a long label text")))   # text: to the end
    assert [type(i).__name__ for i in cfg.items()] == ["Label"]


def test_link_source_is_max_frame_less_7_with_or_without_a_tlv():
    clock, ep, hst = bench()
    fn = core.link_fn(hst)
    data = core.link_source_data(hst.call(fn, core.LINK_SOURCE, core.link_source_request(5000), locked=False).payload)
    assert len(data) == 1024 - 7 == core.link_size(1024)          # the answer's header and len (oep-if-link §2)
    r = hst.call(fn, core.LINK_SOURCE, core.link_source_request(5000) + m.tlv(0x3D, b""), locked=False)
    assert len(core.link_source_data(r.payload)) == len(data)      # an unknown TLV ignored: the same len
