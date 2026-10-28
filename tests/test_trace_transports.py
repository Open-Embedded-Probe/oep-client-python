"""Rule-by-rule trace of oep-spec a09fa45 docs/oep-transports.ja.md (docs/trace/transports.json): the rules no other
test pins down, checked on the virtual bench (its serial port, its USB model, its TCP serving) and on the client's link
(COBS, length frames, HID reports, USB bulk, TCP)."""

import struct
import sys
import time

import pytest

from oep_client import (cobs, endpoint, frames, hid_stream, host as h, link, message as m, registry as reg,
                        virtual_bench, virtual_bench_serial, virtual_bench_serve, virtual_bench_usb)

from test_link_host import CONFIRM_V1, Stream, frame, result


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def req(corr, op=m.OP_CLOCK, payload=b"", fn=m.CORE_FN, session=0):
    return m.Request(corr, fn, op, payload, session).pack()


def serial_port(profile=virtual_bench.esp32_v003):
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    return ep, clock, virtual_bench_serial.VirtualSerialPort(ep, min(ep.serial_ports))


def answers(port) -> list[int]:
    """The corr of every answer the port sends now (each a whole 0x00 <COBS> 0x00 frame)."""
    out = port.output()
    return [m.Result.unpack(cobs.unframe(c)).corr for c in out.split(b"\x00") if c]


def bad_crc(message: bytes) -> bytes:
    body = bytearray(message + struct.pack("<H", cobs.crc16(message)))
    body[-1] ^= 0xFF
    return b"\x00" + cobs.encode(bytes(body)) + b"\x00"


# ---- §1 frames: the serial port's COBS frame ---------------------------------------------------------------------------

def test_a_serial_frame_is_message_crc16_le_cobs_between_zeros_and_empty_frames_are_skipped():
    """transports §1: CRC-16/CCITT-FALSE ("123456789" -> 0x29B1) little endian after the message, COBS, 0x00 before
    and after - the probe's answers too; 0x00 0x00 (an empty frame) is read past; a decoder takes the last 254-byte
    block with and without the empty block after it."""
    assert cobs.crc16(b"123456789") == 0x29B1
    ep, clock, port = serial_port()
    port.feed(b"\x00\x00\x00" + cobs.frame(req(1)) + b"\x00\x00")
    out = port.output()
    assert out[0] == 0 and out[-1] == 0 and 0 not in out[1:-1]                   # one frame, both 0x00s
    body = cobs.decode(out[1:-1])
    assert body[-2:] == struct.pack("<H", cobs.crc16(body[:-2]))
    assert m.Result.unpack(body[:-2]).corr == 1 and port.output() == b""        # empty frames: nothing more
    full = bytes(range(1, 255))
    assert cobs.encode(full) == b"\xff" + full                                   # no empty block after it
    assert cobs.decode(b"\xff" + full) == cobs.decode(b"\xff" + full + b"\x01") == full


def test_the_probe_takes_only_candidates_between_zeros_and_the_closing_zero_opens_the_next():
    """transports §4: only 0x00 <COBS(message | CRC)> 0x00 is processed - bytes outside a candidate, a CRC mismatch
    and a candidate that does not decode are dropped; the 0x00 that closes a candidate also starts the next."""
    ep, clock, port = serial_port()
    f1, f2, f3 = cobs.frame(req(1)), cobs.frame(req(2)), cobs.frame(req(3))
    port.feed(b"raw bytes" + f1 + f2[1:] + bad_crc(req(9)) + b"\x00\x05\x01\x00" + f3)
    assert answers(port) == [1, 2, 3]


def test_the_probe_drops_a_frame_that_stops_for_probe_frame_gap_ms():
    """transports §2 / §4: a frame whose input stops 200 ms (probe_frame_gap_ms) half way is dropped and reading starts
    again; 199 ms is no gap."""
    assert reg.TIMING["probe_frame_gap_ms"] == 200
    ep, clock, port = serial_port()
    f1, f2 = cobs.frame(req(1)), cobs.frame(req(2))
    port.feed(f1[:5])
    clock.ms += 199
    port.feed(f1[5:])
    assert answers(port) == [1]
    port.feed(f2[:5])
    clock.ms += 200
    port.feed(f2[5:] + cobs.frame(req(3)))
    assert answers(port) == [3]


@pytest.mark.parametrize("profile", [virtual_bench.esp32_v003, virtual_bench.core_v1])
def test_the_probe_drops_a_frame_over_max_frame_and_what_follows_until_a_gap(profile):
    """transports §1: a length over max_frame - the probe drops that frame and the input until probe_frame_gap_ms
    passes with nothing, sends no answer, and takes the next frame after the gap."""
    ep, clock, port = serial_port(profile)
    big = req(1, m.OP_CLOCK, m.tlv(0x3E, bytes(ep.probe.max_frame)))
    port.feed(cobs.frame(big) + cobs.frame(req(2)))
    clock.ms += 100
    port.feed(cobs.frame(req(3)))
    assert answers(port) == []
    clock.ms += 200
    port.feed(cobs.frame(req(4)))
    assert answers(port) == [4]


