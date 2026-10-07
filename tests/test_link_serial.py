"""A serial port is always COBS with the probe's raw bytes on the same line (transports §1, §4; host guide §2, §6):
noise is skipped without a resend, a missing answer is sent once more by the timeout, a broken answer on a held port
at once (host guide §8), the port is opened exclusively;
the same Host over TCP (a broker's length frames); taking the lock (host guide §6); the capture record hook."""

import os
import struct
import subprocess
import sys
import time

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


# ---- a broken frame on a held serial port (host guide §8, core §5.2) --------------------------------------------------

def broken(corr):
    """The answer to `corr` as the line broke it: framed, its CRC spoiled."""
    from oep_client import virtual_bench_serial
    return virtual_bench_serial._spoil(cobs.frame(result(corr)))


def held_link(answers, timeout=2.0, held=True):
    """A serial link whose port a session holds (`held`), the probe answering the n-th write (0-based) with
    `answers[n]`: "broken" (the answer, CRC spoiled), "good", or None (nothing). -> (link, the writes)."""
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", timeout)
    lk.held = lambda: held
    sent = []

    def probe(data):
        sent.append(data)
        corr = m.Request.unpack(cobs.unframe(data[1:-1])).corr
        what = answers[len(sent) - 1] if len(sent) <= len(answers) else None
        if what == "broken":
            s.rx += broken(corr)
        elif what == "good":
            s.rx += cobs.frame(result(corr))
    s.on_write = probe
    return lk, sent


def test_a_broken_answer_on_a_held_port_is_resent_at_once_with_the_same_corr():
    """Host guide §8: a CRC that does not match while an answer is awaited is the lost answer - the request goes again
    at once with the same corr (the probe answers it from its resend table), not after the wait; one broken frame does
    not fail the transport."""
    lk, sent = held_link(["broken", "good"])
    t0 = time.monotonic()
    reply = lk.send(m.Request(5, 1, 0x01, b"", session=1).pack())
    assert m.Result.unpack(reply).corr == 5 and time.monotonic() - t0 < 0.5       # the wait is 2 s
    assert len(sent) == 2 and sent[0] == sent[1] and lk.retries == 1 and lk.corrupt == 1
    assert not lk.failed_transport


def test_broken_answers_are_resent_up_to_broken_resends_times_then_the_transport_failed():
    assert link.BROKEN_RESENDS == 3
    lk, sent = held_link(["broken"] * link.BROKEN_RESENDS + ["good"])           # frames keep arriving: 3 resends
    t0 = time.monotonic()
    assert m.Result.unpack(lk.send(m.Request(6, 1, 0x01, b"", session=1).pack())).corr == 6
    assert len(sent) == 1 + link.BROKEN_RESENDS and len(set(sent)) == 1 and time.monotonic() - t0 < 0.5
    assert not lk.failed_transport
    lk, sent = held_link(["broken"] * (link.BROKEN_RESENDS + 1))                 # one more broken: failed
    t0 = time.monotonic()
    with pytest.raises(link.TransportFailed):
        lk.send(m.Request(7, 1, 0x01, b"", session=1).pack())
    assert len(sent) == 1 + link.BROKEN_RESENDS and len(set(sent)) == 1 and time.monotonic() - t0 < 0.5
    assert lk.failed_transport                                                 # recovered before the next request


def test_a_wait_with_nothing_allows_the_one_resend_of_core_5_2():
    lk, sent = held_link([None, None], timeout=0.2)
    t0 = time.monotonic()
    with pytest.raises(link.TransportFailed):
        lk.send(m.Request(8, 1, 0x01, b"", session=1).pack())
    assert len(sent) == 2 and sent[0] == sent[1] and time.monotonic() - t0 >= 0.4   # both waits, one resend
    lk, sent = held_link(["broken", None, None], timeout=0.2)                  # a broken frame, then silence: the
    with pytest.raises(link.TransportFailed):                                  # silence still gets its one resend
        lk.send(m.Request(9, 1, 0x01, b"", session=1).pack())
    assert len(sent) == 3 and len(set(sent)) == 1


def test_a_broken_frame_on_a_port_no_session_holds_is_noise_and_waits():
    """Not held: the probe's raw bytes share the line (transports §4), so a broken candidate is skipped without a
    resend; the answer's loss shows by the wait only (core §5.2)."""
    lk, sent = held_link(["broken", "good"], timeout=0.2, held=False)
    t0 = time.monotonic()
    assert m.Result.unpack(lk.send(m.Request(10, 1, 0x01, b"").pack())).corr == 10
    assert len(sent) == 2 and time.monotonic() - t0 >= 0.2 and lk.corrupt == 0 and lk.noise > 0


