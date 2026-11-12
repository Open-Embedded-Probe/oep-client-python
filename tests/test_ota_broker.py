"""ota_broker: espota's uploader side against the broker, the writer a fake (no probe)."""

from __future__ import annotations

import glob
import hashlib
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from oep_client import ota_broker as ob


def upload(port: int, payload: bytes, piece: int = 1460, command: int = ob.FLASH) -> tuple[bytes, bytes]:
    """espota.py's steps (the uploader): -> (the UDP answer, everything read after the last piece)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(5)
    msg = f"{command} {srv.getsockname()[1]} {len(payload)} {hashlib.md5(payload).hexdigest()}\n"
    u.sendto(msg.encode(), ("127.0.0.1", port))
    answer = u.recv(128)
    u.close()
    if answer != b"OK":
        srv.close()
        return answer, b""
    srv.settimeout(5)
    conn, _ = srv.accept()
    with conn:
        conn.settimeout(10)
        for at in range(0, len(payload), piece):
            conn.sendall(payload[at:at + piece])
            assert conn.recv(32)
        tail = b""
        while b"O" not in tail and b"E" not in tail:
            got = conn.recv(64)
            if not got:
                break
            tail += got
    srv.close()
    return answer, tail


@pytest.fixture
def broker_with():
    made = []

    def make(writer, board="rpipico"):
        t = ob.Target(address="oep://fafe00000001/pico", board=board, writer=writer)
        b = ob.Broker([t], announce=False, log=lambda m: None)
        th = threading.Thread(target=b.serve, daemon=True)
        th.start()
        made.append((b, th))
        return t
    yield make
    for b, th in made:
        b.close()
        th.join(2)


def test_parse_invitation():
    inv = ob.parse_invitation(b"0 4321 100 " + b"a" * 32 + b"\n")
    assert inv == ob.Invitation(0, 4321, 100, "a" * 32)
    assert ob.parse_invitation(b"0 0 100 " + b"a" * 32) is None
    assert ob.parse_invitation(b"0 4321 100 abc") is None
    assert ob.parse_invitation(b"OK") is None
    assert ob.parse_invitation(b"\xff\xfe") is None


def test_names_and_writers():
    assert ob.instance_name("oep://80f1b2d835d6/pico") == "oep-80f1b2d835d6-pico"
    assert ob.instance_name("oep://80f1b2d835d6/x035.pico") == "oep-80f1b2d835d6-x035-pico"
    assert ob.board_family("rpipico") == "rp2040" and ob.board_family("rpipico2") == "rp2350"
    for board in ("esp32", "rpipico2"):
        with pytest.raises(ValueError):
            ob.writer_for(board)


def test_upload_written(broker_with):
    got = {}

    def writer(address, path, log):
        got["address"], got["data"] = address, open(path, "rb").read()
    t = broker_with(writer)
    payload = os.urandom(5000)
    answer, tail = upload(t.udp_port, payload)
    assert answer == b"OK" and b"OK" in tail
    assert got == {"address": "oep://fafe00000001/pico", "data": payload}


def test_write_failure_is_an_error(broker_with):
    def writer(address, path, log):
        raise RuntimeError("no target")
    t = broker_with(writer)
    answer, tail = upload(t.udp_port, b"x" * 100)
    assert answer == b"OK" and tail.startswith(b"ERROR") and b"no target" in tail


def test_slow_write_keeps_the_uploader_waiting(broker_with, monkeypatch):
    monkeypatch.setattr(ob, "KEEPALIVE", 0.1)
    def writer(address, path, log):
        time.sleep(0.5)
    t = broker_with(writer)
    answer, tail = upload(t.udp_port, b"y" * 3000)
    assert answer == b"OK" and tail.endswith(b"OK") and tail.count(b"0") >= 2


def test_md5_mismatch_and_other_commands(broker_with):
    t = broker_with(lambda a, p, l: None)
    assert upload(t.udp_port, b"z", command=ob.FILESYSTEM)[0].startswith(b"ERR")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(5)
    u.sendto(f"0 {srv.getsockname()[1]} 4 {'0' * 32}\n".encode(), ("127.0.0.1", t.udp_port))
    assert u.recv(32) == b"OK"
    srv.settimeout(5)
    conn, _ = srv.accept()
    conn.settimeout(5)
    conn.sendall(b"abcd")
    tail = b""
    while b"E" not in tail:
        tail += conn.recv(64)
    assert b"MD5" in tail
    conn.close(), srv.close(), u.close()


def _core_espota() -> str | None:
    found = sorted(glob.glob(os.path.expanduser("~/.arduino15/internal/rp2040_rp2040_*/tools/espota.py")) +
                   glob.glob(os.path.expanduser("~/.arduino15/packages/rp2040/hardware/rp2040/*/tools/espota.py")))
    return found[-1] if found else None


@pytest.mark.skipif(_core_espota() is None, reason="arduino-pico's espota.py is not installed here")
def test_the_cores_espota(broker_with, tmp_path):
    """arduino-pico's own tools/espota.py, as its uf2conv-network upload pattern runs it."""
    got = {}
    t = broker_with(lambda a, p, l: got.setdefault("data", open(p, "rb").read()))
    image = tmp_path / "sketch.ino.bin"
    image.write_bytes(os.urandom(20000))
    r = subprocess.run([sys.executable, "-I", _core_espota(), "-i", "127.0.0.1", "-p", str(t.udp_port), "--auth=",
                        "-f", str(image)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert got["data"] == image.read_bytes()