def test_answers_and_notifications_go_as_whole_frames_one_after_another():
    """transports §4: answers and notifications use the same frames; one writer, no frame inside another."""
    ep, clock, port = serial_port()
    push = bytes([m.ROLE_DATA]) + bytes(40)
    ep.pushes = lambda: [push]                                     # the probe sends a frame by itself every tick
    port.feed(cobs.frame(req(1)))
    port.tick()
    port.feed(cobs.frame(req(2)))
    port.tick()
    out = port.output()
    got = [cobs.unframe(c) for c in out.split(b"\x00") if c]
    assert out.count(b"\x00") == 2 * len(got)                       # every frame whole, 0x00 on both sides
    assert [g[0] for g in got] == [m.ROLE_RESULT, m.ROLE_DATA, m.ROLE_RESULT, m.ROLE_DATA]


def test_a_usb_serial_port_takes_any_line_coding():
    """transports §4: on a USB serial port (USB CDC, the built-in USB serial) the probe sends and takes OEP whatever line
    coding the host set; only a UART bridge's line has a rate."""
    ep = endpoint.Endpoint(virtual_bench.rp2350_pins(), Clock())                  # transport 0: USB CDC
    assert ep.transports[0] == virtual_bench.TRANSPORT["usb_cdc"]
    lk = link.SerialLink.on_stream(virtual_bench_serial.VirtualSerialStream(ep, 0, baudrate=9600), "cobs", 0.5)
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    assert hst.confirmed()["revision"] == 1
    bridge = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())               # a UART bridge at another rate
    stream = virtual_bench_serial.VirtualSerialStream(bridge, 0, baudrate=9600)
    assert not stream._matched()


def test_the_host_reads_the_span_before_the_first_zero_as_a_candidate():
    """transports §1 host COBS: from the port's open to the first 0x00 is a candidate too (the leading 0x00 may have
    gone before the open); a candidate that does not decode or fails its CRC, and a frame of another role or corr, is
    a broken candidate and dropped."""
    from test_link_serial import Scripted
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", 0.3)
    s.rx += cobs.frame(result(7))[1:]                                     # its leading 0x00 lost before the open
    assert m.Result.unpack(lk.send(req(7))).corr == 7
    s.rx += (bad_crc(result(8)) + cobs.frame(req(8)) + cobs.frame(result(99)) + cobs.frame(result(8)))
    lk.resend = False
    with pytest.raises(cobs.CorruptFrame):
        lk.send(req(8))                                                   # the broken candidate: taken for the answer
    assert lk.noise > 0
    assert m.Result.unpack(lk._recv_for(8)).corr == 8                     # a request role and corr 99 read past
    assert lk.dropped == 1 and lk.stale == 1


# ---- §1 frames: length frames, HID reports, USB bulk ----------------------------------------------------------------

class Trickle(Stream):
    """A stream that gives one byte a read."""

    def read(self, n=1):
        return super().read(1)


def test_length_frames_skip_length_0_and_need_no_boundaries_from_the_reads():
    """transports §1 / §2: length(u16) message, no CRC; length 0 is the reserved keepalive and skipped; a frame may
    come in any number of reads, several in one."""
    s = Trickle()
    s.rx += b"\x00\x00" + frame(result(1)) + b"\x00\x00" + frame(result(2))
    f = frames.LengthFrames(s)
    assert m.Result.unpack(f.recv(1.0)).corr == 1 and m.Result.unpack(f.recv(1.0)).corr == 2


def test_the_host_reads_a_65535_byte_message():
    """transports §3: the host takes messages of up to 65535 bytes from the probe."""
    s = Stream()
    big = bytes([m.ROLE_DATA]) + bytes(65534)
    s.rx += frame(big)
    assert frames.LengthFrames(s).recv(1.0) == big


class Backend:
    def __init__(self, reports):
        self.reports, self.sent = list(reports), []

    def read_report(self, timeout):
        if self.reports:
            return self.reports.pop(0)
        time.sleep(timeout / 10)
        return b""

    def write_report(self, payload):
        self.sent.append(payload)

    def close(self):
        pass


def test_hid_reports_carry_counted_bytes_skip_count_0_and_ignore_any_padding():
    """transports §1 HID rules 1-3: the counted bytes after count(u16), joined in report order, are the length-framed
    stream; a report of count 0 is empty; the sender pads with 0, the receiver ignores the padding whatever it is."""
    msg = frame(result(5))
    reports = [len(msg[:4]).to_bytes(2, "little") + msg[:4] + b"\xff" * 4, b"\x00\x00" + b"\xee" * 8,
               len(msg[4:]).to_bytes(2, "little") + msg[4:] + b"\x55" * (8 - len(msg[4:]))]
    s = hid_stream.HidReportStream(Backend(reports), 10, 10)
    try:
        lf = frames.LengthFrames(s)
        assert m.Result.unpack(lf.recv(1.0)).corr == 5
        s.write(msg)
        assert all(len(r) == 10 for r in s.backend.sent)
        last = s.backend.sent[-1]
        assert last[2 + (last[0] | last[1] << 8):] == bytes(10 - 2 - (last[0] | last[1] << 8))   # zero padding
    finally:
        s.close()


def test_hid_reports_keep_the_declared_report_id_and_drop_another():
    """transports §1 HID rule 4: with a report ID declared every report starts with it both ways (count after it);
    a report with another ID is dropped."""

    class Device:
        def __init__(self):
            self.written, self.reads = [], [bytes([7]) + b"\x01\x00a" + bytes(5), bytes([8]) + b"\x01\x00b" + bytes(5)]

        def read(self, n, timeout_ms):
            return list(self.reads.pop(0)) if self.reads else []

        def write(self, data):
            self.written.append(bytes(data))
            return len(data)

    dev = Device()
    b = hid_stream.HidapiBackend(dev, 7)
    assert b.read_report(0.1) == b"\x01\x00a" + bytes(5) and b.read_report(0.1) == b""
    b.write_report(b"\x01\x00q" + bytes(5))
    assert dev.written == [bytes([7]) + b"\x01\x00q" + bytes(5)]


class UsbClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def usb_model(kind, **kw):
    clock = UsbClock()
    ep = endpoint.Endpoint(virtual_bench.core_v1(), lambda: int(clock() * 1000), boot_id=17)
    return clock, virtual_bench_usb.Usb(ep, kind, clock=clock, sleep=clock.sleep, **kw)


def usb_answers(usb) -> list[int]:
    data = b"".join(usb.inbox) if usb.kind == "bulk" else b"".join(
        r[1 + 2:1 + 2 + (r[1] | r[2] << 8)] for r in usb.inbox)          # report ID 6, count, bytes
    usb.inbox.clear()
    out, at = [], 0
    while at < len(data):
        n = struct.unpack_from("<H", data, at)[0]
        out.append(m.Result.unpack(data[at + 2:at + 2 + n]).corr)
        at += 2 + n
    return out


def hid_reports(usb, data: bytes) -> list[bytes]:
    room = usb.output_size - 3
    return [bytes([usb.report_id]) + len(data[i:i + room]).to_bytes(2, "little") + data[i:i + room]
            + bytes(room - len(data[i:i + room])) for i in range(0, len(data), room)]


@pytest.mark.parametrize("kind", ["bulk", "hid"])
def test_usb_probe_drops_a_frame_that_stops_for_probe_frame_gap_ms(kind):
    """transports §2 and HID rule 5: on vendor bulk and HID a frame whose input stops 200 ms is dropped and the next
    byte of the stream is read as a length."""
    clock, usb = usb_model(kind)
    send = (lambda d: [usb.write(r) for r in hid_reports(usb, d)]) if kind == "hid" else usb.write
    f1, f2 = frame(req(1)), frame(req(2))
    send(f1[:4])
    clock.now += 0.2
    send(f1[4:])
    assert usb_answers(usb) == []
    clock.now += 0.2
    send(f2)
    assert usb_answers(usb) == [2]


def test_hid_count_over_the_report_breaks_the_stream_until_a_gap_at_the_probe():
    """transports §1 HID rule 6: a count over what the report holds (its length - 2, - 3 with a report ID) - the report
    is dropped and the input until 200 ms pass with nothing."""
    clock, usb = usb_model("hid")
    bad = bytes([usb.report_id]) + (usb.output_size - 2).to_bytes(2, "little") + bytes(usb.output_size - 3)
    usb.write(bad)
    for r in hid_reports(usb, frame(req(1))):
        usb.write(r)
    assert usb_answers(usb) == []
    clock.now += 0.2
    for r in hid_reports(usb, frame(req(2))):
        usb.write(r)
    assert usb_answers(usb) == [2]


def test_the_probe_ends_a_whole_packet_bulk_answer_with_a_zero_length_packet():
    """transports §1 USB bundling: an answer whose last transfer is a multiple of wMaxPacketSize is followed by a
    zero-length packet."""
    clock, usb = usb_model("bulk", out_packet_size=8)
    usb.respond(bytes(6))                                           # 2 + 6 = 8 bytes: one whole packet
    assert list(usb.inbox) == [struct.pack("<H", 6) + bytes(6), b""]
    usb.inbox.clear()
    usb.respond(bytes(7))
    assert [len(t) for t in usb.inbox] == [8, 1]                     # a short last packet ends it already


def test_the_host_ends_a_whole_packet_bulk_write_with_a_zero_length_packet(monkeypatch):
    """transports §1 USB bundling: the host follows a write whose length is a multiple of wMaxPacketSize with a
    zero-length transfer - the OUT endpoint's own size (64 at full speed), on both USB backends."""
    from oep_client import usb_stream
    import test_usb_stream as fake

    class EpOut:
        wMaxPacketSize = 64

        def __init__(self):
            self.writes = []

        def write(self, data, timeout):
            self.writes.append(bytes(data))
            return len(data)

    s = object.__new__(usb_stream.UsbBulkStream)
    s.ep_out = EpOut()
    for n in (64, 63, 128):
        s.write(bytes(n))
    assert [len(w) for w in s.ep_out.writes] == [64, 0, 63, 128, 0]

    class Handle(fake.Handle):
        def __init__(self, ctx):
            super().__init__(ctx)
            self.writes = []

        def bulkWrite(self, ep, data, timeout):
            self.writes.append(bytes(data))
            return len(data)

    monkeypatch.setitem(sys.modules, "usb1", fake.usb1)
    ctx = fake.Context()
    hd = Handle(ctx)
    a = usb_stream.UsbAsyncStream(ctx, hd, 0x81, 0x01, 0, 64)
    try:
        for n in (64, 63, 512):
            a.write(bytes(n))
        assert [len(w) for w in hd.writes] == [64, 0, 63, 512, 0]
    finally:
        a.close()


# ---- §1: framing by the kind of transport, TCP --------------------------------------------------------------------