def test_pipelined_requests_resend_the_unanswered_ones_after_a_broken_frame():
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", 2.0)
    lk.held = lambda: True
    writes = []

    def probe(data):
        writes.append(data)
        corrs = [m.Request.unpack(cobs.unframe(f)).corr for f in data.split(b"\x00") if f]
        for c in corrs:
            s.rx += broken(c) if (c == 12 and len(writes) == 1) else cobs.frame(result(c))
    s.on_write = probe
    msgs = [m.Request(c, 1, 0x01, b"", session=1).pack() for c in (11, 12, 13)]
    t0 = time.monotonic()
    replies = lk.exchange(msgs, 3, 4096)
    assert [m.Result.unpack(r).corr for r in replies] == [11, 12, 13] and time.monotonic() - t0 < 0.5
    assert lk.retries == 1 and not lk.failed_transport


def serve(*args):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", *args],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    return proc, proc.stdout.readline().split()


@pytest.mark.skipif(sys.platform != "linux", reason="pty and TIOCEXCL as on Linux")
def test_open_host_on_a_serial_port_with_console_bytes_and_tiocexcl():
    proc, where = serve("--pty", "--profile", "esp32-v003", "--slot", "v003", "--bind", "0",
                        "--console", "uptime %d\\r\\n", "--every", "5")
    try:
        hst = link.open_host(where[1], timeout=1.0)
        assert hst.limits["max_frame"] == 512
        if os.geteuid() != 0:
            with pytest.raises(link.PortBusy):
                link.open_host(where[1])                              # exclusive: one host at a time
        deadline = time.monotonic() + 3                               # lock-free reads among the console bytes,
        while hst.link.noise == 0 and time.monotonic() < deadline:   # before a session holds the port's raw side
            assert [k for _, k, _ in core.transports(hst)] == [1]     # a UART bridge
            time.sleep(0.02)
        opened = core.take(hst, 3000, owner="test")                   # the only way in: by force at once
        assert opened.lease_ms == 3000
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
    hst._revisions[5], hst._describes[5] = 1, []
    cap = capture.LogicCapture(hst, fn=5)
    cap.config = capture.Config(width=8, positions=list(range(8)), samples=4)
    cap.read = lambda position, length, generation=None: bytes(range(length))   # the probe's bytes
    records = []
    hst.on_capture.append(records.append)
    seg = capture.Segment(1, 0, 4, 1234000, 50, None, capture.SEGMENT_SLIPPED, 2)
    assert cap.read_segment(seg) == b"\x00\x01\x02\x03"
    (r,) = records
    assert r.fn == 5 and r.data == b"\x00\x01\x02\x03" and r.segment.slipped and r.config.width == 8


_END_WITHOUT_CLOSE = """
import os, sys
from oep_client import core, link
hst = link.open_host(sys.argv[1], timeout=1.0)
core.take(hst, 3000, owner="child")
hst.end()                                  # the session ends; the link is never closed
if sys.argv[2] == "hard":
    os._exit(0)                            # no atexit: only the virtual bench can let the port go
"""


@pytest.mark.skipif(sys.platform != "linux", reason="pty and TIOCEXCL as on Linux")
@pytest.mark.parametrize("exit_mode", ["normal", "hard"])
def test_a_host_that_ends_without_closing_leaves_the_pty_free_for_the_next(exit_mode):
    """A pty slave's tty lives on while virtual_bench_serve holds the master, so a TIOCEXCL left set refused every later open
    (EBUSY) - a real port clears it at its last close. Now the client clears it at exit, and virtual_bench_serve at the last
    close of its slave (also after an exit that skipped atexit)."""
    proc, where = serve("--pty", "--profile", "esp32-v003")
    try:
        for _ in range(2):
            child = subprocess.run([sys.executable, "-c", _END_WITHOUT_CLOSE, where[1], exit_mode],
                                   capture_output=True, text=True, timeout=30)
            assert child.returncode == 0, child.stderr               # the second child: the next process
        deadline = time.monotonic() + (1.0 if exit_mode == "hard" else 0)
        while True:                                                   # the virtual bench sees the hard exit's close a few
            try:                                                      # ms later; the client's own exit at once
                hst = link.open_host(where[1], timeout=1.0)
                break
            except link.PortBusy:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        core.take(hst, 3000, owner="test")
        hst.end()
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="pty and TIOCEXCL as on Linux")
def test_a_port_left_open_at_exit_has_its_exclusive_mode_cleared():
    """On a bare pty (nobody clears the flag for it) a program that opened the port and ended without closing it
    leaves it openable: open_serial's ports get TIOCNXCL and close at interpreter exit."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    try:
        child = subprocess.run([sys.executable, "-c", "import sys; from oep_client import link; "
                                "link.open_serial(sys.argv[1])", path], capture_output=True, text=True, timeout=30)
        assert child.returncode == 0, child.stderr
        if os.geteuid() != 0:
            os.close(os.open(path, os.O_RDWR | os.O_NOCTTY))        # was EBUSY: the flag outlived the process
    finally:
        os.close(master)
