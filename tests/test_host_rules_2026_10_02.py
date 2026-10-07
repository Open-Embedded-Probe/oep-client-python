"""The host side of oep-spec docs/v1-rule-change-proposal-2026-10-02.md (applied 09622ef..536fc99): the wait's floor
(C-06), the serial line (C-09), the revision in use (C-15), confirm's transport (C-05), x- unit_ids (C-24), TCP and the
resync wait (C-07, transports §5), the line search (PC-1), text shown and sent (C-22), no ignored marker (C-04, gone in
the rule review 2026-10-07), what a probe must give (C-10), and the new answer TLVs in the API. Updated to oep-spec
0f455a0."""

import os
import struct
import sys
import time

import pytest

from oep_client import (capture, config, core, dump, endpoint, virtual_bench, fixture, frames, host as h, link, message as m,
                        registry as reg, riscv, speed_record)

from test_virtual_bench_rules_2026_10_02 import Clock
from test_link_host import CONFIRM_V1, Stream, frame, make_link, result


def in_process(profile=virtual_bench.p4_bench, transport=0, **kw):
    ep = endpoint.Endpoint(profile(), Clock(), **kw)
    hst = h.Host(lambda b: ep.handle(b, transport))
    return ep, hst


# ---- C-06: the wait's floor -------------------------------------------------------------------------------------

class Serial:
    """A COBS port stand-in with a line speed; nothing ever answers."""
    baudrate = 115200

    def __init__(self):
        self.timeout, self.writes = 0.05, []

    in_waiting = 0

    def read(self, n=1):
        time.sleep(min(self.timeout or 0, 0.01))
        return b""

    def write(self, data):
        self.writes.append(bytes(data))

    def reset_input_buffer(self):
        pass


def test_c06_the_floor_is_argument_time_plus_1000_ms_plus_the_transfer_time():
    lk = link.SerialLink.on_stream(Serial(), "cobs", 0.2)
    lk.max_frame = 1024
    lk._write([m.Request(1, 0, m.OP_KEEPALIVE).pack()])
    transfer = (lk.tx_len + 1024 * 3) * 10 / 115200                    # (L + max_frame x (1 + 2)) x 10 / baud
    assert lk.transfer_s() == pytest.approx(transfer) and transfer > 0.26
    assert lk._wait() == pytest.approx(1.0 + transfer)                 # over the link's own 0.2 s
    lk.expected_s = lambda: 2.5                                        # run's timeout_ms, dmi's waits ...
    assert lk._wait() == pytest.approx(2.5 + 1.0 + transfer)
    lk._own += 1                                                       # the link's own short requests keep theirs
    assert lk._wait() == 0.2


def test_c06_no_transfer_time_on_length_frames():
    lk = make_link(Stream())
    assert lk.transfer_s() == 0 and lk._wait() == pytest.approx(1.0)


def test_c06_attach_scan_and_reset_wait_max_op_ms():
    """debug §1 / §4.3 (rule review 2026-10-07): attach, scan and riscv-dm's reset answer within max_op_ms, and the host
    counts max_op_ms as their argument time (core §4.4) - no hold_ms or reset_settle_ms on top any more."""
    ep, hst = in_process()
    seen = []
    send = hst.send
    hst.send = lambda b: (seen.append((b[5], hst.expect_ms)), send(b))[1]
    hst.open(3000)
    w = riscv.Wire(hst)
    w.scan()
    w.attach(pins=ep.pairs[1][0], reset=None)
    budgets = dict((op, ms) for op, ms in seen if op in (riscv.Wire.SCAN, riscv.Wire.ATTACH))
    assert budgets == {riscv.Wire.SCAN: virtual_bench.MAX_OP_MS, riscv.Wire.ATTACH: virtual_bench.MAX_OP_MS}
    assert w.attach_ms((7, 20)) == w.attach_ms() == w.scan_ms() == virtual_bench.MAX_OP_MS and w.search_retries == 0
    dm = riscv.RiscvDm(hst, 1)
    assert dm.reset_ms() == virtual_bench.MAX_OP_MS
    assert core.FALLBACK_MAX_OP_MS == 10000 and "reset_settle_ms" not in reg.TIMING
    seen.clear()
    dm.reset(confirm=False)
    assert seen == [(riscv.RiscvDm.RESET, virtual_bench.MAX_OP_MS)]