def test_the_framing_follows_the_kind_of_transport_not_the_vid_pid(monkeypatch):
    """transports §1: COBS on a serial port, length frames on vendor bulk, HID and TCP - by the kind of transport alone;
    a USB device of any VID:PID gets length frames."""
    monkeypatch.setattr(link.SerialLink, "attach_host", lambda self, hst: self._bind_host(hst))   # no probing here
    monkeypatch.setattr(link, "open_serial", lambda port, baud: Stream())

    class Tcp(Stream):
        keeps_boundaries = True

        def __init__(self, addr, port):
            super().__init__()

    monkeypatch.setattr(link, "TcpStream", Tcp)
    monkeypatch.setattr(link, "_open_usb_stream", lambda kind, vid, pid, serial, *sizes: Stream())
    assert link.open_host("/dev/ttyUSB9", keep_session=False).link.framing == "cobs"
    assert link.open_host("tcp://127.0.0.1:7450", keep_session=False).link.framing == "length"
    for target in ("usb:1209:4f45", "usb:303a:1001"):
        assert link.open_host(target, keep_session=False).link.framing == "length"


def test_the_virtual_bench_listens_on_the_local_host_unless_told():
    """transports §1: TCP only on a trusted local connection (or inside an authenticated tunnel) - the virtual bench's
    TCP listener is on 127.0.0.1 unless --listen names another address."""
    assert virtual_bench_serve.parse(["--tcp", "0"]).listen == "127.0.0.1"


class TcpStream(Stream):
    """A TCP connection stand-in: the boundaries never get lost (transports §2)."""
    keeps_boundaries = True

    def __init__(self, respond=None):
        super().__init__(respond)
        self.closed = False

    def close(self):
        self.closed = True


def confirming(by_corr=None):
    """confirm answered v1 (boot_id 0x11); any other request answered by by_corr(corr) frames or a plain success."""
    def respond(msg):
        r = m.Request.unpack(msg)
        if r.op == m.OP_CONFIRM:
            return [result(r.corr, CONFIRM_V1)]
        return by_corr(r.corr) if by_corr else [result(r.corr)]
    return respond


def test_on_tcp_an_answer_to_a_corr_not_waited_for_is_read_past():
    """transports §5: on TCP a normal answer whose corr is not waited for is dropped, not taken for broken framing - no
    resync, no new connection."""
    s = TcpStream(confirming(lambda corr: [result(corr + 100), result(corr)]))
    lk = link.SerialLink.on_stream(s, "length", 0.3)
    assert m.Result.unpack(lk.send(req(7))).corr == 7
    assert lk.stale == 1 and lk.resyncs == 0 and not s.closed


def test_on_tcp_a_pause_or_a_timeout_keeps_the_frame_being_read():
    """transports §2 / §5: TCP keeps its boundaries - a pause inside a frame is no lost boundary and a timeout drops
    nothing of a frame half read."""
    reply = frame(result(7))
    s = TcpStream(lambda msg: [])
    lk = link.SerialLink.on_stream(s, "length", 0.2)
    lk.resend = False
    s.rx += reply[:3]
    with pytest.raises(TimeoutError):
        lk.send(req(7))
    s.rx += reply[3:]
    assert lk.pump(0.3) == 1 and lk.stale == 1 and lk.resyncs == 0 and not s.closed


def test_on_tcp_broken_framing_closes_and_opens_the_connection_and_confirms_there():
    """transports §5: a TCP length or frame that is broken - the connection is closed and opened again (quiet input and
    a confirm do not reset a TCP parser); the new connection is confirmed (its boot_id seen) and only the request still
    unresolved goes again with its corr (§2: nothing resolved is sent again, no old queue carried over)."""
    old = TcpStream(lambda msg: [])
    new = TcpStream(confirming())
    lk = link.SerialLink.on_stream(old, "length", 0.3)
    lk.frames.max_frame = lk.max_frame = 1024
    old.rx += struct.pack("<H", 2000)                                       # a length over max_frame
    lk.reopener = lambda: new
    boots = []
    lk.on_boot_id = boots.append
    assert m.Result.unpack(lk.send(req(7))).corr == 7
    assert old.closed and boots == [0x11]
    sent = [m.Request.unpack(w[2:]) for w in new.writes]
    assert [(r.op, r.corr) for r in sent][-1] == (m.OP_CLOCK, 7) and sent[0].op == m.OP_CONFIRM
    assert [r.op for r in sent].count(m.OP_CLOCK) == 1
    lk2 = link.SerialLink.on_stream(TcpStream(lambda msg: []), "length", 0.3)
    lk2.frames.max_frame = 1024
    lk2.stream.rx += struct.pack("<H", 2000)
    with pytest.raises(ConnectionError, match="close the connection and open it again"):
        lk2.send(req(8))                                                # nothing to open again: said, not resynced


# ---- §2 sending --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("framing", ["cobs", "length"])
def test_the_host_writes_frames_whole_in_one_write(framing):
    """transports §2: no sender stops probe_frame_gap_ms inside a frame (TCP aside) - the host writes each batch of
    frames in one write."""
    s = Stream()
    lk = link.SerialLink.on_stream(s, framing, 0.3)
    s.writes.clear()
    lk._write([req(1), req(2, m.OP_LIST, b"\x00\x00")])
    assert len(s.writes) == 1
    data = s.writes[0]
    if framing == "cobs":
        assert [m.Request.unpack(cobs.unframe(c)).corr for c in data.split(b"\x00") if c] == [1, 2]
    else:
        assert data == frame(req(1)) + frame(req(2, m.OP_LIST, b"\x00\x00"))


def test_answers_on_one_transport_keep_the_order_of_the_requests():
    """transports §2: on one transport whole messages arrive in the order they were sent."""
    ep, clock, port = serial_port()
    port.feed(b"".join(cobs.frame(req(c, op)) for c, op in ((5, m.OP_CLOCK), (3, m.OP_LOCK_STATE), (9, m.OP_CLOCK))))
    assert answers(port) == [5, 3, 9]


