"""A serial port is always COBS with the probe's raw bytes on the same line (oep-core §3.1, §3.4; host guide §1.6, §2):
noise is skipped without a resend, a missing answer is sent once more by the timeout, the port is opened exclusively;
the same Host over TCP (a broker's length frames); taking the lock (host guide §2); the capture record hook."""

import os
import struct
import subprocess
import sys

import pytest

from oep_client import capture, cobs, core, host as h, link, message as m


class Scripted:
    """A pyserial-shaped stream: what the probe sends is queued in `rx`; what the host wrote collects in `tx`."""

    def __init__(self):
        self.rx, self.tx, self.timeout = bytearray(), bytearray(), 0.05
        self.on_write = None

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n=1):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.tx += data
        if self.on_write:
            self.on_write(bytes(data))
        return len(data)

    def reset_input_buffer(self):
        self.rx.clear()


def result(corr, payload=b"\x00" * 5):
    return m.Result(corr, m.COMPLETED, m.SUCCESS, payload).pack()


def test_noise_and_answers_to_other_requests_are_skipped_without_a_resend():
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", 0.3)
    s.rx += b"console says hi\r\n\x00zz" + cobs.frame(result(9)) + b"more console" + cobs.frame(result(7))
    reply = lk.send(m.Request(7, 0, m.OP_LOCK_STATE, b"").pack())
    assert m.Result.unpack(reply).corr == 7
    assert lk.noise > 0 and lk.stale == 1 and lk.retries == 0
    assert s.tx.startswith(b"\x00") and s.tx.endswith(b"\x00")      # the request framed on both sides


def test_a_missing_answer_goes_once_more_with_the_same_corr():
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", 0.1)
    sent = []

    def probe(data):
        sent.append(data)
        if len(sent) == 2:                                          # the first request's answer was lost
            s.rx += cobs.frame(result(5))
    s.on_write = probe
    assert m.Result.unpack(lk.send(m.Request(5, 1, 0x01, b"", session=1).pack())).corr == 5
    assert lk.retries == 1 and sent[0] == sent[1]


def serve(*args):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", *args],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    return proc, proc.stdout.readline().split()


@pytest.mark.skipif(sys.platform != "linux", reason="pty and TIOCEXCL as on Linux")
def test_open_host_on_a_serial_port_with_console_bytes_and_tiocexcl():
    proc, where = serve("--pty", "--profile", "esp32-v003", "--slot", "v003", "--bind", "last-reset",
                        "--console", "uptime %d\\r\\n", "--every", "5")
    try:
        hst = link.open_host(where[1], timeout=1.0)
        assert hst.limits["max_frame"] == 64
        if os.geteuid() != 0:
            with pytest.raises(link.PortBusy):
                link.open_host(where[1])                              # exclusive: one host at a time
        opened = core.take(hst, 3000, owner="test")                   # the only way in: by force at once
        assert opened.lease_ms == 3000
        assert [k for _, k, _ in core.transports(hst)] == [1]         # a UART bridge
        for _ in range(20):                                           # requests among the console bytes
            hst.keepalive()
        hst.end()
        assert hst.link.noise > 0 and hst.link.retries == 0         # console bytes before the session, all skipped
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_the_same_host_over_tcp_length_frames_and_a_holder_that_keeps_its_lease_is_named():
    proc, where = serve("--tcp", "0", "--framing", "length", "--profile", "p4-x035")
    try:
        a = link.open_host(f"tcp://127.0.0.1:{where[1]}", timeout=1.0)
        a.open(60000, owner="ch32rv monitor")
        locked, remaining, owner = a.lock_owner()                     # one read: the lease ticks down between two
        assert locked and 0 < remaining <= 60000 and owner == "ch32rv monitor"
        b = h.Host(a.link.send)                                       # another host on the same link (a broker's client)
        b.link = a.link
        b.revision = 1
        with pytest.raises(h.InUse, match="ch32rv monitor"):
            core.take(b, 3000, wait_s=0.2)                            # four transports: not by force
        assert core.take(b, 3000, force=True).lease_ms == 3000        # the user said so
        a.link.close()
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_every_segment_read_goes_to_the_record_hook():
    hst = h.Host(lambda b: b)
    hst._revisions[5] = 1
    cap = capture.LogicCapture(hst, fn=5)
    cap.config = capture.Config(width=8, positions=list(range(8)), samples=4)
    cap.read = lambda position, length: bytes(range(length))        # the probe's bytes
    records = []
    hst.on_capture.append(records.append)
    seg = capture.Segment(1, 0, 4, 1234, None, capture.SEGMENT_SLIPPED)
    assert cap.read_segment(seg) == b"\x00\x01\x02\x03"
    (r,) = records
    assert r.fn == 5 and r.data == b"\x00\x01\x02\x03" and r.segment.slipped and r.config.width == 8
