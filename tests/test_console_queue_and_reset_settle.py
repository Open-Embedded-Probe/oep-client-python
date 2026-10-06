"""oep-spec f0c68bf (R-A, R-B), d34dafa and 4bd3a87 on the fake and the client: the console's per-stream send queue
(console §1 tag 0x41, §2, §3), the reset settle wait (debug §3, §4.3, core §4.4), a longer probe.config item
(probe-config §1, core §2.3) and oep.link source's len (oep-if-link §2)."""

import random
import struct

import pytest

from oep_client import config, console, core, endpoint, fake, host as h, message as m, registry as reg, riscv

SETTLE = reg.LIMITS["reset_settle_ms"]


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench(profile=fake.p4_x035):
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    hst = h.Host(lambda b: ep.handle(b, 1), rng=random.Random(9))
    hst.open(10000)
    return clock, ep, hst


# ---- R-A: the console's send queue ----------------------------------------------------------------------------------

def test_send_queue_is_declared_only_with_a_mechanism_that_carries_input():
    assert reg.LIMITS["console_send_queue_min_bytes"] == 64 and fake.CONSOLE_SEND_QUEUE >= 64
    tag = reg.TARGET_CONSOLE.tlv["describe"]["send_queue"]
    assert any(t[0] == tag for t in fake._console(3).tlvs)
    assert not any(t[0] == tag for t in fake._console(3, mechanisms=(0,)).tlvs)   # SDI only: none (console §1)


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


# ---- R-B: the reset settle wait -------------------------------------------------------------------------------------

@pytest.mark.parametrize("restart_ms, ok", [(0, True), (SETTLE, True), (SETTLE + 1, False)])
def test_reset_waits_out_a_self_restarting_target_up_to_reset_settle_ms(restart_ms, ok):
    clock, ep, hst = bench()
    conn, _ = riscv.Wire(hst).attach(halt=False)
    ep.target.restart_ms = restart_ms
    dm = riscv.RiscvDm(hst, conn)
    n = len(ep.requests)
    if ok:
        flags, attempts, _ = dm.reset(confirm=False)
        assert flags & 1 and attempts == 1
    else:
        with pytest.raises(riscv.TargetError) as e:
            dm.reset(confirm=False)
        assert e.value.status == reg.STATUS["line"]
        r = m.Result.unpack(ep.handle(m.Request(999, dm.fn, dm.RESET, struct.pack("<HB", conn, 0), hst.session).pack(), 1))
        assert struct.unpack("<BBBI", r.payload[:7])[:2] == (reg.STATUS["line"], 0)   # no bit2: not tried again (§4.3)
        assert conn in ep.conns                                    # the connection kept
    assert ep.settle_log[0] == min(restart_ms, SETTLE)
    assert ep.requests[n].op == dm.RESET


def test_attach_with_the_reset_tlv_waits_too_and_keeps_an_existing_connection():
    clock, ep, hst = bench(fake.esp32_v003)                        # swio, NRST on 23
    wire = riscv.Wire(hst, "oep.wire.swio")
    ep.target.restart_ms = 300
    conn, dpc = wire.attach_under_reset(23, hold_ms=20)
    assert ep.settle_log == [300] and ep.target.halted
    ep.target.restart_ms = SETTLE + 100
    with pytest.raises(h.Failed):
        wire.attach(halt=False, reset=(23, 20))                    # status line after reset_settle_ms (debug §3)
    assert conn in ep.conns and ep.settle_log[-1] == SETTLE
    assert wire.attach_ms((23, 20)) == min(1000 + 20 + SETTLE, core.max_op_ms(hst))   # the host's floor (core §4.4)


# ---- d34dafa / 4bd3a87 -------------------------------------------------------------------------------------------

def test_a_longer_item_is_ignored_or_unsupported_when_critical_and_the_others_apply():
    clock, ep, hst = bench(fake.p4_bench)
    cfg = config.ProbeConfig(hst)
    longer = m.tlv(config.ITEM["uart"], struct.pack("<HIB", 5, 115200, 0) + b"\x00")
    r = hst.call(cfg.fn, cfg.SET, longer + config.item(config.Label(channel=20, text="ok")))
    assert m.Reader(r.payload[4:]).tail().ignored == [config.ITEM["uart"]]
    assert [type(i).__name__ for i in cfg.items()] == ["Label"]                # the other item applied
    with pytest.raises(h.Unsupported) as e:
        hst.call(cfg.fn, cfg.SET, m.tlv(config.ITEM["uart"], struct.pack("<HIB", 5, 115200, 0) + b"\x00",
                                        critical=True))
    assert e.value.tag == config.ITEM["uart"] | 0x80
    r = hst.call(cfg.fn, cfg.SET, config.item(config.Label(channel=21, text="a long label text")))   # text: to the end
    assert not m.Reader(r.payload[4:]).tail().ignored


def test_link_source_keeps_the_ignored_room_always():
    clock, ep, hst = bench()
    fn = core.link_fn(hst)
    data = core.link_source_data(hst.call(fn, core.LINK_SOURCE, core.link_source_request(5000), locked=False).payload)
    assert len(data) == 1024 - reg.LIMITS["link_source_overhead_bytes"] == core.link_size(1024)
    r = hst.call(fn, core.LINK_SOURCE, core.link_source_request(5000) + m.tlv(0x3D, b""), locked=False)
    assert len(core.link_source_data(r.payload)) == len(data)      # the same len with a TLV ignored
