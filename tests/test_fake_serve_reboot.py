"""fake_serve's `reboot` line on stdin (Endpoint.reboot with a new boot_id, mid-session), and what a reboot resets."""

import os
import select
import socket
import struct
import subprocess
import sys
import time

import pytest

from oep_client import cobs, config, core, host as h, riscv

from test_config import open_bench


def _serve(*argv):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", *argv], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, proc.stdout.readline().decode().split()


def _stderr_line(proc, timeout=5.0) -> str:
    deadline = time.monotonic() + timeout
    line = b""
    while not line.endswith(b"\n"):
        left = deadline - time.monotonic()
        assert left > 0, f"no line on stderr (so far {line!r})"
        if select.select([proc.stderr], [], [], left)[0]:
            ch = os.read(proc.stderr.fileno(), 1)
            assert ch, "stderr closed"
            line += ch
    return line.decode().strip()


def _command(proc, text: str) -> str:
    proc.stdin.write(text.encode() + b"\n")
    proc.stdin.flush()
    return _stderr_line(proc)


def _tcp_send(sock):
    def send(message: bytes) -> bytes:
        sock.sendall(struct.pack("<H", len(message)) + message)
        head = b""
        while len(head) < 2:
            head += sock.recv(2 - len(head))
        n = struct.unpack("<H", head)[0]
        body = b""
        while len(body) < n:
            body += sock.recv(n - len(body))
        return body
    return send


def _pty_send(fd):
    buf = bytearray()

    def send(message: bytes) -> bytes:
        os.write(fd, cobs.frame(message))
        deadline = time.monotonic() + 5
        while True:
            while buf.count(0) >= 2:
                start = buf.index(0)
                end = buf.index(0, start + 1)
                body = bytes(buf[start + 1:end])
                del buf[:end]
                if body:
                    try:
                        return cobs.unframe(body)
                    except cobs.CorruptFrame:
                        pass
            assert time.monotonic() < deadline, "no answer on the pty"
            if select.select([fd], [], [], 0.1)[0]:
                buf.extend(os.read(fd, 4096))
    return send


def _reboot_mid_session(proc, send):
    """Open a session, reboot through stdin, and check what the host sees."""
    hst = h.Host(send)
    before = hst.confirm()["boot_id"]
    opened = hst.open(3000)
    assert opened.boot_id == before
    hst.keepalive()

    line = _command(proc, "reboot")
    assert line.startswith("fake_serve: rebooted, boot_id 0x")
    printed = int(line.rsplit("0x", 1)[1], 16)
    assert printed != before

    with pytest.raises(h.NoSession):                                 # the session table went with the reboot
        hst.keepalive()
    assert h.Host(send).confirm()["boot_id"] == printed              # confirm shows the new boot_id
    again = hst.open(3000)                                           # a new open works, with the new boot_id
    assert again.boot_id == printed and hst.session is not None
    hst.keepalive()

    assert "unknown command" in _command(proc, "frobnicate")       # ignored, the server goes on
    hst.keepalive()


def test_reboot_on_stdin_over_tcp_length_framing():
    proc, where = _serve("--tcp", "0", "--framing", "length", "--profile", "p4-x035")
    try:
        assert where[0] == "PORT"
        with socket.create_connection(("127.0.0.1", int(where[1])), timeout=5) as s:
            _reboot_mid_session(proc, _tcp_send(s))
    finally:
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0                           # stdin's end still ends it by default


@pytest.mark.skipif(sys.platform != "linux", reason="pty as on Linux")
def test_reboot_on_stdin_over_a_pty_and_keep_on_eof():
    proc, where = _serve("--pty", "--profile", "esp32-v003", "--keep-on-eof")
    try:
        assert where[0] == "PTY"
        fd = os.open(where[1], os.O_RDWR | os.O_NOCTTY)
        try:
            send = _pty_send(fd)
            _reboot_mid_session(proc, send)
            proc.stdin.close()                                       # --keep-on-eof: the probe goes on serving
            time.sleep(0.2)
            assert proc.poll() is None
            assert h.Host(send).confirm()["boot_id"] is not None
        finally:
            os.close(fd)
    finally:
        if not proc.stdin.closed:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)


def test_reboot_resets_what_a_probe_loses_and_applies_the_saved_settings_again():
    ep, hst = open_bench()
    ep.capture_slipped = True                                        # the simulation's own knobs stay
    ep.uart_clock_hz = 1_000_000
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=21, mode="pull-down")])
    cfg.save()
    cfg.set([config.Disable(channel=20)])                            # not saved
    core.plan_apply(hst, [(4, 1, 30)])
    riscv.Wire(hst, "oep.wire.rvswd").attach(pins=(4, 5))
    ep.subscribed[4] = (0, 0)                                        # (p4-bench has nothing that emits: a stand-in)
    hst.keepalive()
    saved = dict(ep.saved)
    assert ep.conns and ep.plan and ep.resend and ep.subscribed and ep.holder is not None and ep.disabled == {20}
    assert ep.revision_in_use

    old = ep.boot_id
    ep.reboot()
    assert ep.boot_id != old
    assert ep.holder is None and ep.last is None                      # the session table
    assert ep.conns == {} and ep.streams == {} and ep.resources == {}
    assert ep.plan == set() and ep.resend == {} and ep.newest_corr is None
    assert ep.subscribed == {} and ep.outbox == []
    assert ep.revision_in_use == {} and ep.speed_state == "base"
    assert ep.disabled == set() and ep.config == saved and ep.parked[21] == 2   # the saved settings, applied again
    assert ep.capture_slipped and ep.uart_clock_hz == 1_000_000

    with pytest.raises(h.NoSession):
        hst.keepalive()
    hst.open(3000)
    assert config.ProbeConfig(hst).items() == [config.Idle(channel=21, mode="pull-down")]