# ---- §3 several transports, max_frame ---------------------------------------------------------------------------------

def test_the_probe_does_not_check_which_transport_a_sessions_request_came_on():
    """transports §3: a host sends one session's requests on one transport; the probe does not check it - the same
    session's request on another transport runs (a misjudgement is the host's), and session 0 may go anywhere."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    opened = m.Result.unpack(ep.handle(req(1, m.OP_OPEN, struct.pack("<IB", 3000, 0), session=0x51), 0))
    assert opened.succeeded
    assert m.Result.unpack(ep.handle(req(2, m.OP_KEEPALIVE, session=0x51), 1)).succeeded
    assert m.Result.unpack(ep.handle(req(3, m.OP_LOCK_STATE), 2)).succeeded


def test_max_frame_binds_the_host_and_is_64_until_a_confirm_answer():
    """transports §3: the host sends no message over max_frame - and none over 64 bytes (min_max_frame) before
    confirm's answer; every probe takes 64-byte messages before a confirm."""
    assert reg.MIN_MAX_FRAME == 64
    lk = link.SerialLink.on_stream(Stream(), "cobs", 0.3)
    lk._write([bytes(64)])
    with pytest.raises(ValueError, match="over max_frame 64"):
        lk._write([bytes(65)])
    hst = h.Host(lk.send)
    lk._bind_host(hst)
    hst.on_limits({"max_frame": 1024})
    lk._write([bytes(1024)])
    with pytest.raises(ValueError):
        lk._write([bytes(1025)])
    for profile in (virtual_bench.esp32_v003_64, virtual_bench.core_v1):
        ep = endpoint.Endpoint(profile(), Clock())
        msg = req(1, m.OP_CLOCK, m.tlv(0x3E, bytes(64 - m.REQUEST_HEADER - 3)))
        assert len(msg) == 64 and m.Result.unpack(ep.handle(msg, 0)).corr == 1


def test_the_probe_sends_nothing_over_max_frame():
    """transports §3: the probe's answers stay within max_frame - a read asked for 65535 bytes is cut to fit."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003_64(), Clock())
    hst = h.Host(ep.handle)
    hst.open(3000)
    from oep_client import core, fixture
    core.plan_apply(hst, [(5, 1, 21)])
    uart = fixture.FixtureUart(hst, 5)
    uart.configure(115200)
    ep.uart_rx(5, bytes(range(200)))
    raw = ep.handle(req(hst.next_corr(), reg.FIXTURE_UART.op["read"], struct.pack("<BQH", 1, 0, 0xFFFF), fn=5), 0)
    assert len(raw) <= ep.probe.max_frame == 64


# ---- §4 the serial port -------------------------------------------------------------------------------------------

def test_a_uart_bridge_boots_at_115200_8n1_and_port_speed_changes_the_rate_alone():
    """transports §4: 8 data bits, no parity, 1 stop bit, no flow control, 115200 bps at boot (uart_bridge_boot_baud);
    the host opens every serial port so (link.open_serial) and a set_baud changes the rate and nothing else."""
    assert reg.TIMING["uart_bridge_boot_baud"] == link.BASE_BAUD == 115200

    class Port(Stream):
        baudrate, bytesize, parity, stopbits = 115200, 8, "N", 1

    s = Port()
    lk = link.SerialLink.on_stream(s, "cobs", 0.3)
    lk.set_baud(500000)
    assert (s.baudrate, s.bytesize, s.parity, s.stopbits) == (500000, 8, "N", 1)


def test_the_host_takes_the_vendor_interface_by_class_subclass_protocol_alone(monkeypatch):
    """transports §3: in a device known as an OEP probe the vendor bulk way in is the interface of class 0xFF, subclass
    0x4F ('O'), protocol 0x45 ('E') with a bulk IN / OUT pair - another class 0xFF interface (a built-in USB serial's
    debug function, WebUSB) is not taken."""
    import types
    from oep_client import usb_stream
    assert (usb_stream.VENDOR_CLASS, usb_stream.VENDOR_SUBCLASS, usb_stream.VENDOR_PROTOCOL) == (0xFF, 0x4F, 0x45)
    assert (reg.USB["hid_usage_page"], reg.USB["hid_usage"]) == (0xFF4F, 0x45)

    def ep(address):
        return types.SimpleNamespace(bmAttributes=2, bEndpointAddress=address)

    class Intf:
        def __init__(self, n, cls, sub, proto):
            self.bInterfaceNumber, self.bInterfaceClass, self.bInterfaceSubClass, self.bInterfaceProtocol = n, cls, sub, proto

        def __iter__(self):
            return iter([ep(0x80 | self.bInterfaceNumber), ep(self.bInterfaceNumber)])

    intf = Intf

    dev = types.SimpleNamespace(get_active_configuration=lambda: [intf(1, 0xFF, 0x00, 0x00), intf(2, 0xFF, 0x4F, 0x01),
                                                                  intf(3, 0xFF, 0x4F, 0x45)])
    util = types.SimpleNamespace(ENDPOINT_TYPE_BULK=2, ENDPOINT_IN=0x80, ENDPOINT_OUT=0,
                                 endpoint_type=lambda a: a & 3, endpoint_direction=lambda a: a & 0x80,
                                 claim_interface=lambda d, n: None, get_string=lambda d, i: "")
    usb = types.SimpleNamespace(core=types.SimpleNamespace(find=lambda **kw: dev), util=util)
    for name, mod in (("usb", usb), ("usb.core", usb.core), ("usb.util", util)):
        monkeypatch.setitem(sys.modules, name, mod)
    taken = []
    monkeypatch.setattr(usb_stream.UsbBulkStream, "__init__",
                        lambda self, d, i, o, n: taken.append((i.bEndpointAddress, o.bEndpointAddress, n)))
    usb_stream.UsbBulkStream.open(0x1209, 0x4F45)
    assert taken == [(0x83, 0x03, 3)]


# ---- review additions (independent review of the trace) ------------------------------------------------------------

@pytest.mark.parametrize("kind", ["bulk", "hid"])
def test_after_a_gap_the_probe_reads_the_next_byte_as_a_length(kind):
    """transports §2 and §1 HID rule 5: once a frame stopped probe_frame_gap_ms half way and was dropped, the next byte of
    the stream starts a length - a whole frame sent right after the gap is answered, with nothing else in between."""
    clock, usb = usb_model(kind)
    send = (lambda d: [usb.write(r) for r in hid_reports(usb, d)]) if kind == "hid" else usb.write
    f1, f2 = frame(req(1)), frame(req(2))
    send(f1[:4])
    clock.now += 0.2
    send(f2)
    assert usb_answers(usb) == [2]


def test_a_named_unit_id_is_compared_without_letter_case(monkeypatch):
    """transports §3 named probe: the unit_id named and the describe's unit_id are compared without letter case (as the
    USB serial number is) - USB:30EDA0E3B001 is the probe whose describe says 30eda0e3b001."""
    from test_usb_identity import LengthStream, fake_ep, ops
    s = LengthStream(fake_ep(unit_id="30eda0e3b001"))
    asked = []
    monkeypatch.setattr(link, "find_usb", lambda unit_id: asked.append(unit_id) or (0x1234, 0x5678))
    monkeypatch.setattr(link, "_open_usb_stream", lambda kind, vid, pid, serial, *sizes: s)
    hst = link.open_host("usb:30EDA0E3B001")
    assert not s.closed and hst.limits is not None and m.OP_DESCRIBE in ops(s.sent)


def test_the_probing_confirm_waits_core_quick_op_max_ms_and_host_wait_add_ms():
    """transports §3 probing rule: before max_op_ms is known the confirm (and its one resend) waits
    core_quick_op_max_ms + host_wait_add_ms (2000 ms) and the transfer time; afterwards the link's own wait is back."""
    assert link.PROBE_WAIT_S == (reg.TIMING["core_quick_op_max_ms"] + reg.TIMING["host_wait_add_ms"]) / 1000 == 2.0
    lk = link.SerialLink.on_stream(Stream(), "length", 0.3)
    seen = []

    class Probing:
        def confirm(self):
            seen.append((lk.timeout, lk.resend))
            return {"revision": 1}

    assert lk.probe(Probing()) == {"revision": 1}
    assert seen == [(2.0 + lk.transfer_s(), True)] and lk.timeout == 0.3