# ---- C-09: the serial line ----------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux", reason="a pty")
def test_c09_a_serial_port_opens_8n1_without_flow_control_and_dtr_rts_asserted():
    master, slave = os.openpty()
    try:
        stream = link.open_serial(os.ttyname(slave))
        try:
            assert (stream.baudrate, stream.bytesize, stream.parity, stream.stopbits) == (115200, 8, "N", 1)
            assert not (stream.xonxoff or stream.rtscts or stream.dsrdtr) and stream.dtr and stream.rts
        finally:
            stream.close()
    finally:
        os.close(master)
        os.close(slave)


# ---- C-15 / C-05: confirm -----------------------------------------------------------------------------------------

def test_c15_every_later_confirm_asks_for_the_revision_in_use():
    ep, hst = in_process()
    assert hst.confirm_range() == (1, 1) and hst.confirm()["revision"] == 1
    hst.revision = 3                                                   # as if a later probe had chosen revision 3
    assert hst.confirm_body() == m.CONFIRM_REQUEST + b"\x03\x03"
    with pytest.raises(h.Unsupported) as e:
        hst.confirm()
    assert e.value.supported == (1, 1) and ep.requests[-1].payload[4:6] == b"\x03\x03"


def test_c15_the_links_own_confirms_use_the_revision_in_use():
    stream = Stream(lambda req: [result(m.Request.unpack(req).corr, CONFIRM_V1)])
    lk = make_link(stream)
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    hst.revision = 2
    lk.resync()
    sent = m.Request.unpack(stream.writes[-1][2:])
    assert sent.op == m.OP_CONFIRM and sent.payload == m.CONFIRM_REQUEST + b"\x02\x02"


def test_c05_the_confirm_names_the_transport_and_port_speed_takes_it():
    probe = virtual_bench.esp32_v003()
    core_fn = probe.offered[0]
    two = virtual_bench.Offered(0, 0, virtual_bench.CORE_NAME, tuple(t for t in core_fn.tlvs if t[0] != virtual_bench.CORE_TRANSPORT)
                       + virtual_bench._transports([(virtual_bench.TRANSPORT["uart_bridge"], 0xFF)] * 2))
    probe = virtual_bench.VirtualProbe(probe.label, probe.max_frame, [two] + probe.offered[1:])
    ep, hst = in_process(lambda: probe, transport=1)
    assert hst.confirm()["transport"] == 1
    assert link._speed_port(hst) == (ep.link_fn, 1, "")                # the second bridge, not bridges[0]
    ep2, hst2 = in_process(virtual_bench.p4_x035, transport=1)                  # vendor bulk: not a UART bridge
    ep2.port_speed_base = 115200
    hst2.confirm()
    assert link._speed_port(hst2)[1] is None


def test_c05_a_relaying_brokers_0xff_and_a_confirm_without_the_tlv():
    hst = h.Host(lambda b: b)
    hst.limits = {"transport": 0xFF}
    hst._fns["oep.probe.link"] = 10                                    # oep.probe.link offering port_speed (its ops)
    hst._describes[10] = [(virtual_bench.catalog.OPS, virtual_bench.catalog.pack_ops({1, 2, 3}))]
    assert "broker" in link._speed_port(hst)[2]
    hst.limits = {"transport": None}
    assert "names no transport" in link._speed_port(hst)[2]


# ---- C-24: an x- unit_id names no unit ------------------------------------------------------------------------------

def test_c24_the_speed_record_keeps_nothing_under_an_x_unit_id(tmp_path):
    rec = speed_record.SpeedRecord(tmp_path / "r.json")
    rec.note("/dev/ttyUSB0", "x-esp32", 921600, True, "verify")
    assert rec.lookup("/dev/ttyUSB0", "x-esp32") == ([], []) and not rec.data
    rec.note("/dev/ttyUSB0", "fafe00000003", 921600, True, "verify")
    assert rec.lookup("/dev/ttyUSB0", "fafe00000003") == ([921600], [])


