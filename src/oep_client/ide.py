"""Arduino's network upload, received for a board on an OEP probe: the IDE's Upload writes it over the probe.

The Arduino IDE (arduino-cli) lists a network port for each `_arduino._tcp` service it finds over mDNS, and a core's
network upload (`upload.tool.network`: arduino-pico's uf2conv-network, arduino-esp32's esp_ota) runs the core's own
`espota.py` against it. This server announces one such service per OEP port and answers espota as a board's ArduinoOTA
would; what it receives it writes through the probe. Nothing of the core is changed (no platform.local.txt): pick the
server's network port in the IDE and press Upload.

    python -m oep_client.ide oep://<probe>/<port>[=<board>] ...

- `<board>` is the board id put in the TXT `board` (the IDE shows it; default rpipico). The writer is chosen by the
  board's family (`writer_for`): RP2040 boards are written over SWD by `rp2040_swd.flash`.
- One UDP port per target (`--udp-port` the first, the rest after it; 0: any free one), announced as the service's
  port. The TXT says auth_upload=no, tcp_check=no, ssh_upload=no: espota sends no password.

espota's protocol (the core's tools/espota.py, the uploader's side):
1. UDP to the service's port: `<command> <tcp port> <size> <md5>\\n` (command 0 = flash; 100 = filesystem, 200 =
   auth reply). The board answers `OK` (or `AUTH <nonce>`).
2. The board connects to the uploader's TCP port (10 s). The uploader sends the file in pieces of 1460 bytes and,
   after each, waits up to 10 s for an answer (the board sends the count received as digits).
3. After the last piece the uploader reads until it sees `O` (OK) or `E` (ERROR), each read waiting up to 60 s.
   Digits are neither: the server sends one every few seconds while the probe writes, so a write longer than a
   minute still ends in its OK.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import select
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

SERVICE = "_arduino._tcp.local."
FLASH, FILESYSTEM, AUTH = 0, 100, 200
CONNECT_TIMEOUT = 10.0      # espota accepts the board's connection for 10 s
PIECE_TIMEOUT = 10.0        # and waits as long for each piece's answer
KEEPALIVE = 5.0             # a digit this often while writing (espota's read after the last piece waits 60 s)

Writer = Callable[[str, str, Callable[[str], None]], None]      # (address, image path, log)


@dataclass
class Invitation:
    command: int
    port: int
    size: int
    md5: str


def parse_invitation(data: bytes) -> Invitation | None:
    """espota's first datagram `<command> <tcp port> <size> <md5>\\n` -> Invitation; None when it is not one."""
    try:
        parts = data.decode("ascii").split()
        if len(parts) != 4:
            return None
        command, port, size = int(parts[0]), int(parts[1]), int(parts[2])
    except (UnicodeDecodeError, ValueError):
        return None
    if not 0 < port < 65536 or size < 0 or len(parts[3]) != 32:
        return None
    return Invitation(command, port, size, parts[3].lower())


def board_family(board: str) -> str:
    b = board.lower()
    if "2350" in b or b.startswith("rpipico2"):
        return "rp2350"
    if b.startswith(("rpipico", "rp2040", "generic")) or "2040" in b:
        return "rp2040"
    return ""


def writer_for(board: str) -> Writer:
    """The writer for a board id: RP2040 boards over SWD (rp2040_swd.flash, the RP2040's core 0 and boot ROM)."""
    family = board_family(board)
    if family == "rp2040":
        def write(address: str, path: str, log: Callable[[str], None]) -> None:
            from . import rp2040_swd
            rp2040_swd.flash(address, path, log=log)
        return write
    raise ValueError(f"board {board!r}: no writer for it (RP2040 boards only)")


def instance_name(address: str) -> str:
    """oep://<probe>/<port> -> oep-<probe>-<port>, a name the IDE shows ("<name> at <ip>")."""
    from .rp2040_swd import parse
    unit, port = parse(address)
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in port.lower())
    return f"oep-{unit}-{safe}"