def test_the_hid_is_opened_exclusively(monkeypatch, tmp_path):
    """transports §3: the host opens the HID exclusively where the OS allows it - on Linux an flock on the hidraw node:
    a second open is refused (HidBusy, and libusb is not tried on it), and closing the first frees it."""
    if not sys.platform.startswith("linux"):
        pytest.skip("flock on the hidraw node is Linux's")
    import types
    from test_hid_stream import espusbdevice_vendor_descriptor
    node = tmp_path / "hidraw7"
    node.write_bytes(b"")

    class Device:
        def open_path(self, path):
            self.path = path

        def get_report_descriptor(self):
            return list(espusbdevice_vendor_descriptor(63))

        def read(self, n, timeout_ms):
            time.sleep(timeout_ms / 1000)
            return []

        def close(self):
            pass

    hid = types.SimpleNamespace(enumerate=lambda vid, pid: [{"usage_page": 0xFF4F, "path": str(node).encode(),
                                                             "serial_number": "x"}], device=Device)
    monkeypatch.setitem(sys.modules, "hid", hid)
    monkeypatch.setattr(hid_stream, "_open_usb1", lambda *a: pytest.fail("libusb tried on a busy HID"))
    first = hid_stream.open_hid(0x1209, 0x4F45)
    with pytest.raises(hid_stream.HidBusy):
        hid_stream.open_hid(0x1209, 0x4F45)
    first.close()
    hid_stream.open_hid(0x1209, 0x4F45).close()


def test_a_host_that_may_have_raised_the_speed_sets_its_own_limit_for_the_boot_speed():
    """transports §4: a host that handles port_speed retries confirm at the boot speed until the probe is back, with a
    limit it sets itself, counting the earlier requests' processing and the frames' transfer (the link's wait) -
    port_speed_idle_ms + host_wait_add_ms is not its only bound."""
    assert link.OPEN_RETRY_S == (reg.TIMING["port_speed_idle_ms"] + reg.TIMING["host_wait_add_ms"]) / 1000
    lk = link.SerialLink.on_stream(Stream(), "cobs", 0.3)
    asked = []
    lk._confirm_within = lambda wait_s, each_s: asked.append(wait_s) or True
    lk.wait_boot_speed()
    lk.timeout = link.OPEN_RETRY_S + 5.0                                # a long request's wait before it
    lk.wait_boot_speed()
    assert asked == [link.OPEN_RETRY_S, link.OPEN_RETRY_S + 5.0]


