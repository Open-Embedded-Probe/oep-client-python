"""Serve a fake probe (`endpoint.Endpoint`) on a pty or a TCP port, for other programs' tests.

    python -m oep_client.fake_serve [--pty | --tcp PORT] [options]

--pty (the default) opens a pseudo terminal that is the probe's serial port (oep-core §3.4): COBS frames
0x00 <COBS> 0x00 and the raw bytes of the port's bind on one line. The host opens the printed path itself (and
should set TIOCEXCL on it, as on a real port; this program never opens the slave side). --tcp PORT (0 = any free
one) serves one connection at a time: --framing cobs is the serial port again, --framing length is
length(u16) message as on vendor bulk / TCP (no raw bytes).

The first line on stdout says where to open: `PTY /dev/pts/N` or `PORT n`. The program ends when stdin closes
(so a test's child never stays behind), or with --once when the first TCP connection closes.

Options:
  --profile NAME        p4-x035 (default), esp32-v003, p4-bench or rp2350-pins (p4_x035 style names work too)
  --port-index N        which serial port of the profile the pty / cobs TCP is (default: the first one)
  --noise TEXT          raw bytes written in front of every answer (the host must skip them)
  --drop N              the N-th answer (1-based) is not sent, once (the request did run: a resend gets the
                        remembered result)
  --corrupt N           the N-th answer goes out once with a broken CRC
  --console FMT         what the targets write to their consoles, %d = a counter, {t} = the target's index
  --every MS            how often (default 100; "100ms" works too)
  --slot NAME           register a slot at boot (repeatable; the n-th on the n-th pin pair of the first wire, at boot,
                        retry 1 s, mechanism dmseq), as if saved
  --bind MODE           bind the serial port to every --slot: last-reset, manual or mixed
  --target-id HEX       the target_id every target's attach reports (wch_dmi_7f)
  --absent N            the N-th pin pair of the first wire has no target (repeatable)
  --capture-slipped     every oep.fixture.capture segment says flags bit2 (slipped: a pace that fell behind)
  --uart-plan           the first oep.fixture.uart gets its RX / TX plan at boot, as if saved (the jig's "DUT TX" /
                        "DUT RX" labels when the profile has them, else the first free channels); configure then works
  --uart-rx TEXT        what arrives on that UART's RX every --every ms once it is configured (%d = a counter)
  --run-hook SPEC       what riscv-dm run does on every target: SPEC is module:function or path/file.py:function,
                        called as function(target, pc, regs) -> (stopped, dpc, elapsed_us). `target` is the
                        endpoint.FakeTarget (mem = word address -> value, regs = regno -> value, halted, dpc), so a
                        host's loader can be played by the host's own test code (the fake knows no loader).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import re
import select
import socket
import struct
import sys
import time
import tty

from . import catalog, endpoint, fake, fake_serial, registry as reg

_ITEM = reg.PROBE_CONFIG.tlv["item"]
_MODES = reg.PROBE_CONFIG.enum["bind_mode"]


def _ms(text: str) -> int:
    return int(re.fullmatch(r"(\d+)\s*(ms)?", text.strip()).group(1))


def build(a: argparse.Namespace) -> endpoint.Endpoint:
    profile = fake.PROFILES.get(a.profile) or fake.PROFILES[a.profile.replace("_", "-")]
    start = time.monotonic()
    ep = endpoint.Endpoint(profile(), lambda: int((time.monotonic() - start) * 1000))
    wire = min(ep.pairs) if ep.pairs else None
    if a.target_id is not None:
        for tg in ep.targets.values():
            tg.target_id = int(a.target_id, 16)
    if a.run_hook:
        hook = _load_hook(a.run_hook)
        for tg in ep.targets.values():
            tg.run_hook = (lambda t: lambda pc, regs: hook(t, pc, regs))(tg)
    ep.capture_slipped = getattr(a, "capture_slipped", False)
    for n in a.absent:
        ep.targets[(wire, ep.pairs[wire][n])].present = False
    items = []
    for n, name in enumerate(a.slot):
        swdio, swclk = ep.pairs[wire][n]
        mech = 2 if 2 in ep.mechanisms else min(ep.mechanisms)
        raw = name.encode()
        value = struct.pack("<BHHHBHIBBB", n, wire, swdio, swclk, reg.PROBE_CONFIG.enum["slot_attach"]["at_boot"], 1,
                            0, 0, mech, len(raw)) + raw + b"\x00"       # no speed ceiling, rests high, no lock
        items.append(bytes([_ITEM["slot"], len(value)]) + value)
    if a.bind:
        streams = b"".join(struct.pack("<BH", reg.PROBE_CONFIG.enum["bind_stream"]["slot_console"], n)
                           for n in range(len(a.slot)))
        value = struct.pack("<BBBB", a.port_index, _MODES[a.bind.replace("-", "_")], 0, len(a.slot)) + streams
        items.append(bytes([_ITEM["bind"], len(value)]) + value)
    if a.uart_plan:
        fn = uart_fn(ep)
        rx, tx = _uart_channels(ep, fn)
        for role, ch in ((1, rx), (2, tx)):
            items.append(bytes([_ITEM["plan"], 5]) + struct.pack("<HBH", fn, role, ch))
    if items:
        ep.load_config(items, saved=True)
    return ep


def uart_fn(ep: endpoint.Endpoint) -> int:
    fn = ep.fns.get("oep.fixture.uart")
    if fn is None:
        raise SystemExit("this profile offers no oep.fixture.uart")
    return fn


def _uart_channels(ep: endpoint.Endpoint, fn: int) -> tuple[int, int]:
    """RX, TX for the UART's plan: the jig's DUT TX (probe RX) / DUT RX (probe TX) labels, else the first two free."""
    labels = {name: ch for ch, name in ep.static_labels.items()}
    if "DUT TX" in labels and "DUT RX" in labels:
        return labels["DUT TX"], labels["DUT RX"]
    allowed = []
    for t in ep.static[fn]:
        if t[0] == catalog.ROLE_CHANNELS and t[2] == 1:
            base = struct.unpack_from("<H", t, 3)[0]
            allowed = catalog.bitmap_to_channels(base, t[5:2 + t[1]])
    taken = {p for s in ep.slots.values() for p in s.pair} | {a[2] for a in ep.plan}
    free = [ch for ch in allowed if ch not in taken]
    if len(free) < 2:
        raise SystemExit("no two free channels for the UART's plan")
    return free[0], free[1]


def _load_hook(spec: str):
    where, _, name = spec.rpartition(":")
    if not where or not name:
        raise SystemExit(f"--run-hook {spec}: want module:function or file.py:function")
    if where.endswith(".py") or os.sep in where:
        loader = importlib.util.spec_from_file_location("fake_run_hook", where)
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
    else:
        module = importlib.import_module(where)
    return getattr(module, name)


def _filter(a: argparse.Namespace):
    done = set()

    def answer(n: int, result: bytes) -> bytes | None:
        from . import cobs
        if n == a.drop and "drop" not in done:
            done.add("drop")
            return None
        wire = cobs.frame(result)
        if n == a.corrupt and "corrupt" not in done:
            done.add("corrupt")
            body = bytearray(cobs.decode(wire[1:-1]))
            body[-1] ^= 0xFF                             # the CRC's high byte
            wire = b"\x00" + cobs.encode(bytes(body)) + b"\x00"
        return a.noise.encode() + wire if a.noise else wire
    return answer


def _text(fmt: str, count: int) -> bytes:
    text = fmt.replace("%d", str(count)) if "%d" in fmt else fmt
    return text.encode().decode("unicode_escape").encode("latin-1")


class Console:
    """Every `every_ms`: the targets write `fmt` to their consoles, and `uart_rx` arrives on the UART's RX (fn
    `uart`) while it runs (configured)."""

    def __init__(self, ep: endpoint.Endpoint, fmt: str | None, every_ms: int, uart_rx: str | None = None,
                 uart: int | None = None):
        self.ep, self.fmt, self.every, self.uart_rx, self.uart = ep, fmt, every_ms, uart_rx, uart
        self.next_ms, self.count = 0, 0

    def tick(self) -> None:
        if not (self.fmt or self.uart_rx) or self.ep.now() < self.next_ms:
            return
        self.next_ms = self.ep.now() + self.every
        for i, tg in enumerate(self.ep.targets.values()) if self.fmt else ():
            self.ep.target_says(_text(self.fmt.replace("{t}", str(i)), self.count), tg)
        if self.uart_rx and self.uart in self.ep.uarts:
            self.ep.uart_rx(self.uart, _text(self.uart_rx, self.count))
        self.count += 1


def _stdin_closed(readable) -> bool:
    if sys.stdin in readable:
        try:
            return not os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return True
    return False


def serve_pty(a, ep, console) -> None:
    master, slave = os.openpty()
    tty.setraw(slave)                                    # no echo, no line discipline between host and probe
    print(f"PTY {os.ttyname(slave)}", flush=True)
    os.close(slave)
    os.set_blocking(master, False)
    port = fake_serial.FakeSerialPort(ep, a.port_index, _filter(a))
    pending = bytearray()
    watch = [master] + ([sys.stdin] if not sys.stdin.closed else [])
    while True:
        readable, _, _ = select.select(watch, [], [], 0.005)
        if _stdin_closed(readable):
            return
        if master in readable:
            try:
                data = os.read(master, 65536)
            except OSError:                              # nobody has the port open (EIO)
                data = b""
            if data:
                port.feed(data)
        console.tick()
        port.tick()
        if len(pending) < 1024:
            pending += port.output()
        if pending:
            try:
                pending = pending[os.write(master, pending):]
            except (BlockingIOError, OSError):
                pass                                     # full or not open: keep it (the stream positions hold)


def serve_tcp(a, ep, console) -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.tcp))
    srv.listen(1)
    print(f"PORT {srv.getsockname()[1]}", flush=True)
    watch_stdin = [sys.stdin] if not sys.stdin.closed else []
    while True:
        readable, _, _ = select.select([srv] + watch_stdin, [], [], 0.05)
        if _stdin_closed(readable):
            return
        console.tick()
        ep.tick()
        if srv not in readable:
            continue
        conn, _ = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if _serve_conn(a, ep, console, conn, watch_stdin):
            return
        conn.close()
        if a.once:
            return


def _serve_conn(a, ep, console, conn, watch_stdin) -> bool:
    """One TCP connection; True = stdin closed (end the program)."""
    conn.setblocking(False)
    if a.framing == "cobs":
        port = fake_serial.FakeSerialPort(ep, a.port_index, _filter(a))
    else:
        index = next((i for i, k in enumerate(ep.transports) if i not in ep.serial_ports), 0)
        buf, answers, filt = bytearray(), 0, _filter(a)
    while True:
        readable, _, _ = select.select([conn] + watch_stdin, [], [], 0.005)
        if _stdin_closed(readable):
            return True
        if conn in readable:
            try:
                data = conn.recv(65536)
            except BlockingIOError:
                data = None
            except ConnectionResetError:
                data = b""
            if data == b"":
                return False
            if data:
                if a.framing == "cobs":
                    port.feed(data)
                else:
                    buf += data
                    while len(buf) >= 2:
                        n = struct.unpack_from("<H", buf)[0]
                        if n == 0:
                            del buf[:2]
                            continue
                        if len(buf) < 2 + n:
                            break
                        msg = bytes(buf[2:2 + n])
                        del buf[:2 + n]
                        try:
                            result = ep.handle(msg, index)
                        except ValueError:
                            continue
                        if result is None:
                            continue
                        answers += 1
                        if a.drop == answers and filt(answers, result) is None:
                            continue
                        conn.sendall(a.noise.encode() + struct.pack("<H", len(result)) + result)
        console.tick()
        if a.framing == "cobs":
            port.tick()
            out = port.output()
            if out:
                conn.setblocking(True)
                conn.sendall(out)
                conn.setblocking(False)
        else:
            ep.tick()
            for f in ep.pushes():                                  # events and data pushes (core §11)
                conn.setblocking(True)
                conn.sendall(struct.pack("<H", len(f)) + f)
                conn.setblocking(False)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m oep_client.fake_serve", description=__doc__.split("\n\n")[0])
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--pty", action="store_true")
    where.add_argument("--tcp", type=int, metavar="PORT")
    ap.add_argument("--framing", choices=["cobs", "length"], default="cobs")
    ap.add_argument("--profile", default="p4-x035")
    ap.add_argument("--port-index", type=int)
    ap.add_argument("--noise", default="")
    ap.add_argument("--drop", type=int, default=0)
    ap.add_argument("--corrupt", type=int, default=0)
    ap.add_argument("--console")
    ap.add_argument("--every", default="100")
    ap.add_argument("--slot", action="append", default=[])
    ap.add_argument("--bind", choices=["last-reset", "manual", "mixed"])
    ap.add_argument("--target-id")
    ap.add_argument("--absent", type=int, action="append", default=[])
    ap.add_argument("--uart-plan", action="store_true")
    ap.add_argument("--uart-rx")
    ap.add_argument("--run-hook")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--capture-slipped", action="store_true")
    a = ap.parse_args(argv)
    if a.tcp is None and a.framing == "length":
        ap.error("--framing length is for --tcp")
    profile = fake.PROFILES.get(a.profile) or fake.PROFILES.get(a.profile.replace("_", "-"))
    if profile is None:
        ap.error(f"unknown profile {a.profile}; one of {', '.join(sorted(fake.PROFILES))}")
    if a.port_index is None:
        probe_ep = endpoint.Endpoint(profile(), lambda: 0)
        a.port_index = min(probe_ep.serial_ports) if probe_ep.serial_ports else 0
    ep = build(a)
    console = Console(ep, a.console, _ms(a.every), a.uart_rx, uart_fn(ep) if a.uart_rx else None)
    try:
        if a.tcp is None:
            serve_pty(a, ep, console)
        else:
            serve_tcp(a, ep, console)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