def test_c24_raise_speed_records_nothing_for_an_x_unit_id(tmp_path):
    from oep_client import virtual_bench_serial
    start = time.monotonic()
    ep = endpoint.Endpoint(virtual_bench.with_unit_id(virtual_bench.esp32_v003(), "x-esp32"),
                           lambda: int((time.monotonic() - start) * 1000))
    lk = link.SerialLink.on_stream(virtual_bench_serial.VirtualSerialStream(ep, 0), "cobs", 0.5)
    lk.transport = "serial"
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    core.take(hst, 10000)
    path = tmp_path / "speed.json"
    report = link.raise_speed(hst, [500000], record=str(path), verify_ms=900)
    assert report.chosen == 500000 and "names no unit" in report.why and not path.exists()


def test_c24_usb_by_an_x_unit_id_is_refused():
    with pytest.raises(ValueError, match="x-"):
        link.open_host("usb:x-esp32")


# ---- C-07 / transports §5: TCP and the resync wait ------------------------------------------------------------------------

class Paused(Stream):
    """A TCP-like stream whose frame arrives in two parts 300 ms apart."""
    keeps_boundaries = True

    def __init__(self, data):
        super().__init__()
        self.parts, self.t0 = [data[:3], data[3:]], time.monotonic()

    def read(self, n=1):
        if self.parts and (len(self.parts) == 2 or time.monotonic() - self.t0 > 0.3):
            self.rx += self.parts.pop(0)
        if not self.rx:
            time.sleep(0.01)
        return super().read(n)


def test_c07_on_tcp_a_pause_inside_a_frame_is_read_on():
    msg = result(7, b"abc")
    f = frames.LengthFrames(Paused(frame(msg)))
    assert f.stall_s is None and f.recv(1.0) == msg
    g = frames.LengthFrames(Paused(frame(msg)))
    g.stall_s = frames.STALL_S                                         # vendor bulk / HID: a lost boundary
    with pytest.raises(frames.FramingLost):
        g.recv(1.0)
    assert link.TcpStream.keeps_boundaries


def test_c07_a_resync_waits_250_ms_after_the_hosts_last_write():
    stream = Stream(lambda req: [result(m.Request.unpack(req).corr, CONFIRM_V1)])
    lk = make_link(stream)
    lk._write([m.Request(1, 0, m.OP_KEEPALIVE).pack()])
    t0 = time.monotonic()
    lk.resync()
    assert time.monotonic() - t0 >= 0.25


# ---- PC-1: the line search's step (c) ------------------------------------------------------------------------------