class Noisy(Stream):
    def __init__(self, respond=None):
        super().__init__(respond)
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize("reopens", [True, False])
def test_a_length_port_that_never_goes_quiet_is_closed_and_opened_again(monkeypatch, reopens):
    """transports §5: vendor bulk / HID with lost boundaries - the host reads and discards; when the input cannot be made
    quiet it closes the transport and opens it again (nothing that changes state goes out blind), then confirms with
    its own corr there; with no way to open it again it says so."""
    monkeypatch.setattr(link.SerialLink, "NOISY_S", 0.05)
    s = Noisy(lambda msg: [])
    lk = link.SerialLink.on_stream(s, "length", 0.3)
    hst = h.Host(lk.send)
    hst.session, hst.revision, hst.subscriptions = 0xABCD, 1, {5}
    lk._bind_host(hst)                                           # what open_host binds
    s.noise = frame(bytes([m.ROLE_DATA]) + struct.pack("<HHQ", 5, 0, 0) + b"x" * 32)   # pushes that never stop
    new = Noisy(confirming())
    lk.reopener = (lambda: new) if reopens else None
    boots = []
    lk.on_boot_id = boots.append
    lk.frames.max_frame = 1024
    s.rx += struct.pack("<H", 5000) + b"garbage"                          # a lost boundary starts the resync
    if not reopens:
        with pytest.raises(ConnectionError, match="close the transport and open it again"):
            lk.send(req(hst.next_corr(), 0x01, fn=5, session=0xABCD))
        return
    lk.send(req(hst.next_corr(), 0x01, fn=5, session=0xABCD))             # sent again there after the confirm
    assert s.closed and lk.stream is new
    assert [m.Request.unpack(w[2:]).op for w in s.writes] == [0x01]       # no blind unsubscribe / end
    sent = [m.Request.unpack(w[2:]) for w in new.writes]
    assert [r.op for r in sent] == [m.OP_CONFIRM, 0x01] and boots == [0x11]   # confirmed there, its boot_id seen
    assert hst.session == 0xABCD and hst.subscriptions == {5}


# ---- second review -------------------------------------------------------------

