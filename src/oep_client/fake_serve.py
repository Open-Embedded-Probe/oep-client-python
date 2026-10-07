"""Serve a fake probe (`endpoint.Endpoint`) on a pty or a TCP port, for other programs' tests.

    python -m oep_client.fake_serve [--pty | --tcp PORT] [options]

--pty (the default) opens a pseudo terminal that is the probe's serial port (transports §4): COBS frames
0x00 <COBS> 0x00 and the raw bytes of the port's bind on one line. The host opens the printed path itself (and
should set TIOCEXCL on it, as on a real port). On Linux this program keeps a slave fd of its own and watches the
path's opens and closes (inotify): when the last host closes it - or ends without closing it - TIOCEXCL is cleared
and the unread input dropped, as a real port's last close does, so the next host's open succeeds. --tcp PORT (0 = any free
one) serves one connection at a time: --framing cobs is the serial port again, --framing length is
length(u16) message as on vendor bulk / TCP (no raw bytes): the listening socket is then a TCP transport of the
probe (kind 6, listed in fn 0's describe, its index in every confirm's transport TLV; transports §1), no pause inside a
frame restarts the reader (transports §2), and a length over max_frame closes the connection.

The first line on stdout says where to open: `PTY /dev/pts/N` or `PORT n`. The program ends when stdin closes
(so a test's child never stays behind), or with --once when the first TCP connection closes. With --keep-on-eof the
end of stdin does not end it (stop it with a signal).

Lines on stdin are commands, read between requests (the serving never waits for them):
  reboot                the probe restarts with a new random boot_id (Endpoint.reboot, core §6.5): the session table,
                        the resend table, connections, streams, subscriptions, the plan and the unsaved settings are
                        gone, the saved settings (--slot, --bind, --label, --uart-plan) apply again, the clock starts
                        from 0 and a serial port is back at its boot speed. A request with the old session gets
                        no_session; confirm and open show the new boot_id. The pty or TCP connection stays open (on
                        a serial port the half-read frame and the unsent answers are lost). stderr says
                        "fake_serve: rebooted, boot_id 0x........"
  lose [CONNECTION]     the line of that connection (every live connection without one) is lost for good
                        (Endpoint.lose, debug §2): the connection closes, its console streams get mark link-lost and
                        close with detail 4, and a request naming it is answered no_connection. An at-boot --slot on
                        its place attaches again by itself at its next retry (a new connection), its bound console
                        back under the same stream number. stderr says "fake_serve: lost connection(s) ..."
A line that is no command is ignored with a message on stderr.

oep.probe.restart's restart (oep-if-restart; every profile lists the interface, after its other fns) does the same
from a request: the answer (completed success, no payload) goes out first, then the probe reboots as above. The pty or
TCP connection stays open, as the `reboot` command leaves it; what the probe had read behind the restart request is
dropped. A host waits a moment (host guide §5.2), may close and open again (the pty and the TCP listener take a new
open), and confirms: the boot_id is new. Its describe declares restart_max_ms 2000 (oep-if-restart §1). --no-restart makes a
probe without the optional interface: list does not show oep.probe.restart, and its fn is unknown_function.

Options:
  --profile NAME        p4-x035 (default), esp32-v003, esp32-v003-64, p4-bench or rp2350-pins (p4_x035 style names work too)
  --port-index N        which serial port of the profile the pty / cobs TCP is (default: the first one)
  --noise TEXT          raw bytes written in front of every answer (the host must skip them)
  --drop N              the N-th answer (1-based) is not sent, once (the request did run: a resend gets the
                        remembered result)
  --corrupt N           the N-th answer goes out once with a broken CRC
  --console FMT         what the targets write to their consoles, %d = a counter, {t} = the target's index
  --every MS            how often (default 100; "100ms" works too)
  --slot NAME           register a slot at boot (repeatable; the n-th on the n-th pin pair of the first wire, at boot,
                        retry 1000 ms, mechanism dmseq), as if saved
  --bind N              the serial port carries the console of the N-th --slot (0 = the first; probe.config §1.2)
  --target-id HEX       the target_id every target's attach reports (scheme dmi_7f)
  --absent N            the N-th pin pair of the first wire has no target (repeatable)
  --silent-until-reset N
                        the N-th pin pair of the first wire has a target that answers nothing on the wire until a
                        reset through its line (repeatable): a host's attach with the reset TLV
  --label CH=TEXT       a label item on channel CH at boot, as if saved (repeatable; e.g. 23=v003.nrst names slot
                        v003's reset line, probe.config §1.3)
  --no-drive-levels     the profile's oep.fixture.gpio without drive_levels (fixture §1.1): a probe that cannot switch
                        the output strength (describe has no levels; a set with a drive is refused unsupported)
  --capture-slipped     every oep.fixture.logic segment says flags bit2 (slipped: a pace that fell behind)
  --uart-plan           the first oep.fixture.uart gets its RX / TX plan at boot, as if saved (the jig's "DUT TX" /
                        "DUT RX" labels when the profile has them, else the first free channels); configure then works
  --uart-rx TEXT        what arrives on that UART's RX every --every ms once it is configured (%d = a counter)
  --no-port-speed       the profile's port_speed (oep.probe.link, oep-if-link §3; esp32-v003 has it) off: not in the ops, unknown_operation
  --no-restart          a probe without oep.probe.restart (oep-if-restart, optional): not listed, unknown_function
  --broken-rate SPEC    port_speed's line model: frames at RATE break (repeatable). SPEC is
                        RATE[:MIN_SIZE][:in|out][:duplex][:everyN][:afterB]: only frames of MIN_SIZE bytes and more on the wire
                        (default every frame), only towards the host (in) or the probe (out) (default both), only
                        while both ways carry such frames at once (duplex), only every Nth of them (everyN), only once
                        B bytes have passed at RATE since the switch to it (afterB). The fake cannot see the host's own rate (a pty,
                        TCP): only the probe's rate decides
  --keep-on-eof         the end of stdin does not end the program
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
import secrets
import select
import socket
import struct
import sys
import time
import tty

from . import catalog, endpoint, fake, fake_serial, message as m, registry as reg

_ITEM = reg.PROBE_CONFIG.tlv["item"]


def _ms(text: str) -> int:
    return int(re.fullmatch(r"(\d+)\s*(ms)?", text.strip()).group(1))


def _label(text: str) -> str:
    channel, sep, _ = text.partition("=")
    try:
        int(channel, 0)
    except ValueError:
        sep = ""
    if not sep:
        raise argparse.ArgumentTypeError(f"{text}: want CH=TEXT")
    return text


def build(a: argparse.Namespace) -> endpoint.Endpoint:
    profile = fake.PROFILES.get(a.profile) or fake.PROFILES[a.profile.replace("_", "-")]
    probe = profile()
    if getattr(a, "no_drive_levels", False):
        probe = fake.without_drive_levels(probe)
    if getattr(a, "no_restart", False):
        probe = fake.without(probe, fake.RESTART)        # the optional oep.probe.restart left out (oep-if-restart)
    start = time.monotonic_ns()
    # the clock in ns since this start (core §2.6a), and a boot_id from the OS's random source (core §6.5, C-19)
    ep = endpoint.Endpoint(probe, lambda: (time.monotonic_ns() - start) // 1_000_000, boot_id=secrets.randbits(32),
                           now_ns=lambda: time.monotonic_ns() - start)
    wire = min(ep.pairs) if ep.pairs else None
    if a.target_id is not None:
        for tg in ep.targets.values():
            tg.target_id = int(a.target_id, 16)
    if a.run_hook:
        hook = _load_hook(a.run_hook)
        for tg in ep.targets.values():
            tg.run_hook = (lambda t: lambda pc, regs: hook(t, pc, regs))(tg)
    ep.capture_slipped = getattr(a, "capture_slipped", False)
    if getattr(a, "no_port_speed", False):
        ep.port_speed_base = None
    for spec in getattr(a, "broken_rate", []):
        rate, *rest = spec.split(":")
        size = next((int(x) for x in rest if x.isdigit()), 0)
        way = next((x for x in rest if x in ("in", "out")), None)
        every = next((int(x[5:]) for x in rest if x.startswith("every") and x[5:].isdigit()), 1)
        after = next((int(x[5:]) for x in rest if x.startswith("after") and x[5:].isdigit()), 0)
        ep.broken_rates[int(rate)] = endpoint.BrokenRate(size, to_host=way != "out", to_probe=way != "in",
                                                         duplex="duplex" in rest, every=every, after=after)
    for n in a.absent:
        ep.targets[(wire, ep.pairs[wire][n])].present = False
    for n in getattr(a, "silent_until_reset", []):
        ep.targets[(wire, ep.pairs[wire][n])].silent_until_reset = True
    items = []
    for n, name in enumerate(a.slot):
        swdio, swclk = ep.pairs[wire][n]
        mech = 2 if 2 in ep.mechanisms else min(ep.mechanisms)
        raw = name.encode()
        value = struct.pack("<BHHHBIIBBB", n, wire, swdio, swclk, reg.PROBE_CONFIG.enum["slot_attach"]["at_boot"],
                            1000, 0, 0, mech, len(raw)) + raw
        items.append(m.tlv(_ITEM["slot"], value))                 # retry 1 s, no speed ceiling, rests high
    if a.bind is not None:
        if not 0 <= a.bind < len(a.slot):
            raise SystemExit(f"--bind {a.bind}: no such --slot")
        port = a.port_index if a.port_index is not None else min(ep.serial_ports)
        value = struct.pack("<BBH", port, reg.PROBE_CONFIG.enum["bind_stream"]["slot_console"], a.bind)
        items.append(m.tlv(_ITEM["bind"], value))                 # port kind id (probe.config §1.2)
    for spec in getattr(a, "label", []):
        channel, _, text = spec.partition("=")
        value = struct.pack("<H", int(channel, 0)) + text.encode()
        items.append(m.tlv(_ITEM["label"], value))
    if a.uart_plan:
        fn = uart_fn(ep)
        rx, tx = _uart_channels(ep, fn)
        for role, ch in ((1, rx), (2, tx)):
            items.append(m.tlv(_ITEM["plan"], struct.pack("<HBH", fn, role, ch)))
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
    for tag, v in ep.decl[fn]:
        if tag == catalog.ROLE_CHANNELS and v[0] == 1:
            allowed = catalog.bitmap_to_channels(struct.unpack_from("<H", v, 1)[0], v[3:])
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


class Commands:
    """The command lines on stdin (see the module's doc), read without blocking: `watch()` is what select waits on,
    `poll(readable)` reads what came and runs every whole line; True = stdin ended and the program ends with it (not
    with `keep_on_eof`, which then stops watching stdin). `port` is the serial port being served (a reboot drops its
    half-read frame and unsent answers), None for the length framing."""

    def __init__(self, ep: endpoint.Endpoint, keep_on_eof: bool = False):
        self.ep, self.keep_on_eof = ep, keep_on_eof
        self.port: fake_serial.FakeSerialPort | None = None
        self.buf = b""
        try:
            self.fd = None if sys.stdin is None or sys.stdin.closed else sys.stdin.fileno()
        except (OSError, ValueError):
            self.fd = None

    def watch(self) -> list:
        return [] if self.fd is None else [self.fd]

    def poll(self, readable) -> bool:
        if self.fd is None or self.fd not in readable:
            return False
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            data = b""
        if data:
            self.buf += data
            while b"\n" in self.buf:
                line, _, self.buf = self.buf.partition(b"\n")
                self.run(line)
            return False
        if self.buf:
            self.run(self.buf)                           # a last line without its newline
            self.buf = b""
        self.fd = None
        return not self.keep_on_eof

    def run(self, line: bytes) -> None:
        text = line.decode(errors="replace").strip()
        if not text:
            return
        if text == "reboot":
            self.ep.reboot()
            if self.port is not None:
                self.port.reboot()
            print(f"fake_serve: rebooted, boot_id 0x{self.ep.boot_id:08X}", file=sys.stderr, flush=True)
        elif text.split()[0] == "lose" and len(text.split()) <= 2:
            args = text.split()[1:]
            try:
                cid = int(args[0], 0) if args else None
            except ValueError:
                print(f"fake_serve: lose {args[0]!r}: want a connection number", file=sys.stderr, flush=True)
                return
            gone = self.ep.lose(cid)
            print(f"fake_serve: lost connection(s) {', '.join(map(str, gone)) or 'none'}", file=sys.stderr, flush=True)
        else:
            print(f"fake_serve: unknown command {text!r} ignored (known: reboot, lose)", file=sys.stderr,
                  flush=True)


class _Openers:
    """Who has the pty's slave open, from inotify's open / close events on its path (Linux). The slave's tty lives on
    while the master is open, so a TIOCEXCL its last host left set (a program that ended without closing the port)
    would refuse every later opener with EBUSY, where a real port's tty clears it at its last close. This keeps one
    slave fd of its own (`keeper`), opened before any host, and when the last host's close comes: TIOCNXCL on it and the
    unread input dropped, as a real port does. Without inotify (not Linux): keeper None, nothing is tracked."""

    IN_OPEN, IN_CLOSE_WRITE, IN_CLOSE_NOWRITE, IN_Q_OVERFLOW = 0x20, 0x08, 0x10, 0x4000

    def __init__(self, path: str, keeper: int):
        self.fd, self.keeper, self.count = -1, None, 0
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return
            events = self.IN_OPEN | self.IN_CLOSE_WRITE | self.IN_CLOSE_NOWRITE
            if libc.inotify_add_watch(fd, os.fsencode(path), events) < 0:
                os.close(fd)
                return
        except (OSError, AttributeError):
            return
        self.fd, self.keeper = fd, keeper

    @property
    def present(self) -> bool:
        return self.keeper is None or self.count > 0     # untracked: as before, writes are tried

    def poll(self) -> None:
        if self.fd < 0:
            return
        while True:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                return
            i = 0
            while i + 16 <= len(data):
                _wd, mask, _cookie, length = struct.unpack_from("iIII", data, i)
                i += 16 + length
                if mask & self.IN_OPEN:
                    self.count += 1
                if mask & (self.IN_CLOSE_WRITE | self.IN_CLOSE_NOWRITE):
                    self.count = max(0, self.count - 1)
                    if self.count == 0:
                        self.let_go()
                if mask & self.IN_Q_OVERFLOW:
                    self.count = 0                       # lost track: the next close / write tells again
                    self.let_go()

    def let_go(self) -> None:
        import fcntl
        import termios
        try:
            fcntl.ioctl(self.keeper, termios.TIOCNXCL)   # exclusive mode off: the next opener gets the port
            termios.tcflush(self.keeper, termios.TCIFLUSH)   # what the last host left unread goes with it
        except OSError:
            pass


def serve_pty(a, ep, console, commands: Commands) -> None:
    master, slave = os.openpty()
    tty.setraw(slave)                                    # no echo, no line discipline between host and probe
    path = os.ttyname(slave)
    openers = _Openers(path, slave)
    if openers.keeper is None:
        os.close(slave)
    print(f"PTY {path}", flush=True)
    os.set_blocking(master, False)
    port = commands.port = fake_serial.FakeSerialPort(ep, a.port_index, _filter(a))
    pending = bytearray()
    watch = [master] + ([openers.fd] if openers.fd >= 0 else [])
    while True:
        readable, _, _ = select.select(watch + commands.watch(), [], [], 0.005)
        if commands.poll(readable):
            return
        openers.poll()
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
        if pending and openers.present:
            try:
                pending = pending[os.write(master, pending):]
            except (BlockingIOError, OSError):
                pass                                     # full or not open: keep it (the stream positions hold)


def serve_tcp(a, ep, console, commands: Commands) -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.tcp))
    srv.listen(1)
    print(f"PORT {srv.getsockname()[1]}", flush=True)
    while True:
        readable, _, _ = select.select([srv] + commands.watch(), [], [], 0.05)
        if commands.poll(readable):
            return
        console.tick()
        ep.tick()
        if srv not in readable:
            continue
        conn, _ = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ended = _serve_conn(a, ep, console, conn, commands)
        commands.port = None
        if ended:
            return
        conn.close()
        if a.once:
            return


def _serve_conn(a, ep, console, conn, commands: Commands) -> bool:
    """One TCP connection; True = stdin closed (end the program)."""
    conn.setblocking(False)
    if a.framing == "cobs":
        port = commands.port = fake_serial.FakeSerialPort(ep, a.port_index, _filter(a))
    else:
        # the listening socket is a TCP transport of its own (transports §1, C-05): listed in describe, named by confirm
        index = getattr(ep, "tcp_index", None)
        if index is None:
            index = ep.tcp_index = ep.add_transport(fake.TRANSPORT["tcp"])
        buf, answers, filt = bytearray(), 0, _filter(a)
    while True:
        readable, _, _ = select.select([conn] + commands.watch(), [], [], 0.005)
        if commands.poll(readable):
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
                        if n > ep.probe.max_frame:
                            return False                           # over max_frame on TCP: the probe closes (transports §1)
                        if len(buf) < 2 + n:
                            break
                        msg = bytes(buf[2:2 + n])
                        del buf[:2 + n]
                        boots = ep.reboots
                        try:
                            result = ep.handle(msg, index)
                        except ValueError:
                            continue
                        if result is None:
                            continue
                        answers += 1
                        if not (a.drop == answers and filt(answers, result) is None):
                            conn.sendall(a.noise.encode() + struct.pack("<H", len(result)) + result)
                        if ep.reboots != boots:
                            buf.clear()                            # a restart (oep-if-restart §2): what came behind it is lost
                            break
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


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line -> the options `build` takes (the port index filled in)."""
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
    ap.add_argument("--bind", type=int, metavar="N")
    ap.add_argument("--target-id")
    ap.add_argument("--absent", type=int, action="append", default=[])
    ap.add_argument("--silent-until-reset", type=int, action="append", default=[], metavar="N")
    ap.add_argument("--label", action="append", default=[], type=_label, metavar="CH=TEXT")
    ap.add_argument("--no-drive-levels", action="store_true")
    ap.add_argument("--uart-plan", action="store_true")
    ap.add_argument("--uart-rx")
    ap.add_argument("--run-hook")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--keep-on-eof", action="store_true")
    ap.add_argument("--capture-slipped", action="store_true")
    ap.add_argument("--no-port-speed", action="store_true")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--broken-rate", action="append", default=[])
    a = ap.parse_args(argv)
    if a.tcp is None and a.framing == "length":
        ap.error("--framing length is for --tcp")
    profile = fake.PROFILES.get(a.profile) or fake.PROFILES.get(a.profile.replace("_", "-"))
    if profile is None:
        ap.error(f"unknown profile {a.profile}; one of {', '.join(sorted(fake.PROFILES))}")
    if a.port_index is None:
        probe_ep = endpoint.Endpoint(profile(), lambda: 0)
        a.port_index = min(probe_ep.serial_ports) if probe_ep.serial_ports else 0
    return a


def main(argv: list[str] | None = None) -> None:
    a = parse(argv)
    ep = build(a)
    console = Console(ep, a.console, _ms(a.every), a.uart_rx, uart_fn(ep) if a.uart_rx else None)
    commands = Commands(ep, a.keep_on_eof)
    try:
        if a.tcp is None:
            serve_pty(a, ep, console, commands)
        else:
            serve_tcp(a, ep, console, commands)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
