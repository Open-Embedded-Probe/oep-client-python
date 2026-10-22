"""Serve a virtual bench probe (`endpoint.Endpoint`) on a pty or a TCP port, for other programs' tests.

    python -m oep_client.virtual_bench_serve [--pty | --tcp PORT] [options]

--pty (the default) opens a pseudo terminal that is the probe's serial port (transports §4): COBS frames
0x00 <COBS> 0x00 on one line (OEP frames only). The host opens the printed path itself (and
should set TIOCEXCL on it, as on a real port). On Linux this program keeps a slave fd of its own and watches the
path's opens and closes (inotify): when the last host closes it - or ends without closing it - TIOCEXCL is cleared
and the unread input dropped, as a real port's last close does, so the next host's open succeeds. --tcp PORT (0 = any free
one): --framing length (the default) is length(u16) message as a probe's TCP transport speaks it (no raw bytes): the listening socket is then a TCP transport of the probe (kind 6, listed in fn 0's
describe, its index in every confirm's transport TLV; transports §1), no pause inside a frame restarts the reader
(transports §2), and a length over max_frame closes the connection. The length framing serves up to --tcp-connections
connections at once (default 3, as the reference probe); one more is accepted and closed at once. Each connection is a
transport of its own (transports §1): its own confirm and revision in use, answers back on it, notifications on the
connection their fn's subscribe came on (core §10.4) - none of the others'. The probe behind them is one: the
session, its lock, subscriptions and the resend table are shared (an open from one connection while another's session
holds the lock is refused locked; lock_state shows it from every connection), and a closed connection does not end
them (transports §3: they stay until the lease runs out or another core §9 event; answers and notifications for a
closed connection are dropped; an open with the same session id from another connection takes the session back,
core §6.2 - its subscriptions still send where their subscribe came, until a subscribe again moves them, §10.3). A connection that takes nothing for 2 s while answers wait is
closed as dead; notifications that would make more than 2 x max_frame wait on one are dropped (core §10.4).
--framing cobs, only when asked for, is the serial port again over the socket (emulating a serial port, COBS frames):
it serves one connection at a time (a second one waits until the first closes).

--announce (with --tcp, length framing) announces the port as a probe listening on TCP does (transports §3): DNS-SD
`_oep._tcp` over mDNS - PTR `_oep._tcp.local.` -> instance `OEP virtual <unit_id> <port>`, its SRV (the port, host
`oep-virtual-<unit_id>-<port>.local.`), its TXT `unit_id=<unit_id>` (fn 0's describe's; --unit-id changes it) and the
host's A record(s): the --listen address (127.0.0.1 by default), or for --listen 0.0.0.0 every announcing interface's
address, loopback last. python-zeroconf answers when the mdns extra is installed, else a minimal responder of this
package (virtual_bench_mdns: legacy unicast queries - from a port other than 5353 - are answered to their sender,
queries from 5353 on the group; IPv4). stderr says "virtual_bench_serve: announcing ...".

A CI on one machine (the querying host and the virtual bench on the same host):

    python -m oep_client.virtual_bench_serve --tcp 0 --announce --profile esp32-v003 --unit-id 0123456789ab

then, once `PORT n` is on stdout (the announcement is up by then), query `_oep._tcp.local.` PTR - from an ephemeral
port to 224.0.0.251:5353 (legacy unicast: the answer comes back to that port) or as a full mDNS querier - and open
the SRV port at the A address (127.0.0.1), checking describe's unit_id. The query's multicast loops back to this host:
every interface joins the group, and loopback does too where the OS lets it (Linux does; a host with no multicast
route at all needs --announce-on 127.0.0.1 and the query sent with IP_MULTICAST_IF 127.0.0.1). A different --unit-id
per job keeps parallel jobs apart. A container or VM sees the query only on its own network.

The first line on stdout says where to open: `PTY /dev/pts/N` or `PORT n`. The program ends when stdin closes
(so a test's child never stays behind), or with --once when no TCP connection is left after one was served: with cobs
framing when the first connection closes; with length framing when the last of those open closes (connections that
overlap keep it running; a connection refused past --tcp-connections does not count). With --keep-on-eof the end of
stdin does not end it (stop it with a signal).

Lines on stdin are commands, read between requests (the serving never waits for them):
  reboot                the probe restarts with a new random boot_id (Endpoint.reboot, core §6.5): the session table,
                        the resend table, connections, streams, subscriptions, the plan and the unsaved settings are
                        gone, the saved settings (--label) apply again, the clock starts
                        from 0 and a serial port is back at its boot speed. A request with the old session gets
                        no_session; confirm and open show the new boot_id. The pty or TCP connection stays open (on
                        a serial port the half-read frame and the unsent answers are lost). stderr says
                        "virtual_bench_serve: rebooted, boot_id 0x........"
  lose [CONNECTION]     the line of that connection (every live connection without one) is lost for good
                        (Endpoint.lose, debug §2): the connection and its console streams close, and a request naming
                        either is answered no_resource. stderr says "virtual_bench_serve: lost connection(s) ..."
  capture-overflow FN [ERROR]
                        capture FN drops data inside the segment it is taking (capture §2.2, Endpoint.capture_overflow):
                        that segment is not handed out and the track stops in error - state 6, stopped reason 3, error
                        ERROR (default 2, storage; 1 for a DMA / peripheral failure)
A line that is no command is ignored with a message on stderr.

oep.probe.restart's restart (oep-if-restart; every profile lists the interface, after its other fns) does the same
from a request: the answer (completed success, no payload) goes out first, then the probe reboots as above. The pty or
TCP connection stays open, as the `reboot` command leaves it; what the probe had read behind the restart request is
dropped. A host waits a moment (host guide §5.2), may close and open again (the pty and the TCP listener take a new
open), and confirms: the boot_id is new. Its describe declares restart_max_ms 2000 (oep-if-restart §1). --no-restart makes a
probe without the optional interface: list does not show oep.probe.restart, and its fn is unknown_function.

Options:
  --profile NAME        core-v1 (current core only; no targets), p4-x035 (default), esp32-v003, esp32-v003-64, p4-bench or rp2350-pins (p4_x035 style names work too)
  --port-index N        which serial port of the profile the pty / cobs TCP is (default: the first one)
  --noise TEXT          raw bytes written in front of every answer (the host must skip them)
  --drop N              the N-th answer (1-based) is not sent, once (the request did run: a resend gets the
                        remembered result)
  --corrupt N           the N-th answer goes out once with a broken CRC
  --console FMT         what the targets write to their consoles, %d = a counter, {t} = the target's index
  --every MS            how often (default 100; "100ms" works too)
  --target-id HEX       the target_id every target's attach reports (scheme dmi_7f)
  --absent N            the N-th pin pair of the first wire has no target (repeatable)
  --silent-until-reset N
                        the N-th pin pair of the first wire has a target that answers nothing on the wire until a
                        reset through its line (repeatable): a host's attach with the reset TLV
  --label CH=TEXT       channel.{}.label of channel CH at boot, as if saved (repeatable; e.g. 23=v003.nrst names the
                        reset line of the host's profile v003, host guide §5.3)
  --capture-slipped     every oep.fixture.logic segment says flags bit2 (slipped: a pace that fell behind)
  --logic-wave ROLE=SPEC
                        a logic role's waveform instead of the counter's bit (repeatable): counter, quiet[:LEVEL],
                        square:PERIOD (samples) or uart:BAUD[:TEXT] (8N1 characters with idle between) - quiet and
                        UART-like lines to measure format 1's typical bandwidth (capture §1.3)
  --uart-rx TEXT        what arrives on the first oep.fixture.uart's RX every --every ms once a session planned and
                        configured it (%d = a counter)
  --no-port-speed       the profile's port_speed (oep.probe.link, oep-if-link §3; esp32-v003 has it) off: not in the ops, unknown_operation
  --no-restart          a probe without oep.probe.restart (oep-if-restart, optional): not listed, unknown_function
  --minimal-ops         console and fixture UART at their smallest (console §1, fixture §2): marks, clear, mark and
                        write not in their ops (unknown_operation; read from 3 unsupported) - a host's test that it
                        sends an optional op only when describe's ops set it (conformance §2)
  --broken-rate SPEC    port_speed's line model: frames at RATE break (repeatable). SPEC is
                        RATE[:MIN_SIZE][:in|out][:duplex][:everyN][:afterB]: only frames of MIN_SIZE bytes and more on the wire
                        (default every frame), only towards the host (in) or the probe (out) (default both), only
                        while both ways carry such frames at once (duplex), only every Nth of them (everyN), only once
                        B bytes have passed at RATE since the switch to it (afterB). The virtual bench cannot see the host's own rate (a pty,
                        TCP): only the probe's rate decides
  --keep-on-eof         the end of stdin does not end the program
  --tcp-connections N   with --tcp and length framing: connections served at once (default 3); one more is accepted
                        and closed at once
  --once                with --tcp: end when no connection is left after one was served (above)
  --listen ADDR         the address --tcp listens on (default 127.0.0.1; 0.0.0.0: every interface)
  --unit-id ID          fn 0's describe's unit_id instead of the profile's (core §7.5 grammar: [a-z0-9-], 1-32)
  --announce            with --tcp: DNS-SD `_oep._tcp` over mDNS for the port (above)
  --announce-on ADDR    an interface, by its IPv4 address, to answer on (repeatable; default every interface with an
                        IPv4 address, loopback included where the OS lets it join the group)
  --announce-engine E   auto (default: zeroconf when installed), zeroconf or minimal
  --run-hook SPEC       what riscv-dm run does on every target: SPEC is module:function or path/file.py:function,
                        called as function(target, pc, regs) -> (stopped, dpc, elapsed_us). `target` is the
                        endpoint.VirtualTarget (mem = word address -> value, regs = regno -> value, halted, dpc), so a
                        host's loader can be played by the host's own test code (the virtual bench knows no loader).
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

from . import catalog, endpoint, virtual_bench, virtual_bench_capture, virtual_bench_serial, message as m, registry as reg

TCP_CONNECTIONS = 3                                      # --tcp-connections' default (length framing)


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


def _profile(name):
    name = name.replace("_", "-")
    return virtual_bench.CORE_PROFILES.get(name) or virtual_bench.PROFILES.get(name)


def build(a: argparse.Namespace) -> endpoint.Endpoint:
    profile = _profile(a.profile)
    probe = profile()
    if getattr(a, "unit_id", None):
        probe = virtual_bench.with_unit_id(probe, a.unit_id)
    if getattr(a, "no_restart", False):
        probe = virtual_bench.without(probe, virtual_bench.RESTART)        # the optional oep.probe.restart left out (oep-if-restart)
    if getattr(a, "minimal_ops", False):
        probe = virtual_bench.minimal_ops(probe)                          # console / fixture UART at their smallest
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
    for spec in getattr(a, "logic_wave", []):
        role, _, wave = spec.partition("=")
        for cap in ep.captures.values():
            if not cap.analog:
                cap.waves[int(role)] = virtual_bench_capture.logic_wave(wave)
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
    for spec in getattr(a, "label", []):
        channel, _, text = spec.partition("=")
        items.append((0, "channel.{}.label", int(channel, 0), text.encode()))
    if items:
        ep.load_settings(items, saved=True)
    return ep


def uart_fn(ep: endpoint.Endpoint) -> int:
    fn = ep.fns.get("oep.fixture.uart")
    if fn is None:
        raise SystemExit("this profile offers no oep.fixture.uart")
    return fn


def _load_hook(spec: str):
    where, _, name = spec.rpartition(":")
    if not where or not name:
        raise SystemExit(f"--run-hook {spec}: want module:function or file.py:function")
    if where.endswith(".py") or os.sep in where:
        loader = importlib.util.spec_from_file_location("virtual_bench_run_hook", where)
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
        self.responder = None                             # --announce's mDNS responder: served in the same select
        self.port: virtual_bench_serial.VirtualSerialPort | None = None
        self.buf = b""
        try:
            self.fd = None if sys.stdin is None or sys.stdin.closed else sys.stdin.fileno()
        except (OSError, ValueError):
            self.fd = None

    def watch(self) -> list:
        return ([] if self.fd is None else [self.fd]) + (self.responder.watch() if self.responder else [])

    def poll(self, readable) -> bool:
        if self.responder is not None:
            self.responder.poll(readable)
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
            print(f"virtual_bench_serve: rebooted, boot_id 0x{self.ep.boot_id:08X}", file=sys.stderr, flush=True)
        elif text.split()[0] == "lose" and len(text.split()) <= 2:
            args = text.split()[1:]
            try:
                cid = int(args[0], 0) if args else None
            except ValueError:
                print(f"virtual_bench_serve: lose {args[0]!r}: want a connection number", file=sys.stderr, flush=True)
                return
            gone = self.ep.lose(cid)
            print(f"virtual_bench_serve: lost connection(s) {', '.join(map(str, gone)) or 'none'}", file=sys.stderr, flush=True)
        elif text.split()[0] == "capture-overflow" and 2 <= len(text.split()) <= 3:
            try:
                fn, *err = (int(w, 0) for w in text.split()[1:])
                self.ep.capture_overflow(fn, *err)
            except (ValueError, KeyError):
                print(f"virtual_bench_serve: {text!r}: want a capture fn and an optional error", file=sys.stderr, flush=True)
                return
            print(f"virtual_bench_serve: capture fn {fn} dropped data inside a segment", file=sys.stderr, flush=True)
        else:
            print(f"virtual_bench_serve: unknown command {text!r} ignored (known: reboot, lose, capture-overflow)",
                  file=sys.stderr, flush=True)


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
    port = commands.port = virtual_bench_serial.VirtualSerialPort(ep, a.port_index, _filter(a))
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
    srv.bind((a.listen, a.tcp))
    srv.listen(8)
    port = srv.getsockname()[1]
    if a.framing == "length":
        # the listening socket is a TCP transport of its own (transports §1, C-05): listed in describe, named by confirm
        ep.tcp_index = ep.add_transport(virtual_bench.TRANSPORT["tcp"])
    if a.announce:
        commands.responder = announce(a, ep, port)
    print(f"PORT {port}", flush=True)
    if a.framing == "length":
        try:
            _serve_length(a, ep, console, commands, srv)
        finally:
            srv.close()
        return
    while True:                                          # cobs: a serial port - one connection at a time
        readable, _, _ = select.select([srv] + commands.watch(), [], [], 0.05)
        if commands.poll(readable):
            return
        console.tick()
        ep.tick()
        ep.pushes()                                      # no connection: notifications are dropped (transports §3)
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


class _Client:
    """One accepted TCP connection of the length framing: a transport of its own (transports §1) - its `link` names it to
    the Endpoint (the revision in use, the notifications' target); the frame it is reading, and what the kernel did
    not take yet."""

    WRITE_WAIT_MS = 2000                                 # a connection that takes nothing for this long is closed

    _numbers = iter(range(1, 1 << 62))

    def __init__(self, sock: socket.socket, a):
        self.sock, self.number = sock, next(self._numbers)
        self.link = ("tcp", self.number)
        self.buf, self.out = bytearray(), bytearray()
        self.answers, self.filt = 0, _filter(a)
        self.stalled_since: float | None = None

    def send(self, data: bytes) -> None:
        self.out += data
        self.flush()

    def flush(self) -> bool:
        """Hand what waits to the kernel; False = the connection is dead (reset, or nothing taken for WRITE_WAIT_MS)."""
        if not self.out:
            self.stalled_since = None
            return True
        try:
            n = self.sock.send(self.out)
        except BlockingIOError:
            n = 0
        except OSError:
            return False
        del self.out[:n]
        if n or not self.out:
            self.stalled_since = None if not self.out else time.monotonic()
            return True
        if self.stalled_since is None:
            self.stalled_since = time.monotonic()
        return (time.monotonic() - self.stalled_since) * 1000 < self.WRITE_WAIT_MS


def _serve_length(a, ep, console, commands: Commands, srv: socket.socket) -> None:
    """The length framing: up to --tcp-connections connections at once, each a transport of its own (transports §1)
    sharing the one probe (sessions, the lock, the resend table: transports §3). One more is accepted and closed at
    once. Answers go back on the connection the request came on, notifications where their fn's subscribe came (core
    §11.4); one whose connection is gone is dropped (transports §3)."""
    index = ep.tcp_index
    clients: list[_Client] = []
    served = False                                       # --once: some connection was served
    limit = 2 * ep.probe.max_frame                       # notifications waiting in a connection at most (core §10.4)

    def drop(c: _Client) -> None:
        clients.remove(c)
        try:
            c.sock.close()
        except OSError:
            pass

    while True:
        socks = [c.sock for c in clients]
        writing = [c.sock for c in clients if c.out]
        readable, writable, _ = select.select([srv] + socks + commands.watch(), writing, [], 0.005)
        if commands.poll(readable):
            return
        if srv in readable:
            try:
                sock, _ = srv.accept()
            except OSError:
                sock = None
            if sock is not None and len(clients) >= a.tcp_connections:
                sock.close()                             # past --tcp-connections: accepted and closed at once
            elif sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setblocking(False)
                clients.append(_Client(sock, a))
                served = True
        boots = ep.reboots
        for c in list(clients):
            if c.sock not in readable or c not in clients:
                continue
            try:
                data = c.sock.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:
                drop(c)                                  # closed: the session and its lock stay (transports §3)
                continue
            c.buf += data
            while len(c.buf) >= 2 and c in clients:
                n = struct.unpack_from("<H", c.buf)[0]
                if n == 0:
                    del c.buf[:2]
                    continue
                if n > ep.probe.max_frame:
                    drop(c)                              # over max_frame on TCP: the probe closes it (transports §1)
                    break
                if len(c.buf) < 2 + n:
                    break
                msg = bytes(c.buf[2:2 + n])
                del c.buf[:2 + n]
                try:
                    result = ep.handle(msg, index, link=c.link)
                except ValueError:
                    continue
                if result is not None:
                    c.answers += 1
                    if not (a.drop == c.answers and c.filt(c.answers, result) is None):
                        c.send(a.noise.encode() + struct.pack("<H", len(result)) + result)
                if ep.reboots != boots:
                    break                                # a restart (oep-if-restart §2): what came behind it is lost
            if ep.reboots != boots:
                for other in clients:
                    other.buf.clear()                    # read for the old boot, on every connection
                break
        console.tick()
        ep.tick()
        live = {c.link: c for c in clients}
        for link, f in ep.pushes_to():                   # events and data pushes (core §10), answers first (§11.4)
            c = live.get(link)
            if c is not None and len(c.out) + 2 + len(f) <= limit:   # else dropped: its seq shows the gap (§10.3)
                c.send(struct.pack("<H", len(f)) + f)
        for c in list(clients):
            if (c.out or c.sock in writable) and not c.flush():
                drop(c)                                  # dead: nothing taken for WRITE_WAIT_MS, or reset
        if a.once and served and not clients:
            return


def announce(a, ep, port: int):
    """--announce: the mDNS responder for this port (virtual_bench_mdns), its records said on stderr."""
    from . import virtual_bench_mdns as mdns
    interfaces = a.announce_on or None
    addresses = mdns.reachable_addresses(a.listen, a.announce_on or mdns.interface_addresses())
    ann = mdns.Announcement(virtual_bench.unit_id_of(ep.probe), port, addresses)
    try:
        r = mdns.start(ann, a.announce_engine, interfaces)
    except ImportError:
        raise SystemExit("virtual_bench_serve: --announce-engine zeroconf: install the mdns extra "
                         "(pip install 'oep-client-python[mdns]')") from None
    except OSError as e:
        raise SystemExit(f"virtual_bench_serve: --announce: {e}") from None
    print(f"virtual_bench_serve: announcing {ann.instance} unit_id={ann.unit_id} host {ann.host} port {port} "
          f"A {', '.join(addresses)} ({r.engine}, on {', '.join(r.joined)})", file=sys.stderr, flush=True)
    if a.listen.startswith("127.") and any(not ip.startswith("127.") for ip in r.joined):
        print(f"virtual_bench_serve: listening on {a.listen}: only this machine can connect (--listen 0.0.0.0 for "
              "others)", file=sys.stderr, flush=True)
    return r


def _serve_conn(a, ep, console, conn, commands: Commands) -> bool:
    """One TCP connection with the COBS framing (the serial port again); True = stdin closed (end the program)."""
    conn.setblocking(False)
    port = commands.port = virtual_bench_serial.VirtualSerialPort(ep, a.port_index, _filter(a))
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
                port.feed(data)
        console.tick()
        port.tick()
        out = port.output()
        if out:
            conn.setblocking(True)
            conn.sendall(out)
            conn.setblocking(False)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line -> the options `build` takes (the port index filled in)."""
    ap = argparse.ArgumentParser(prog="python -m oep_client.virtual_bench_serve", description=__doc__.split("\n\n")[0])
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--pty", action="store_true")
    where.add_argument("--tcp", type=int, metavar="PORT")
    ap.add_argument("--framing", choices=["cobs", "length"])
    ap.add_argument("--profile", default="p4-x035")
    ap.add_argument("--port-index", type=int)
    ap.add_argument("--noise", default="")
    ap.add_argument("--drop", type=int, default=0)
    ap.add_argument("--corrupt", type=int, default=0)
    ap.add_argument("--console")
    ap.add_argument("--every", default="100")
    ap.add_argument("--target-id")
    ap.add_argument("--absent", type=int, action="append", default=[])
    ap.add_argument("--silent-until-reset", type=int, action="append", default=[], metavar="N")
    ap.add_argument("--label", action="append", default=[], type=_label, metavar="CH=TEXT")
    ap.add_argument("--uart-rx")
    ap.add_argument("--run-hook")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--tcp-connections", type=int, metavar="N")
    ap.add_argument("--keep-on-eof", action="store_true")
    ap.add_argument("--capture-slipped", action="store_true")
    ap.add_argument("--logic-wave", action="append", default=[], metavar="ROLE=SPEC")
    ap.add_argument("--no-port-speed", action="store_true")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--minimal-ops", action="store_true")
    ap.add_argument("--broken-rate", action="append", default=[])
    ap.add_argument("--listen", default="127.0.0.1", metavar="ADDR")
    ap.add_argument("--unit-id", metavar="ID")
    ap.add_argument("--announce", action="store_true")
    ap.add_argument("--announce-on", action="append", default=[], metavar="ADDR")
    ap.add_argument("--announce-engine", choices=["auto", "zeroconf", "minimal"], default="auto")
    a = ap.parse_args(argv)
    if a.tcp is None and a.framing == "length":
        ap.error("--framing length is for --tcp")
    if a.announce and a.tcp is None:
        ap.error("--announce is for --tcp")
    if a.announce and a.framing == "cobs":
        ap.error("--announce: a host that finds the port speaks length frames (transports §1); drop --framing cobs")
    if a.framing is None:
        a.framing = "length" if a.tcp is not None else "cobs"   # TCP: length frames as a probe's TCP transport (transports §1)
    if a.tcp_connections is not None and (a.tcp is None or a.framing != "length"):
        ap.error("--tcp-connections is for --tcp with length framing (cobs over TCP is a serial port: one connection "
                 "at a time)")
    if a.tcp_connections is None:
        a.tcp_connections = TCP_CONNECTIONS
    if a.tcp_connections < 1:
        ap.error("--tcp-connections: 1 or more")
    if a.unit_id is not None and not virtual_bench.UNIT_ID.fullmatch(a.unit_id):
        ap.error(f"--unit-id {a.unit_id}: [a-z0-9-], 1 to 32 characters (core §7.5)")
    for ip in a.announce_on:
        try:
            socket.inet_aton(ip)
        except OSError:
            ap.error(f"--announce-on {ip}: an interface's IPv4 address")
    profile = _profile(a.profile)
    if profile is None:
        ap.error(f"unknown profile {a.profile}; one of {', '.join(sorted(virtual_bench.PROFILES | virtual_bench.CORE_PROFILES))}")
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
    finally:
        if commands.responder is not None:
            commands.responder.close()


if __name__ == "__main__":
    main()