def test_pc1_find_line_takes_the_firmware_labels_as_step_c():
    items = [config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003")]
    assert config.find_line(items, "v003", "nrst", firmware=[(23, "NRST")]) == 23
    assert config.find_line(items + [config.Label(channel=22, text="nrst")], "v003", "nrst", [(23, "NRST")]) == 22
    assert config.find_line(items, "v003", "nrst", [(23, "NRST"), (24, "nrst")]) is None   # two at one step
    two = items + [config.Slot(slot=1, wire_fn=1, pins=(4, 0xFFFF), name="b")]
    assert config.find_line(two, "v003", "nrst", [(23, "NRST")]) is None   # two slots: (b) and (c) not searched
    ep, hst = in_process(virtual_bench.esp32_v003)
    assert config.find_line(hst, None, "nrst") == 23                   # a Host: describe's labels are read
    assert config.LINE_NAMES == ("nrst", "power_hi", "power_lo")       # from the registry (PC-2)


def test_pc5_a_label_the_probe_would_refuse_or_a_host_would_not_show_is_not_sent():
    """probe.config §1: a probe refuses a label outside 1-32 bytes; it no longer checks the characters (core §2.1), but
    this client still sends only text it would show unchanged (m.valid_text)."""
    for text in ("", "x" * 33, "a\tb"):
        with pytest.raises(ValueError):
            config.Label(channel=1, text=text).value()
    assert config.Label(channel=1, text="x" * 32).value()[2:] == b"x" * 32


# ---- C-22: text ---------------------------------------------------------------------------------------------------

def test_c22_text_from_an_answer_is_shown_without_control_characters():
    assert m.shown(b"ok\x1b[31mred\x7f\xff") == "ok�[31mred��"
    r = m.Result(1, m.REJECTED, m.LOCKED, struct.pack("<I", 5) + m.tlv(0x01, b"evil\x1b]0;x\x07"))
    assert "\x1b" not in h.Locked(r).owner and "\x07" not in str(h.Locked(r))


def test_c22_the_owner_goes_as_valid_text_of_at_most_32_bytes():
    assert h.owner_text("a\nb") == b"a?b"
    raw = h.owner_text("日" * 20)                                      # 3 bytes each: cut on a character
    assert len(raw) == 30 and raw.decode() == "日" * 10
    ep, hst = in_process()
    hst.open(3000, owner="日" * 20)
    assert ep.owner == raw


# ---- C-04 / C-10 ------------------------------------------------------------------------------------------------------

def test_no_ignored_marker_any_more():
    """core §2.3 (rule review 2026-10-07): an answer carries no ignored TLV - 0x7F is never a TLV tag, and the Tail has
    no ignored list."""
    t = m.Tail.parse(bytes([0x01, 1, 0, 0x31]))
    assert t.tlvs == [(0x01, b"\x31")] and not hasattr(t, "ignored") and not hasattr(t, "more_ignored")
    assert m.TAG_RESERVED == 0x7F and not hasattr(m, "TAG_IGNORED")
    with pytest.raises(ValueError):
        m.tlv(0x7F, b"")


def test_c10_dump_says_what_a_probe_must_give_and_did_not():
    caps = dump.collect(virtual_bench.esp32_v003().call)
    assert caps.missing == []
    bare = virtual_bench.VirtualProbe("bare", 256, [virtual_bench.Offered(0, 0, virtual_bench.CORE_NAME)])
    caps = dump.collect(bare.call)
    assert caps.missing == ["describe of fn 0: unit_id", "describe of fn 0: transport", "describe of fn 0: max_op_ms"]
    assert "MISSING" in dump.to_text(caps)


def _confirm_tail(probe: virtual_bench.VirtualProbe, tail: bytes):
    """probe.call with confirm's answer tail replaced by `tail`."""
    def call(fn, op, payload=b""):
        out = probe.call(fn, op, payload)
        return out[:17] + tail if (fn, op) == (dump.CORE_FN, dump.OP_CONFIRM) else out
    return call


def test_c10_confirm_transport_tlv_is_required():
    """A probe without confirm's transport TLV (core §7.1) is named as missing it, by dump and by the hardware test's
    check (dump.required_of). describe has no discoverable any more (core §7.5, rule review 2026-10-07)."""
    assert "discoverable" not in reg.CORE.tlv["describe"]
    call = _confirm_tail(virtual_bench.esp32_v003(), b"")
    want = ["confirm's transport TLV"]
    assert dump.collect(call).missing == want
    assert dump.required_of(call) == want
    assert "MISSING what every probe must give" in dump.to_text(dump.collect(call))
    # a transport index fn 0's describe does not declare; 0xFF (a relaying broker) names none and is fine
    assert dump.required_of(_confirm_tail(virtual_bench.esp32_v003(), bytes([0x01, 1, 0, 7]))) == \
        ["describe of fn 0: the transport confirm names (index 7)"]
    assert dump.required_of(_confirm_tail(virtual_bench.esp32_v003(), bytes([0x01, 1, 0, 0xFF]))) == []
    assert dump.required_of(_confirm_tail(virtual_bench.esp32_v003(), bytes([0x01, 2, 0, 0]))) == ["confirm's transport TLV (the answer's tail is broken)"]


@pytest.mark.parametrize("profile", sorted(virtual_bench.PROFILES))
def test_c10_every_virtual_profile_gives_what_is_required_on_every_transport(profile):
    """The virtual bench served on each of its transports (an Endpoint, as virtual_bench_serve and tests/hw's virtual board use it)."""
    for transport in endpoint.Endpoint(virtual_bench.PROFILES[profile](), Clock()).transports:
        ep, hst = in_process(virtual_bench.PROFILES[profile], transport)
        call = lambda fn, op, payload: hst.request(fn, op, payload, locked=False).payload   # noqa: E731
        assert dump.required_of(call, hst.confirm_range()) == [], (profile, transport)


# ---- the new answer TLVs in the API -------------------------------------------------------------------------------

def test_api_search_retries_step_left_internal_pullups_mode():
    ep, hst = in_process()
    hst.open(3000)
    w = riscv.Wire(hst)
    pair = ep.pairs[1][0]
    ep._target(1, pair).search_retries = 3
    conn, _ = w.attach(halt=True, pins=pair)
    assert w.search_retries == 3
    dm = riscv.RiscvDm(hst, conn)
    ep._target(1, pair).step_stuck = "runs"
    with pytest.raises(riscv.StepError) as e:
        dm.step()
    assert e.value.step_left and isinstance(e.value, riscv.TargetError)
    ep2, hst2 = in_process(lambda: virtual_bench.with_i2c_pullups(virtual_bench.p4_x035()))
    assert fixture.I2cTarget(hst2).internal_pullups is True               # features bit2 (fixture §3)
    ep3, hst3 = in_process(virtual_bench.p4_x035)
    assert fixture.I2cTarget(hst3).internal_pullups is False and not hasattr(fixture.I2cTarget, "pullup_ohms")
    text = dump.to_text(dump.collect(virtual_bench.p4_x035().call, "oep.fixture.logic"))
    assert "one-shot, max 1048576 samples x 1 segments" in text           # mode: mode max_samples max_segments


def test_p2_o8_capture_configure_sends_the_table_tlvs_without_the_critical_bit():
    """capture §3.3 (oep-spec c6ab5d9): every capture probe implements the table's TLVs, so the host sends them
    without the critical bit; only multirate goes critical."""
    ep, hst = in_process(virtual_bench.esp32_v003)
    hst.open(3000)
    core.plan_apply(hst, [(6, 0, 4)])
    cap = capture.LogicCapture(hst, 6)
    c = cap.configure(rate=1_000_000, samples=1 << 20, trigger=(1, 0, 1), pretrigger=4)
    tags = {t: v for t, v in m.split_tlvs(ep.requests[-1].payload)}
    assert set(tags) == {capture.MODE, capture.RATE, capture.TRIGGER, capture.PRETRIGGER, capture.SAMPLES}
    assert c.samples == 65536                                          # rounded down: the answer holds


def test_p2_o9_blocking_ms_sends_nothing_then_resyncs_a_length_link():
    calls = []

    class FakeLink:
        framing = "length"

        def resync(self):
            calls.append("resync")
    hst = h.Host(lambda b: b)
    hst.link = FakeLink()
    capture.blocked(hst, 250, sleep=lambda s: calls.append(s))
    assert calls == [0.25, "resync"]
    hst.link.framing = "cobs"
    calls.clear()
    capture.blocked(hst, 10, sleep=lambda s: calls.append(s))
    assert calls == [0.01]
    capture.blocked(hst, 0, sleep=lambda s: calls.append("never"))
    assert calls == [0.01]


def test_c18_the_session_id_is_random_and_never_0():
    ep, hst = in_process()
    ids = set()
    for _ in range(20):
        hst.open(3000, force=True)
        ids.add(hst.session)
    assert 0 not in ids and len(ids) > 1
    opens = [r for r in ep.requests if r.fn == 0 and r.op == m.OP_OPEN]
    assert len(opens) == 20 and {r.session for r in opens} == ids        # the id in the header (core §4.1, §6.4)