def test_a_closed_route_drops_what_it_had_and_rolls_nothing_back():
    """transports §3: a transport closing leaves the session, the lock and the core §5.1 table; a request it had already
    carried out stays done, one it had not yet run is dropped (never run); on another transport the request sent again
    gets its recorded answer (not run twice) and the order state is not rolled back (a corr behind is result_lost)."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    sid = 0x51
    assert m.Result.unpack(ep.handle(req(1, m.OP_OPEN, struct.pack("<IB", 3000, 0), session=sid), 0)).succeeded
    done, waiting = req(2, m.OP_KEEPALIVE, session=sid), req(3, m.OP_LOCK_STATE, session=sid)
    assert ep.receive(done, 0) == "queued" and ep.receive(waiting, 0) == "queued"
    answer = ep.process(0)
    runs = ep.executions
    ep.close_route(0)                                                    # closed before either answer went out
    assert ep.credits() == {} and ep.executions == runs                   # the waiting one dropped, not run
    assert ep.holder == sid and ep.last == sid
    assert ep.handle(done, 1) == answer and ep.executions == runs         # sent again elsewhere: the record
    lost = m.Result.unpack(ep.handle(req(0xF000, m.OP_KEEPALIVE, session=sid), 1))
    assert (lost.resolution, lost.detail) == (m.REJECTED, m.RESULT_LOST)  # behind the last corr: not rolled back
    assert m.Result.unpack(ep.handle(waiting, 1)).succeeded               # the dropped one, sent again, runs once


def test_notifications_for_a_closed_connection_are_dropped_and_not_carried_to_a_new_one():
    """transports §3, core §10.3 / §10.4: when the subscriber's TCP connection closes the session and its subscription
    stay, but what was to go there is dropped - a new connection taking the session back gets none of it (nor another
    connection); a subscribe again there sends from seq 0, nothing of the old route carried over."""
    import select
    import socket
    import subprocess
    from oep_client import capture as cap, core
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    port = int(proc.stdout.readline().decode().split()[1])

    class Raw:
        def __init__(self):
            self.sock, self.buf, self.corr = socket.create_connection(("127.0.0.1", port), timeout=5), bytearray(), 0

        def send(self, fn, op, payload=b"", session=0):
            self.corr += 1
            msg = m.Request(self.corr, fn, op, payload, session).pack()
            self.sock.sendall(struct.pack("<H", len(msg)) + msg)
            return self.corr

        def frames(self, seconds):
            out, deadline = [], time.monotonic() + seconds
            while (left := deadline - time.monotonic()) > 0:
                if select.select([self.sock], [], [], left)[0]:
                    data = self.sock.recv(65536)
                    if not data:
                        break
                    self.buf += data
                while len(self.buf) >= 2 and len(self.buf) >= 2 + struct.unpack_from("<H", self.buf)[0]:
                    n = struct.unpack_from("<H", self.buf)[0]
                    out.append(bytes(self.buf[2:2 + n]))
                    del self.buf[:2 + n]
            return out

    try:
        a = link.open_host(f"tcp://127.0.0.1:{port}", keep_session=False)
        core.take(a, 5000)
        sid = a.session
        lc = cap.LogicCapture(a)
        core.plan_apply(a, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
        lc.configure(rate=100_000, mode=cap.STREAMING)
        lc.subscribe()
        lc.start()
        assert len(lc.stream(a.link, nbytes=500, seconds=3).data) >= 500
        a.link.close()                                                    # no unsubscribe, no end: it just goes
        time.sleep(0.3)                                                   # data made meanwhile, for the closed one
        watcher, taker = Raw(), Raw()
        taker.corr = a._corr + 10
        corr = taker.send(m.CORE_FN, m.OP_OPEN, struct.pack("<IB", 5000, 0), session=sid)
        got = taker.frames(0.5)
        assert [m.Result.unpack(f).corr for f in got if f[0] == m.ROLE_RESULT] == [corr]
        assert m.Result.unpack(next(f for f in got if f[0] == m.ROLE_RESULT)).succeeded   # the session stayed
        assert not [f for f in got if f[0] in (m.ROLE_DATA, m.ROLE_EVENT)]     # nothing of the closed route
        assert not [f for f in watcher.frames(0.2) if f[0] != m.ROLE_RESULT]
        taker.send(lc.fn, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0), session=sid)
        pushed = [f for f in taker.frames(0.5) if f[0] in (m.ROLE_DATA, m.ROLE_EVENT)]
        assert pushed and struct.unpack_from("<H", pushed[0], 3)[0] == 0      # from seq 0: the new subscription's own
        taker.send(m.CORE_FN, m.OP_END, session=sid)
        taker.frames(0.2)
        for x in (watcher, taker):
            x.sock.close()
    finally:
        proc.stdin.close()
        proc.wait(5)


@pytest.mark.parametrize("named, txt", [("FAFE00000003", "fafe00000003"), ("fafe00000003", "FAFE00000003")])
def test_a_tcp_probe_named_by_unit_id_is_matched_without_letter_case(monkeypatch, named, txt):
    """transports §3 TCP: a probe named by unit_id may be chosen by its TXT unit_id and is used when describe's unit_id
    is the one named - compared as for a named USB probe, without letter case (TXT, describe and the name)."""
    import re
    import subprocess
    from oep_client import discovery
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length",
                             "--profile", "esp32-v003"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    port = int(re.search(r"PORT (\d+)", proc.stdout.readline()).group(1))
    try:
        monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto":
                            [discovery.Found("OEP x", txt, "oep-x.local.", port, ["127.0.0.1"])])
        hst = link.open_host(f"tcp:{named}", keep_session=False)          # describe says fafe00000003
        try:
            assert hst.limits is not None and hst.link.transport == "tcp"
        finally:
            hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)


# ---- third review additions ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(virtual_bench.PROFILES))
def test_a_probe_offers_at_most_one_vendor_bulk_and_one_hid(name):
    """transports §3 (choosing the interface): a probe offers vendor bulk and HID in the OEP form and at most one of
    each - every profile's fn 0 describe lists at most one transport of kind 4 and one of kind 5, and the virtual
    bench refuses a second vendor bulk or HID (its USB model included) while one of each is fine."""
    T = virtual_bench.TRANSPORT
    ep = endpoint.Endpoint(virtual_bench.PROFILES[name](), Clock())
    kinds = list(ep.transports.values())
    assert kinds.count(T["vendor_bulk"]) <= 1 and kinds.count(T["hid"]) <= 1
    for kind in ("vendor_bulk", "hid"):
        if T[kind] not in ep.transports.values():
            ep.add_transport(T[kind], interface=1)
        with pytest.raises(ValueError):
            ep.add_transport(T[kind], interface=2)
    kinds = [v[1] for t, v in ep.decl[0] if t == virtual_bench.CORE_TRANSPORT]
    assert kinds.count(T["vendor_bulk"]) == 1 and kinds.count(T["hid"]) == 1


def test_the_usb_model_is_one_vendor_bulk_or_one_hid_interface():
    """transports §3: the virtual bench's USB model adds its one vendor bulk (or HID) transport; a second of the same
    kind on that probe is refused."""
    ep = endpoint.Endpoint(virtual_bench.core_v1(), Clock())
    virtual_bench_usb.Usb(ep, "bulk")
    virtual_bench_usb.Usb(ep, "hid")
    for kind in ("bulk", "hid"):
        with pytest.raises(ValueError):
            virtual_bench_usb.Usb(ep, kind)


def test_each_accepted_tcp_connection_has_its_own_revision_and_credits():
    """transports §1 (the TCP transport): every connection accepted on the one listener reports its index in confirm's
    transport TLV, and the rules core applies per transport apply to each connection apart - the revision a confirm
    chose is recorded per connection (a confirm on one leaves the other's), and each connection has its own
    window / max_inflight credits (max_inflight queued on each at once, counted apart)."""
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    tcp = ep.add_transport(virtual_bench.TRANSPORT["tcp"])
    for corr, link in ((1, "a"), (2, "b")):
        r = m.Result.unpack(ep.handle(req(corr, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x01\x01"), tcp, link=link))
        assert r.succeeded and m.Tail.parse(r.payload[16:]).get(0x01) == bytes([tcp])
    assert ep.revision_in_use == {"a": 1, "b": 1}
    ep.handle(req(3, m.OP_CONFIRM, m.CONFIRM_REQUEST + b"\x02\x05"), tcp, link="b")       # chooses nothing
    assert ep.revision_in_use == {"a": 1, "b": 1}
    for link in ("a", "b"):
        for k in range(ep.max_inflight):
            assert ep.receive(req(10 + k, m.OP_CLOCK), (tcp, link)) == "queued"
    assert ep.credits() == {(tcp, "a"): ep.max_inflight, (tcp, "b"): ep.max_inflight}
    ep.process((tcp, "a"))
    ep.sent((tcp, "a"))
    assert ep.credits() == {(tcp, "a"): ep.max_inflight - 1, (tcp, "b"): ep.max_inflight}