@dataclass
class Target:
    address: str
    board: str = "rpipico"
    name: str = ""
    udp_port: int = 0
    writer: Writer | None = None
    busy: threading.Lock = field(default_factory=threading.Lock)
    sock: socket.socket | None = None
    gdb_port: int = 0                 # a GDB server for the target on this TCP port (0: none)
    debug: object | None = None       # the gdb session's open target while one is connected

    def __post_init__(self) -> None:
        if not self.name:
            self.name = instance_name(self.address)
        if self.writer is None:
            self.writer = writer_for(self.board)


def receive(target: Target, inv: Invitation, peer: str, log: Callable[[str], None]) -> bool:
    """The board's side of one espota upload, after the invitation was answered OK: connect back, take the file,
    check its MD5, write it, answer OK or ERROR. -> whether it was written."""
    try:
        conn = socket.create_connection((peer, inv.port), timeout=CONNECT_TIMEOUT)
    except OSError as e:
        log(f"{target.name}: cannot connect back to {peer}:{inv.port} ({e})")
        return False
    with conn:
        conn.settimeout(PIECE_TIMEOUT * 3)
        data = bytearray()
        try:
            while len(data) < inv.size:
                piece = conn.recv(min(65536, inv.size - len(data)))
                if not piece:
                    log(f"{target.name}: the uploader closed after {len(data)} of {inv.size} B")
                    return False
                data += piece
                conn.sendall(str(len(piece)).encode())
        except OSError as e:
            log(f"{target.name}: receive failed after {len(data)} of {inv.size} B ({e})")
            return False
        if hashlib.md5(data).hexdigest() != inv.md5:
            log(f"{target.name}: MD5 mismatch")
            _send(conn, b"ERROR: MD5 mismatch")
            return False
        log(f"{target.name}: {len(data)} B received, writing to {target.address}")
        fd, path = tempfile.mkstemp(prefix="oep-ota-", suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            outcome: list[BaseException | None] = []

            def run() -> None:
                try:
                    if target.debug is not None:          # gdb holds the probe: write within its session
                        target.debug.upload(open(path, "rb").read(), log)
                    else:
                        target.writer(target.address, path, log)
                    outcome.append(None)
                except BaseException as e:      # reported to the uploader, not raised in a thread
                    outcome.append(e)
            t = threading.Thread(target=run, name=f"ota-write-{target.name}", daemon=True)
            t.start()
            while t.is_alive():
                t.join(KEEPALIVE)
                if t.is_alive():
                    _send(conn, b"0")
            err = outcome[0] if outcome else RuntimeError("the writer ended without a result")
        finally:
            os.unlink(path)
        if err is not None:
            log(f"{target.name}: write failed: {err}")
            _send(conn, f"ERROR: {err}".encode()[:256])
            return False
        log(f"{target.name}: written")
        _send(conn, b"OK")
        try:                                     # let espota read OK before the close
            conn.shutdown(socket.SHUT_WR)
            conn.settimeout(2.0)
            conn.recv(16)
        except OSError:
            pass
        return True


def _send(conn: socket.socket, data: bytes) -> None:
    try:
        conn.sendall(data)
    except OSError:
        pass


class Server:
    """The UDP ports and the mDNS announcements of the targets; `serve()` answers invitations until `stop()`."""

    def __init__(self, targets: list[Target], interfaces: list[str] | None = None, engine: str = "auto",
                 announce: bool = True, log: Callable[[str], None] = print):
        self.targets, self.log = targets, log
        self.responders = []
        self._stop = threading.Event()
        for t in targets:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("", t.udp_port))
            t.udp_port = s.getsockname()[1]
            s.setblocking(False)
            t.sock = s
        if announce:
            from . import virtual_bench_mdns as vm
            from .discovery import interface_addresses
            addrs = interfaces if interfaces is not None else interface_addresses()
            # One address, a LAN one when there is one: the IDE lists a port by the address it took from the A
            # records, and a host with two (loopback and the LAN, or two LAN addresses) gave it a different one from
            # one look to the next - a different port each time. --interface picks it.
            addrs = ([ip for ip in addrs if not ip.startswith("127.")] or list(addrs))[:1]
            for t in targets:
                ann = vm.Announcement(unit_id=t.name, port=t.udp_port, addresses=addrs, instance=t.name,
                                      host=f"{t.name}.local.", service=SERVICE,
                                      properties={"board": t.board, "auth_upload": "no", "tcp_check": "no",
                                                  "ssh_upload": "no"})
                self.responders.append(vm.start(ann, engine=engine, interfaces=interfaces))

        self.gdb_servers = []
        for t in targets:
            if t.gdb_port and board_family(t.board) == "rp2040":
                from . import gdb_server
                srv = gdb_server.Server(self._debug_maker(t), port=t.gdb_port, bind="127.0.0.1", log=log)
                t.gdb_port = srv.port
                self.gdb_servers.append(srv)
                threading.Thread(target=srv.serve_forever, args=(self._stop,), daemon=True,
                                 name=f"gdb-{t.name}").start()

    def _debug_maker(self, t: Target):
        from . import gdb_server

        class Debug(gdb_server.Rp2040Debug):
            def open(self) -> None:
                super().open()
                t.debug = self

            def close(self) -> None:
                t.debug = None
                super().close()
        return lambda: Debug(t.address, owner=f"gdb {t.name}")

    def serve(self) -> None:
        socks = {t.sock: t for t in self.targets}
        while not self._stop.is_set():
            watch = list(socks) + [w for r in self.responders for w in r.watch()]
            readable, _, _ = select.select(watch, [], [], 0.5)
            for r in self.responders:
                r.poll(readable)
            for s in readable:
                t = socks.get(s)
                if t is not None:
                    self._invitation(t)

    def _invitation(self, t: Target) -> None:
        try:
            data, (peer, peer_port) = t.sock.recvfrom(512)
        except OSError:
            return
        inv = parse_invitation(data)
        if inv is None:
            return
        if inv.command != FLASH:
            self.log(f"{t.name}: command {inv.command} from {peer} refused (only a flash upload)")
            t.sock.sendto(b"ERR: only a flash upload", (peer, peer_port))
            return
        if not t.busy.acquire(blocking=False):
            t.sock.sendto(b"ERR: busy", (peer, peer_port))
            return
        t.sock.sendto(b"OK", (peer, peer_port))
        self.log(f"{t.name}: upload of {inv.size} B from {peer}")

        def run() -> None:
            try:
                receive(t, inv, peer, self.log)
            finally:
                t.busy.release()
        threading.Thread(target=run, name=f"ota-{t.name}", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.stop()
        for g in getattr(self, "gdb_servers", []):
            g.close()
        for r in self.responders:
            try:
                r.close()
            except OSError:
                pass
        for t in self.targets:
            if t.sock is not None:
                t.sock.close()


def parse_target(text: str) -> Target:
    """`oep://<probe>/<port>[=<board>]` -> Target."""
    address, _, board = text.partition("=")
    return Target(address=address, board=board or "rpipico")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m oep_client.ide",
                                 description="Answer Arduino's network upload for boards on OEP probes.")
    ap.add_argument("targets", nargs="+", metavar="oep://PROBE/PORT[=BOARD]",
                    help="an OEP port and the board id the IDE should show (default rpipico)")
    ap.add_argument("--udp-port", type=int, default=0, help="the first target's UDP port (0: any free one)")
    ap.add_argument("--interface", action="append", help="announce on this IPv4 address only (repeatable)")
    ap.add_argument("--engine", choices=["auto", "zeroconf", "minimal"], default="auto")
    ap.add_argument("--dry-run", action="store_true", help="receive and check each upload, write nothing")
    ap.add_argument("--gdb-port", type=int, default=0,
                    help="a GDB server on 127.0.0.1 for the first target on this port, the rest after it (0: none)")
    args = ap.parse_args(argv)
    targets = [parse_target(t) for t in args.targets]
    if args.dry_run:
        for t in targets:
            t.writer = lambda address, path, log: log(f"dry run: {os.path.getsize(path)} B not written to {address}")
    for i, t in enumerate(targets):
        if args.udp_port:
            t.udp_port = args.udp_port + i
        if args.gdb_port:
            t.gdb_port = args.gdb_port + i

    def log(msg: str) -> None:
        print(time.strftime("%H:%M:%S"), msg, flush=True)
    server = Server(targets, interfaces=args.interface, engine=args.engine, log=log)
    for t in targets:
        log(f"{t.name}: {t.address} as {t.board}, UDP {t.udp_port}" + (f", gdb 127.0.0.1:{t.gdb_port}"
                                                                         if t.gdb_port else ""))
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
