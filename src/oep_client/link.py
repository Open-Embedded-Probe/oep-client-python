"""v1 transports (oep-spec oep-core §3): a serial port, a USB vendor bulk / HID pair, or a local TCP connection (a
host-side broker), all under one Host.

Framing follows the kind of transport, never the VID:PID. A serial port (USB CDC, USB-Serial/JTAG, a UART bridge) is
COBS + CRC-16 sent as 0x00 <COBS> 0x00, and the probe's raw bytes (a target's console, its bind) share the line: every
span between 0x00s (and from the open to the first 0x00) is a candidate, and one that does not decode, fails its CRC or
answers another request is noise, skipped without a resend (oep-core §3.1, host guide §1.6). A missing answer is seen
by the timeout only, and the request goes once more with the same corr. Vendor bulk, HID and TCP are length(u16) message;
lost boundaries there are recovered by the §5.1 resync: on a result for another request, an impossible length or a
frame that stops half way, read and discard until the input is quiet for 50 ms, prove the link with a confirm, and go
on. When pushes keep the input from going quiet, the host's unsubscribe and end (harmless twice) are sent blind.

A request is sent once more after a missing (or, on length frames, broken) reply, with the same corr: the probe keeps
the lock holder's recent results and answers the repeat from them, so a state-changing request is not run twice
(oep-core §5.2).

A serial port is opened exclusively (host guide §2): pyserial's `exclusive=True` (flock, advisory) and, on Linux and
macOS, TIOCEXCL, so a second open fails at once (EBUSY) instead of sharing the answers. The port opens with pyserial's
defaults - DTR and RTS asserted - which reset none of the measured probes (host guide §1). No sleep after open. The port
asks for the driver's low-latency mode (pyserial `set_low_latency_mode`; an FTDI's latency timer 16 -> 1 ms tripled the
throughput of a UART bridge, oep-spec docs/uart-speed-negotiation.ja.md §3b), where the driver has it.

port_speed (oep-core §3.5 is the handshake; the host's procedure is the host guide §7, followed here): `raise_speed`
(or `open_host(..., port_speed=...)`) asks a probe that declares it for a faster rate on the UART bridge this host
opened. The minimal form (§7.2, the default): try a candidate, switch to the requested baud, settle 20 ms, confirm
(100 ms, 3 tries), commit - about 50 ms, no measurement. The full form (`verify=True`, §7.3): a baseline at the boot
speed (this session's frames, or 60 per flow), then per candidate every flow the caller will use (`flows`: in = link_source,
out = link_sink, duplex = both, each with its in-flight n) for 16 frames at max_frame - 16, failing a flow on broken +
lost >= 3 and a ratio over max(2 x baseline, 5 %), once more at n = 1 before giving up on it (then n = 1 is the cap),
commit when every flow passed. A failed candidate: revert (step 2, at the new rate), the boot speed, confirms up to
port_speed_idle_max_ms + 1 s. The report (`link.speed`) records the baseline, every candidate's flows and the step
downs. In use: the first period at a committed rate is its probation (32 KiB both ways and 1 s), judged as the verify
judges a flow - 3 or more broken or lost over max(2 x baseline, 5 %), or a missed answer, is a verify failure and
steps down at once; after it the frames of the last 3 s (none judged under 50) over max(2 x baseline, 10 %) broken or
lost step the link down. A step down: revert at the raised rate, the boot speed, a confirm, then the next lower
candidate of that raise_speed call that has not failed in this session gets a fresh try -> confirm -> verify ->
commit (none left: the boot speed); a rate that broke is not used again in the session, nor any above it. A request
whose answer never comes at the raised rate takes the link back to the boot speed, confirmed within that same bound,
steps down the same way and goes once more (never again at the raised rate; no confirm = ConnectionError). A revert's
or an end's answer puts the link back at the boot speed at once (obligation 6). The probe also goes back after idle_ms (at
most 3 s) with no good frame, so while raised the link sends a keepalive before a request when it has been quiet for
less than half of idle_ms (1 s), and `keep_alive()` does the same for a caller that sits idle for long. A host
opening a serial port retries its first confirm for that maximum and a little (4 s in all): a host that raised the
speed and died leaves the probe at its rate until then. `record=True` keeps passed / failed rates per (port, unit_id)
in `speed_record` (a pass 30 days, a failure 1 day; a failure within 2 s of a breakdown at another rate is unknown)
and puts passed rates first, failed ones out (all failed: the slowest is tried once); `max_tries` bounds the
candidates one call tries.
"""

from __future__ import annotations

import atexit
import collections
import select
import socket
import struct
import time
import weakref
from typing import Callable
from dataclasses import dataclass, field

import serial

from . import cobs, host as _host, message as m, registry as reg
from .frames import FramingLost, LengthFrames

RESYNC_QUIET_S = reg.TIMING["resync_quiet_ms"] / 1000
USB_VID, USB_PID = 0x303A, 0x0002   # the reference P4 probe's: the board's default, a temporary USB ID (probe guide §3.8)
# The project's own USB VID:PID pairs (core §3.3): the only automatic identification of an OEP probe. The registry lists
# them once obtained; none yet, so this stays empty and nothing is identified automatically.
PROJECT_VID_PIDS: tuple[tuple[int, int], ...] = ()
# Temporary clues until the project's VID:PID exists (host guide §1.7, not normative; gone once it does). A device that
# fits one is only a candidate: it is probed by the confirm-only rule (SerialLink.probe) before anything else is sent.
TEMPORARY_IPRODUCT_PREFIX = "OEP"
TEMPORARY_VENDOR_INTERFACE = (reg.USB["vendor_bulk_class"], reg.USB["vendor_bulk_subclass"],
                              reg.USB["vendor_bulk_protocol"])
TEMPORARY_HID_USAGE_PAGE = reg.USB["hid_usage_page"]
PROBE_WAIT_S = reg.TIMING["host_wait_add_ms"] / 1000   # the probing rule's wait for confirm's answer (core §3.3, §4.4)


class CorrMismatch(FramingLost):
    """A result for a request other than the one waited for."""


class NotOepProbe(ConnectionError):
    """A device or port this host had not identified gave no valid confirm answer (core §3.3 probing rule): it was
    closed and nothing else was sent to it."""


class UnitIdMismatch(ConnectionError):
    """The device found by the named unit_id (its USB serial) says another unit_id in describe (core §3.3): closed."""


class PortBusy(OSError):
    """Another program holds the serial port (it was opened exclusively): only one host at a time on a serial port."""


BASE_BAUD = 115200   # the boot speed of every reference UART bridge (the board's profile decides; core §3.5)
IDLE_MAX_MS = reg.TIMING["port_speed_idle_max_ms"]   # a committed rate goes back after this with no good frame (§3.5)
KEEPALIVE_S = 1.0    # raised: a keepalive once the link has been quiet this long (under half of idle_ms, core §3.5 ob. 4)
OPEN_RETRY_S = IDLE_MAX_MS / 1000 + 1.0   # port_speed_idle_max_ms + 1 s: the confirm bound at the boot speed (ob. 5 and 7)
OPEN_TRY_S = 0.5     # each of those confirms waits this long (at most the link's timeout)
IN_USE_WINDOW_S = 3.0      # raised, in use: the frames of the last 3 s are judged (host guide §7.3.2 item 4) ...
IN_USE_MIN_FRAMES = 50     # ... none under this many in the window ...
IN_USE_FLOOR = 0.10        # ... broken + lost over max(2 x baseline, this) steps down for the rest of the session
STEP_DOWN_WAIT_S = 0.2   # the step down's revert (step 2) at the raised rate waits this long, never sent again
RAISED_WAIT_MIN_S = 0.3  # raised, in use: each wait for an answer is a quarter of the lease, at least this
EXPECT_MARGIN_S = 0.5    # a request that may take long on the probe (Host.expecting): waited that long and this
LINK_ERRORS = (cobs.CorruptFrame, TimeoutError, FramingLost)


def open_serial(port: str, baud: int = BASE_BAUD):
    """The port opened exclusively (flock and TIOCEXCL): a second open by anyone fails with PortBusy. The driver's
    low-latency mode on where it has one (the FTDI latency timer 16 -> 1 ms; a pty or a driver without it: ignored)."""
    try:
        stream = _ExclusiveSerial(port, baud, timeout=0.05, exclusive=True)
    except serial.SerialException as e:
        if "busy" in str(e).lower() or "lock" in str(e).lower() or getattr(e, "errno", None) == 16:
            raise PortBusy(f"{port} is open in another program: {e}") from e
        raise
    try:
        stream.set_low_latency_mode(True)
    except Exception:                               # not on this platform / driver (ValueError, OSError, ...)
        pass
    try:
        import fcntl
        import termios
        fcntl.ioctl(stream.fileno(), termios.TIOCEXCL)
    except (ImportError, AttributeError, OSError):
        pass                                        # Windows opens a COM port exclusively by itself
    _open_serials.add(stream)
    return stream


def _exclusive_off(stream) -> None:
    try:
        import fcntl
        import termios
        fcntl.ioctl(stream.fileno(), termios.TIOCNXCL)   # exclusive mode off: the next opener gets the port
    except Exception:
        pass


class _ExclusiveSerial(serial.Serial):
    """A port whose every close turns TIOCEXCL off first - SerialLink.close, a garbage-collected stream, the exit. A
    real port's tty clears TIOCEXCL at its last close by itself; a pty slave's tty lives on while its master is open,
    so a flag left set would refuse every later opener (EBUSY)."""

    def close(self) -> None:
        _open_serials.discard(self)
        if self.is_open:
            _exclusive_off(self)
        super().close()


_open_serials: "weakref.WeakSet" = weakref.WeakSet()   # every port open_serial opened, until it is closed


@atexit.register
def _close_serials_at_exit() -> None:
    """A program that ends without closing its link (Host.end() ends the session only) still lets the port go."""
    for stream in list(_open_serials):
        try:
            stream.close()
        except Exception:
            pass


class TcpStream:
    """A TCP connection shaped like the part of a pyserial port the link uses (read with a timeout, write,
    in_waiting): a local broker in the spec's TCP form, length(u16) message (oep-core §3.1)."""

    def __init__(self, host: str, port: int, connect_timeout: float = 3.0):
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.timeout = 0.05
        self._buf = bytearray()

    @property
    def in_waiting(self) -> int:
        self._pull(0)
        return len(self._buf)

    def _pull(self, wait: float) -> None:
        ready, _, _ = select.select([self.sock], [], [], wait)
        if ready:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("the TCP peer closed the connection")
            self._buf += data

    def read(self, n: int = 1) -> bytes:
        if not self._buf:
            self._pull(self.timeout or 0)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def write(self, data: bytes) -> int:
        self.sock.sendall(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self._buf.clear()
        while select.select([self.sock], [], [], 0)[0]:
            if not self.sock.recv(65536):
                break

    def close(self) -> None:
        self.sock.close()


class SerialLink:
    NOISY_S = 1.0          # a resync that is still not quiet after this sends the blind stops

    def __init__(self, port: str, timeout: float = 3.0, baud: int = BASE_BAUD):
        self._setup(open_serial(port, baud), "cobs", timeout)
        self.port_path = port

    @classmethod
    def on_stream(cls, stream, framing: str = "length", timeout: float = 3.0) -> SerialLink:
        """A link on any pyserial-shaped stream: "cobs" for a serial port (a pty, a scripted stream), "length" for a
        USB bulk pair, HID or TCP."""
        lk = cls.__new__(cls)
        lk._setup(stream, framing, timeout)
        name = getattr(stream, "name", None)
        lk.port_path = name if isinstance(name, str) else None
        return lk

    def _setup(self, stream, framing: str, timeout: float) -> None:
        self.stream = stream
        self.framing = framing
        self.timeout = timeout
        self.resend = True                         # a lost reply: the request once more with the same corr (core §5.2)
        self.retries = 0
        self.inflight_cap = 0                      # port_speed: the in-flight requests the raised rate verified with
        self.answer_burst = self.ANSWER_BURST_MAX  # serial ports: answer bytes in flight at most (0: no bound)
        self.corrupt = 0
        self.stale = 0                             # replies that answered another request
        self.noise = 0                             # serial ports: bytes that were not a frame (the probe's raw side)
        self.resyncs = 0
        self.ended_blind = False
        self.dropped = 0                           # probe-initiated frames of a role this client does not handle
        self.pushes: collections.deque[bytes] = collections.deque()   # role 0x06 frames, oldest first
        self.events: collections.deque[bytes] = collections.deque()   # role 0x05 frames, oldest first
        self.base_baud = getattr(stream, "baudrate", None)   # serial ports: the boot speed every revert goes back to
        self.baud = self.base_baud                 # the rate the host side runs at now
        self.speed: SpeedReport | None = None      # port_speed: the last raise_speed's report (rate in force, KB/s)
        self.speed_lost = 0                        # times a raised rate was found gone (back to the boot speed)
        self.fallback = True                       # a raised rate in use (not raise_speed's own trial): fall back
        self.window: collections.deque[tuple[float, bool]] = collections.deque()   # raised, in use: (when, bad) per frame
        self.baseline_ratio = 0.0                  # raised, in use: the boot speed's ratio the threshold doubles
        self.base_counts = {"good": 0, "broken": 0, "lost": 0}   # this session's frames at the boot speed (the baseline)
        self.keepalive_s = KEEPALIVE_S             # raised: a keepalive once quiet this long (set from idle_ms at a commit)
        self.step_due = ""                         # raised, in use: why the link steps down at the next safe point
        self.step_ratio: float | None = None       # ... the window's ratio that decided it
        self.record = None                         # a speed_record.SpeedRecord and its key, once raise_speed used one
        self.record_key: tuple[str, str] | None = None
        self.speed_port: int | None = None         # the transport index the raised rate is on (the revert names it)
        self.unusable: dict[int, str] = {}         # rates that broke in use in this session -> why (none at or above again)
        self.failed: dict[int, str] = {}           # every rate the line failed in this session (a step down goes below)
        self.probation: Probation | None = None    # raised, in use: the first period at a new rate (guide §7.3.2 item 4)
        self.speed_plan: SpeedPlan | None = None   # the candidates a step down in use may go to (the lower ones)
        self.broke_at: float | None = None         # when the link was last back after a breakdown (monotonic) ...
        self.broke_rate: int | None = None         # ... at this rate (settle_s: results soon after are unknown)
        self.unusable_session: int | None = None   # the session `unusable` belongs to
        self.session_frame = None                  # (op, payload) -> a core request in the session, once bound
        self.lease_s = lambda: None                # the session's lease, once bound (raised: bounds every wait)
        self.expected_s = lambda: 0.0              # how long the request going out may take on the probe (Host.expecting)
        self._own = 0                              # >0: the link's own short requests (confirms, keepalive, revert)
        self.corr_source = self._own_corr          # the host's correlation counter once bound (open_host)
        self.keepalive_frame = None                # raised: a keepalive request in the session, once bound (attach_host)
        self.last_tx = time.monotonic()            # when the link last wrote (raised: quiet for KEEPALIVE_S = keepalive)
        self.held = lambda: False                  # a session holds the port (its raw transfer stopped): broken = resend
        self.blind = lambda: []                    # the host's blind stops (unsubscribe / end) once bound
        self._corr_n = 0x8000
        if self.framing == "length":
            self.frames = LengthFrames(self.stream)
            self.frames.discard_input()
        else:
            self._buf = bytearray()
            self.stream.reset_input_buffer()

    def _own_corr(self) -> int:
        self._corr_n = self._corr_n % 0xFFFF + 1
        return self._corr_n

    # ---- one frame each way ----------------------------------------------------------------------
    def _write(self, messages: list[bytes]) -> None:
        if self.framing == "length":
            self.frames.send_many(messages)
        else:
            data = b"".join(cobs.frame(msg) for msg in messages)
            self.stream.write(data)
            self._moved(len(data))
        self.last_tx = time.monotonic()

    def _recv(self) -> bytes:
        if self.framing == "length":
            reply = self.frames.recv(self._wait())
            if reply is None:
                raise TimeoutError("no result from the probe")
            return reply
        deadline = time.monotonic() + self._wait()
        while True:
            end = self._buf.find(0)
            if end >= 0:
                raw = bytes(self._buf[:end])
                del self._buf[:end + 1]              # the 0x00 also starts the next candidate
                if not raw:
                    continue
                try:
                    frame = cobs.unframe(raw)
                except cobs.CorruptFrame:
                    self.noise += len(raw)           # raw bytes of the port, or a broken frame: noise, no resend
                    if self.held():
                        # a session holds this port: the probe sends no raw bytes on it (oep-core §3.4), so this was a
                        # broken frame - most likely the reply (§5.2). Send again now instead of waiting out the
                        # timeout (a second waited 1 s each on an M5Stack ATOM's FTDI, 2026-10-01); a repeat is
                        # answered from the probe's retry table
                        self._count("broken")
                        raise
                    continue
                self._moved(len(raw) + 2)
                self._count("good")                  # a result or a notification that decoded (host guide §7.3.2)
                return frame
            if time.monotonic() > deadline:
                raise TimeoutError("no result from the probe")
            self._buf += self.stream.read(max(1, self.stream.in_waiting))

    # ---- requests --------------------------------------------------------------------------------
    @staticmethod
    def _corr(message: bytes) -> int:
        return message[1] | message[2] << 8          # request and result both carry it right after the role byte

    def _route(self, frame: bytes) -> bool:
        """Frames that are not results: data pushes and events are kept, other roles dropped. True if `frame` was one
        of them. Only a result (role 0x02) carries a correlation id; matching anything else by its bytes 1-2 would take
        a push for a reply whenever its fn happened to equal the id."""
        if frame and frame[0] == m.ROLE_RESULT:
            return False
        if frame and frame[0] == m.ROLE_DATA:
            self.pushes.append(frame)
        elif frame and frame[0] == m.ROLE_EVENT:
            self.events.append(frame)
        else:
            self.dropped += 1
        return True

    def _recv_for(self, corr: int) -> bytes:
        """The reply to request `corr` (pushes and events routed on the way). Length frames: a result for another
        request raises CorrMismatch (the §5.1 resync follows); a serial port: it is read past (oep-core §11.1)."""
        while True:
            reply = self._recv()
            if self._route(reply):
                continue
            if len(reply) >= 3 and self._corr(reply) == corr:
                return reply
            self.stale += 1
            if self.framing == "length":
                raise CorrMismatch(f"a result for correlation {self._corr(reply) if len(reply) >= 3 else None}, "
                                   f"waiting for {corr}")

    def pump(self, timeout: float = 0.0, until_one: bool = False) -> int:
        """Read the frames that arrive within `timeout` in total (pushes are kept, stray results counted stale); a
        probe that keeps pushing cannot hold this past the deadline. `until_one`: return as soon as a frame was read.
        -> frames read."""
        saved = self.timeout
        deadline = time.monotonic() + timeout
        n = 0
        self._own += 1                             # its reads wait what it says, whatever a caller expects
        try:
            while True:
                if self._raised():                 # a long pump at a raised rate keeps the line alive (core §3.5)
                    self.timeout = saved
                    self._keep_raised()
                self.timeout = max(0.0, deadline - time.monotonic())
                if self._raised():
                    self.timeout = min(self.timeout, self.keepalive_s)
                try:
                    frame = self._recv()
                except TimeoutError:
                    if self._raised() and time.monotonic() < deadline:
                        continue
                    return n
                except FramingLost:
                    self.timeout = saved
                    self.resync()
                    return n
                except cobs.CorruptFrame as e:
                    self.corrupt += 1
                    self._strike(e)
                    continue
                n += 1
                if not self._route(frame):
                    self.stale += 1
                if until_one or time.monotonic() >= deadline:
                    return n
        finally:
            self.timeout = saved
            self._own -= 1
            self._step_down_if_due()

    def resync(self, tries: int = 3) -> None:
        """oep-core §5.1: read and discard until the input is quiet for 50 ms, then prove the link with a confirm (a read,
        safe to send), then go on. When the input does not go quiet (pushes keep coming), the host's unsubscribe and end go
        out blind, once (and send() then does not send its request again)."""
        self.resyncs += 1
        if self.framing != "length":
            return                                  # COBS frames carry their own boundaries: nothing to find again
        blind_sent = False
        for _ in range(tries):
            while not self.frames.discard_until_quiet(RESYNC_QUIET_S, self.NOISY_S):
                if blind_sent:
                    raise ConnectionError("resync: the input never went quiet, even after unsubscribe and end")
                stops = self.blind()
                blind_sent = True
                if stops:
                    self._write(stops)
                    self.ended_blind = True     # the session ended: a request sent again would only meet no_session
            corr = self.corr_source()
            self._write([m.Request(corr, m.CORE_FN, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([0, 0xFF])).pack()])
            try:
                while True:
                    reply = self._recv()
                    if self._route(reply):
                        continue
                    if len(reply) >= 3 and self._corr(reply) == corr:
                        return                  # any result with our correlation proves the boundaries again
            except (FramingLost, TimeoutError):
                continue
        raise ConnectionError(f"resync: no confirm came back in {tries} tries")

    def _recover(self) -> None:
        """After a lost, broken or missing reply: leave the link in step for the next request. A serial port needs
        nothing (a late answer is read past by its corr). While probing (core §3.3) there is no resync: its confirms
        would be more than the one resend the probing rule allows."""
        if self.framing == "length" and not getattr(self, "_probing", False):
            self.resync()

    def send(self, message: bytes) -> bytes:
        self._keep_raised()
        try:
            reply = self._send(message)
        except LINK_ERRORS as e:
            if not self._speed_fallback(e):
                raise
            reply = self._send(message)            # once more at the boot speed (the probe answers a repeat from what it kept)
        if self.baud != self.base_baud and self._reverts(message, reply):
            self.set_baud(self.base_baud)          # the probe went back right after this answer (core §3.5 ob. 6)
            if self.speed is not None:
                self.speed.rate, self.speed.chosen = self.base_baud, None
        if self._core_completed(message, reply) == m.OP_OPEN:
            self.base_counts = {"good": 0, "broken": 0, "lost": 0}   # a new session: its baseline starts here
            self.window.clear()
        self._step_down_if_due()
        return reply

    @staticmethod
    def _core_completed(message: bytes, reply: bytes) -> int | None:
        """The op of a core request whose answer is completed (any outcome); None for anything else."""
        if len(message) < 6 or len(reply) < 4 or reply[3] != m.COMPLETED or message[3] | message[4] << 8 != m.CORE_FN:
            return None
        return message[5]

    @classmethod
    def _reverts(cls, message: bytes, reply: bytes) -> bool:
        """A completed end, or port_speed's revert: the probe is back at its boot speed once this answer is out."""
        op = cls._core_completed(message, reply)
        if op == m.OP_END:
            return True
        at = 6 + (4 if message[0] & m.ROLE_SESSION else 0) + 5     # port(u8) baud(u32) step(u8)
        return op == OP_PORT_SPEED and len(message) > at and message[at] == SPEED_STEP["revert"]

    def _send(self, message: bytes) -> bytes:
        corr = self._corr(message)
        self.ended_blind = False
        for attempt in (0, 1):
            self._write([message])
            try:
                return self._recv_for(corr)
            except (cobs.CorruptFrame, TimeoutError, FramingLost) as e:
                if isinstance(e, cobs.CorruptFrame):
                    self.corrupt += 1
                self._strike(e)
                self._recover()
                if attempt or not self.resend or (message[0] & m.ROLE_SESSION and self.ended_blind):
                    raise
                # sent once more with the same corr, state-changing ones too: the probe keeps the lock holder's recent
                # results and answers a repeat from them instead of running it twice (v1-open-proposals §4). A result
                # too large to keep comes back rejected result_lost: the caller reads the state again.
                self.retries += 1

    def exchange(self, messages: list[bytes], max_inflight: int, window_bytes: int) -> list[bytes]:
        """Pipelined: keep up to max_inflight requests and window_bytes outstanding, results in order.

        The probe answers in the order it received; each reply is still matched by correlation id. Frames admitted
        together go out in one write (E160). After a broken or missing reply the link resyncs and the requests not yet
        answered go once more with the same corr (oep-core §5.2: the probe answers a repeat from what it kept); a second
        failure is raised.
        """
        self._keep_raised()
        replies: list[bytes] = []
        try:
            out = self._exchange_once(messages, max_inflight, window_bytes, replies)
        except LINK_ERRORS as e:
            self._strike(e)
            if self.ended_blind or not self.resend:
                raise
            self.retries += 1
            rest = messages[len(replies):]
            more: list[bytes] = []
            try:
                out = replies + self._exchange_once(rest, max_inflight, window_bytes, more)
            except LINK_ERRORS as e2:
                self._strike(e2)
                if not self._speed_fallback(e2):
                    raise
                rest = rest[len(more):]
                out = replies + more + self._exchange_once(rest, max_inflight, window_bytes, [])
        self._step_down_if_due()
        return out

    # ---- port_speed (core §3.5) ------------------------------------------------------------------
    def set_baud(self, rate: int, fallback: int | None = None) -> int:
        """The host side of the serial port to `rate` (pyserial's baudrate), what was read so far dropped. The host
        switches to the baud it asked for; `fallback` (the probe's answer, the rate it really makes) is set only when
        the OS refuses `rate` (core §3.5 obligation 2). -> the rate set."""
        if hasattr(self.stream, "baudrate"):
            try:
                self.stream.baudrate = rate
            except (ValueError, OSError, serial.SerialException):
                if fallback is None or fallback == rate:
                    raise
                self.stream.baudrate = fallback
                rate = fallback
        self.baud = rate
        if self.framing == "cobs":
            time.sleep(SWITCH_SETTLE_S)               # the probe switches once its answer is out: let both ends settle
            self._buf.clear()
            self.stream.reset_input_buffer()
        return rate

    def confirm_raw(self, timeout: float) -> bool:
        """A confirm straight on the link (not through the host): True when its answer came within `timeout`. On a held
        serial port a broken frame is normally the awaited answer (§5.2), but a confirm sent to re-sync a measurement
        reads past the broken leftovers of the lost frames it follows: it keeps reading until its own answer or the
        deadline, not giving up on the first broken one."""
        corr = self.corr_source()
        self._write([m.Request(corr, m.CORE_FN, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([0, 0xFF])).pack()])
        saved = self.timeout
        deadline = time.monotonic() + timeout
        self._own += 1
        try:
            while True:
                self.timeout = max(0.0, deadline - time.monotonic())
                try:
                    self._recv_for(corr)
                    return True
                except cobs.CorruptFrame:
                    if time.monotonic() >= deadline:
                        return False                   # the leftovers never cleared within the window
                except (TimeoutError, FramingLost):
                    return False
        finally:
            self.timeout = saved
            self._own -= 1

    def _confirm_within(self, wait_s: float, each_s: float) -> bool:
        """Confirms, each waiting `each_s`, until one is answered (True) or `wait_s` has passed (False)."""
        deadline = time.monotonic() + wait_s
        while True:
            if self.confirm_raw(each_s):
                return True
            if time.monotonic() >= deadline:
                return False

    def back_to_base(self, wait_s: float = OPEN_RETRY_S) -> bool:
        """The host at the boot speed again, and the probe confirmed there: confirms every 0.25 s up to `wait_s`
        (default port_speed_idle_max_ms + 1 s, core §3.5 obligation 5; a probe still trying waits out its verify_ms,
        one committed reverts at the broken candidates these make - 3 in a row)."""
        self.inflight_cap = 0
        if self.base_baud is None:
            return False
        self.set_baud(self.base_baud)
        return self._confirm_within(wait_s, 0.25)

    def _raised(self) -> bool:
        return self.base_baud is not None and self.baud != self.base_baud

    def keep_alive(self) -> bool:
        """While a raised rate is in force and a session holds the port: a keepalive when the link has been quiet for
        `keepalive_s` (1 s, and under half of the committed idle_ms: core §3.5 obligation 4). The probe goes back to
        the boot speed after idle_ms (at most 3 s) with no good frame; every request already does this before it goes
        out, so only a caller that sits idle for long (waiting on a person, a sleep between requests) calls it - often
        is fine, it sends nothing otherwise. True when a keepalive went out."""
        if not self._raised() or self.keepalive_frame is None or not self.held():
            return False
        if time.monotonic() - self.last_tx < self.keepalive_s:
            return False
        self.last_tx = time.monotonic()            # before sending: send() asks again and must not recurse
        self._own += 1
        try:
            self.send(self.keepalive_frame())
        finally:
            self._own -= 1
        return True

    def _keep_raised(self) -> None:
        """keep_alive before a request; a keepalive that fails is left to the request itself to find out."""
        try:
            self.keep_alive()
        except (TimeoutError, cobs.CorruptFrame, FramingLost):
            pass

    def _in_use(self) -> bool:
        """A raised rate in force outside raise_speed's own trial (where every failure is handled there)."""
        return self.fallback and self._raised()

    def _wait(self) -> float:
        """How long one answer is waited for: the link's timeout; raised and in use, at most a quarter of the
        session's lease (at least RAISED_WAIT_MIN_S) - a probe that went back by itself (broken candidates, core §3.5
        item 5) hears nothing at the raised rate, and the fallback (both waits, the confirm at the boot speed, the
        request again there) must end well inside the lease. A request that may take longer on the probe
        (Host.expecting: a run's timeout_ms, a dmi list's waits, ...; the probe does not count the lease meanwhile,
        core §6.1) waits at least that and EXPECT_MARGIN_S, at any rate; the link's own requests never do."""
        wait = self.timeout
        if self._in_use():
            lease = self.lease_s()
            if lease is not None:
                wait = min(self.timeout, max(RAISED_WAIT_MIN_S, lease / 4))
        expected = 0.0 if self._own else self.expected_s()
        return max(wait, expected + EXPECT_MARGIN_S) if expected > 0 else wait

    def _strike(self, e: Exception) -> None:
        """A request's answer did not come (`e`): a lost frame (host guide §7.3.2: counted at the boot speed for the
        baseline, raised and in use for the window). Broken frames are counted where they are read (`_recv`)."""
        if isinstance(e, TimeoutError):
            self._count("lost")

    def _count(self, kind: str) -> None:
        """One frame the host side saw while a session holds the port: `kind` good / broken / lost. At the boot speed
        it goes into this session's baseline (`base_counts`); raised and in use into the 3 s window, judged on every
        bad one (host guide §7.3.2 item 4): IN_USE_MIN_FRAMES or more in the window and a ratio over
        max(2 x baseline, IN_USE_FLOOR) make the link step down at the next safe point. While the rate is in its
        probation (`probation`), its frames are also judged as the verify judges a flow: FLOW_FAIL_MIN or more broken
        or lost and a ratio over max(2 x baseline, VERIFY_FLOOR) step down at once (a verify failure, not an in-use
        one); a good frame once probation_bytes have moved and probation_s have passed ends the probation."""
        if self.base_baud is None or not self.held():
            return
        if self.baud == self.base_baud:
            self.base_counts[kind] += 1
            return
        if not self._in_use():
            return
        now = time.monotonic()
        p = self.probation
        if p is not None and p.rate == self.baud:
            p.frames += 1
            if kind != "good":
                p.bad += 1
                ratio = p.bad / p.frames
                if not self.step_due and p.bad >= FLOW_FAIL_MIN and ratio > p.threshold:
                    self.step_ratio = ratio
                    self.step_due = (f"in probation: {p.bad} of {p.frames} frames broken or lost at {self.baud} after "
                                     f"{p.moved} bytes ({ratio:.1%}, over {p.threshold:.0%})")
            elif p.moved >= p.bytes and now - p.started >= p.seconds:
                self._probation_passed(p)
        self.window.append((now, kind != "good"))
        while self.window and now - self.window[0][0] > IN_USE_WINDOW_S:
            self.window.popleft()
        if kind == "good" or self.step_due or len(self.window) < IN_USE_MIN_FRAMES:
            return
        bad = sum(1 for _, b in self.window if b)
        ratio, threshold = bad / len(self.window), max(2 * self.baseline_ratio, IN_USE_FLOOR)
        if ratio > threshold:
            self.step_ratio = ratio
            self.step_due = (f"{bad} of {len(self.window)} frames broken or lost within {IN_USE_WINDOW_S:g} s at "
                             f"{self.baud} ({ratio:.1%}, over {threshold:.0%})")

    def _moved(self, n: int) -> None:
        """Bytes on the line at a raised rate in use (both ways): the probation counts them."""
        p = self.probation
        if p is not None and self._in_use() and p.rate == self.baud:
            p.moved += n
            p.trial.probation_bytes = p.moved

    def _probation_passed(self, p: Probation) -> None:
        self.probation = None
        p.trial.probation = "passed"
        p.trial.probation_bytes = p.moved
        if self.record is not None and self.record_key is not None:
            self.record.note(*self.record_key, p.rate, passed=True, phase="probation")

    def _step_down_if_due(self) -> None:
        if self.step_due and self._in_use():
            self._step_down(self.step_due)

    def _step_down(self, why: str) -> None:
        """Leave the raised rate: port_speed step 2 at it (STEP_DOWN_WAIT_S, never sent again: a probe that already
        went back cannot hear it, and a committed one reverts at the broken candidates the confirms make), the host at
        the boot speed, a confirm there (ConnectionError when none is answered), then the next lower candidate
        (`_step_lower`)."""
        rate = self.baud
        self.step_due = ""
        self.window.clear()
        if self.session_frame is not None and self.speed_port is not None:
            saved = self.timeout, self.resend, self.fallback
            self.timeout, self.resend, self.fallback = STEP_DOWN_WAIT_S, False, False
            self._own += 1
            try:
                self._send(self.session_frame(OP_PORT_SPEED, struct.pack("<BIBHI", self.speed_port, 0,
                                                                         SPEED_STEP["revert"], 0, 0)))
            except LINK_ERRORS:
                pass
            finally:
                self.timeout, self.resend, self.fallback = saved
                self._own -= 1
        if not self.back_to_base():
            raise ConnectionError(f"the probe answers neither at {rate} nor at the boot speed {self.base_baud}")
        self._stepped(rate, why)
        self._step_lower()

    def _stepped(self, rate: int, why: str) -> None:
        """`rate` broke (in its probation or later in use): not used again in this session, nor anything at or above it
        (raise_speed and the step down skip them); the report and the record say so. A probation's failure is a verify
        failure (phase "probation"), a later one an in-use failure (phase "in_use"); either measured within settle_s
        of a breakdown at another rate is written unknown."""
        p, self.probation = self.probation, None
        in_probation = p is not None and p.rate == rate
        if in_probation and not why.startswith("in probation"):
            why = f"in probation: {why}"
        self.unusable[rate] = self.failed[rate] = why
        if self.speed is not None:
            self.speed.rate, self.speed.chosen = self.base_baud, None
            self.speed.stepped_down, self.speed.down_why = True, why
            self.speed.step_downs.append(StepDown(time.time(), rate, why, self.step_ratio, self.base_baud,
                                                  in_probation))
        if in_probation:
            p.trial.probation = "failed"
        self.step_ratio = None
        if self.record is not None and self.record_key is not None:
            self.record.note(*self.record_key, rate, passed=None if in_probation and p.settling else False,
                             phase="probation" if in_probation else "in_use")
        self.broke_at, self.broke_rate = time.monotonic(), rate

    def _step_lower(self) -> None:
        """After a breakdown in use, back at the boot speed: the next lower candidate of the last raise_speed's plan
        that has not failed in this session (below every rate that has) gets a fresh try -> confirm -> verify ->
        commit, the next after it if that fails; none left (or none passes): the boot speed for the rest of the
        session. The report's last step down says where the link went (`to`)."""
        plan = self.speed_plan
        if plan is None or plan.session != self.unusable_session or not self.unusable:
            return
        ceiling = min(self.unusable.keys() | self.failed.keys())
        lower = sorted({r for r in plan.rates if r < ceiling}, reverse=True)
        if lower:
            saved = self.fallback
            self.fallback = False                  # every failure there is handled there
            self._own += 1
            try:
                plan.go(lower)
            finally:
                self.fallback = saved
                self._own -= 1
        if self.speed is not None and self.speed.step_downs:
            self.speed.step_downs[-1].to = self.baud

    def _speed_fallback(self, e: Exception | None = None) -> bool:
        """A request failed (its resend too) while a raised rate was in use. Broken frames (the probe still answers
        at that rate) or a step down already due: step down (`_step_down`). No answer at all: the probe went back
        (idle_ms, broken candidates, a lapse) - back to the boot speed, confirmed, then the next lower candidate.
        Either way the rate (and anything above it) is not used again in this session. True = send again there."""
        if not self._in_use() or self.base_baud is None:
            return False
        rate = self.baud
        if self.step_due or not isinstance(e, TimeoutError):
            self._step_down(self.step_due or f"frames kept breaking at {rate} ({type(e).__name__})")
            return True
        if not self.back_to_base():
            raise ConnectionError(f"the probe answers neither at {rate} nor at the boot speed {self.base_baud}")
        self.speed_lost += 1
        self.step_due = ""
        self.window.clear()
        if self.speed is not None:
            self.speed.lost = True
        self._stepped(rate, f"no answer at {rate} (the probe went back by itself)")
        self._step_lower()
        return True

    def _exchange_once(self, messages: list[bytes], max_inflight: int, window_bytes: int,
                       replies: list[bytes]) -> list[bytes]:
        outstanding: list[tuple[int, int]] = []      # (correlation, size) of requests in flight
        batch: list[bytes] = []
        try:
            for msg in messages:
                size = len(msg) + 2
                while outstanding and (len(outstanding) >= max_inflight
                                       or sum(s for _, s in outstanding) + size > window_bytes):
                    if batch:
                        self._write(batch)
                        batch = []
                    replies.append(self._recv_for(outstanding.pop(0)[0]))
                batch.append(msg)
                outstanding.append((self._corr(msg), size))
            if batch:
                self._write(batch)
            while outstanding:
                replies.append(self._recv_for(outstanding.pop(0)[0]))
        except (cobs.CorruptFrame, TimeoutError, FramingLost) as e:
            if isinstance(e, cobs.CorruptFrame):
                self.corrupt += 1
            self._recover()
            raise
        return replies

    # A serial port on an OS CDC driver loses data when the probe's answers burst past what the driver buffers (Linux
    # cdc_acm, HS: 16 x 512 B URBs; 8 x 1008 B frames in flight lost 20-30 % on an ESP32-P4, 7 lost none - oep-spec
    # docs/link-measurements.ja.md §1.1). The expected answer bytes in flight stay under this on COBS links; 0 = no cap.
    ANSWER_BURST_MAX = 6144

    def inflight_for(self, limits: dict) -> int:
        """How many requests this link keeps in flight: the probe's max_inflight, the port_speed verify's cap, and on a
        serial port the answer-burst bound (ANSWER_BURST_MAX over a whole COBS frame of max_frame bytes)."""
        n = min(limits["max_inflight"], self.inflight_cap or 255)
        if self.framing == "cobs" and self.answer_burst and limits.get("max_frame"):
            n = min(n, max(1, self.answer_burst // cobs.frame_max(limits["max_frame"])))
        return max(1, n)

    def bind(self, limits: dict):
        """This link's exchange with the probe's limits (core confirm), for Host(exchange=...)."""
        return lambda msgs: self.exchange(msgs, self.inflight_for(limits), limits["window"])

    def attach_host(self, hst) -> None:
        """Bind to a host: its correlation counter and blind stops for the resync, and after a confirm, the probe's
        limits (in-flight, window, max_frame) for pipelining and the framing check."""
        self.corr_source = hst.next_corr
        self.blind = hst.blind_stop
        self.held = lambda: hst.session is not None
        self.session_frame = lambda op, payload: m.Request(hst.next_corr(), m.CORE_FN, op, payload, hst.session).pack()
        self.keepalive_frame = lambda: self.session_frame(m.OP_KEEPALIVE, b"")
        self.lease_s = lambda: hst.lease_ms / 1000 if hst.session is not None and hst.lease_ms else None
        hst.before_request = self._keep_raised      # the keepalive before the request's corr is taken (core §4.1)
        self.expected_s = lambda: hst.expect_ms / 1000
        hst.link = self
        limits = self.probe(hst)
        hst.exchange = self.bind(limits)
        if self.framing == "length" and limits.get("max_frame"):
            self.frames.max_frame = limits["max_frame"]

    def probe(self, hst) -> dict:
        """The probing rule (core §3.3): every device or port this client opens is one it has not identified (no
        project VID:PID is listed yet), so the first thing sent is a confirm, and nothing else until a valid answer
        (completed, the same corr, a payload starting OEP!) came back. None: the link is closed and NotOepProbe raised.
        Vendor bulk / HID: one confirm and its one §5.2 resend, each waiting PROBE_WAIT_S (1000 ms, §4.4). A serial
        port first runs wait_boot_speed's confirms (core §3.5 host obligation 7: repeated for port_speed_idle_max_ms +
        1 s at the boot speed, a previous host's raised rate running out), then the checked confirm. TCP (a host-side
        broker, which opens the probe itself) keeps the link's timeout. -> confirm's limits."""
        transport = getattr(self, "transport", None)
        saved = self.timeout, self.resend
        self._probing = True
        try:
            if self.framing == "cobs" and transport == "serial":
                self.wait_boot_speed()
            elif transport != "tcp":
                self.timeout, self.resend = PROBE_WAIT_S, True
            return hst.confirm()
        except Exception as e:
            self.timeout, self.resend = saved
            try:
                self.close()
            except Exception:
                pass
            where = self.port_path or transport or "the link"
            raise NotOepProbe(f"{where}: no valid confirm answer ({type(e).__name__}: {e}); closed, nothing else "
                              "sent (core §3.3)") from e
        finally:
            self.timeout, self.resend = saved
            self._probing = False

    def wait_boot_speed(self, wait_s: float | None = None) -> None:
        """A serial port just opened: confirm at the boot speed, retried for OPEN_RETRY_S (port_speed_idle_max_ms and
        a second; at least the link's timeout) - a host that raised the speed and died leaves the probe at that rate
        until its idle limit runs out (core §3.5 item 6). TimeoutError when none was answered."""
        wait_s = max(OPEN_RETRY_S, self.timeout) if wait_s is None else wait_s
        if not self._confirm_within(wait_s, min(self.timeout, OPEN_TRY_S)):
            raise TimeoutError(f"no answer to confirm at {self.baud} for {wait_s:.1f} s")

    def close(self) -> None:
        _exclusive_off(self.stream)                      # (a stream open_serial did not open: off here too)
        self.stream.close()


def open_usb_host(vid: int = USB_VID, pid: int = USB_PID, serial: str | None = None, timeout: float = 3.0,
                  transports: tuple[str, ...] = ("vendor", "hid")):
    """A Host on the probe's USB device (the P4's HS OTG port), trying its ways in in the oep-core §3.3 order: vendor bulk,
    then vendor-defined HID (when raw USB is not permitted or the probe offers no vendor interface). A CDC port is
    opened by path (open_host). Length-prefixed frames on all of them. The device is one the caller chose (or named),
    not one identified: each way in is probed by the confirm-only rule (SerialLink.probe); one that gives no valid
    answer is closed and the next tried. NotOepProbe when every opened way in failed that way."""
    from . import host
    errors, probed = [], []
    for kind in transports:
        try:
            stream = _open_usb_stream(kind, vid, pid, serial)
        except (OSError, FileNotFoundError, ImportError) as e:
            errors.append(f"{kind}: {e}")
            continue
        except Exception as e:                           # usb1.USBError / usb.core.USBError (access, busy)
            errors.append(f"{kind}: {type(e).__name__}: {e}")
            continue
        lk = SerialLink.on_stream(stream, "length", timeout)
        lk.transport = kind
        hst = host.Host(lk.send)
        try:
            lk.attach_host(hst)
        except NotOepProbe as e:
            errors.append(f"{kind}: {e}")
            probed.append(kind)
            continue
        return hst
    message = f"no way in to {vid:04x}:{pid:04x}: " + "; ".join(errors)
    if probed:
        raise NotOepProbe(message)
    raise FileNotFoundError(message)


def is_project_device(vid: int, pid: int) -> bool:
    """core §3.3: the only automatic identification of an OEP probe - the project's own USB VID:PID (PROJECT_VID_PIDS,
    from the registry once listed; empty now, so always False)."""
    return (vid, pid) in PROJECT_VID_PIDS


def temporary_clue(product: str | None = None, interfaces=(), hid_usage_pages=()) -> bool:
    """A temporary clue that a USB device may be an OEP probe, until the project's VID:PID exists (host guide §1.7; not
    normative, and removed once that VID:PID is listed): iProduct starting "OEP", a vendor interface class 0xFF /
    subclass 0x4F / protocol 0x45 (`interfaces`: (class, subclass, protocol) per interface), or a HID with usage page
    0xFF4F (`hid_usage_pages`). Never an identification: a candidate is opened and probed by the confirm-only rule
    (SerialLink.probe) before anything else goes to it."""
    return ((product or "").startswith(TEMPORARY_IPRODUCT_PREFIX)
            or any(tuple(i) == TEMPORARY_VENDOR_INTERFACE for i in interfaces)
            or TEMPORARY_HID_USAGE_PAGE in hid_usage_pages)


def check_unit_id(hst, unit_id: str) -> None:
    """A device opened by its named unit_id (core §3.3): after confirm, fn 0's describe must say that unit_id; otherwise
    the link is closed and UnitIdMismatch raised (nothing else is sent)."""
    from . import core
    said = None
    try:
        for tag, value in core.describe(hst, 0):
            if tag & 0x7F == reg.CORE.tlv["describe"]["unit_id"] and value:
                said = value.decode("ascii", "replace")
                break
    except Exception as e:
        hst.link.close()
        raise UnitIdMismatch(f"unit id {unit_id}: describe failed ({type(e).__name__}: {e}); closed") from e
    if said is None or said.lower() != unit_id.lower():
        hst.link.close()
        raise UnitIdMismatch(f"the device with USB serial {unit_id} says unit_id {said!r} in describe; closed")


def find_usb(unit_id: str) -> tuple[int, int]:
    """The VID:PID of the USB device whose serial number is `unit_id` (core §3.3, §7.5) - how a host that kept a probe's
    unit_id (oep://<unit_id>/<slot>, usb:<unit_id>) finds it again whatever VID:PID it enumerates with. The serial alone
    decides (no iProduct or class check): the device is a named one, opened without identification, probed by confirm
    and checked by describe's unit_id (open_host does both, check_unit_id)."""
    want = unit_id.lower()
    try:
        import usb1
        with usb1.USBContext() as ctx:
            for dev in ctx.getDeviceIterator(skip_on_error=True):
                try:
                    h = dev.open()
                except Exception:                            # no access to this one: not ours to judge
                    continue
                try:
                    if (h.getSerialNumber() or "").lower() == want:
                        return dev.getVendorID(), dev.getProductID()
                except Exception:
                    pass
                finally:
                    h.close()
    except ImportError:
        import usb.core
        import usb.util
        for dev in usb.core.find(find_all=True):
            try:
                serial = usb.util.get_string(dev, dev.iSerialNumber) or ""
            except Exception:
                continue
            if serial.lower() == want:
                return dev.idVendor, dev.idProduct
    raise FileNotFoundError(f"no USB device with serial number (unit id) {unit_id}")


def _open_usb_stream(kind: str, vid: int, pid: int, serial: str | None):
    if kind == "hid":
        from .hid_stream import open_hid
        return open_hid(vid, pid, serial)
    from .usb_stream import UsbAsyncStream, UsbBulkStream
    try:
        import usb1  # noqa: F401  python-libusb1: queued asynchronous IN transfers (streaming near the HS ceiling)
        return UsbAsyncStream.open(vid, pid, serial)
    except ImportError:
        return UsbBulkStream.open(vid, pid, serial)


TCP_TIMEOUT = 15.0   # behind a broker: outlast its own 3 s retry towards the probe, and never send again ourselves


def open_host(target: str, timeout: float | None = None, resend: bool | None = None, *, baud: int = BASE_BAUD,
              port_speed=None, flows=None, verify: bool | None = None, record=False, max_tries: int | None = None,
              lease_ms: int = 3000, owner: str | None = None):
    """A Host on `target`, with the link's pipelining bound to the probe's limits (core confirm): Host.pipeline and
    everything built on it (flash, capture reads) then keep several requests in flight.

    target: a serial port path (COM3 on Windows); tcp://HOST:PORT for a local broker (length frames); usb[:VID:PID[:SERIAL]]
    (hex) for the probe's USB device, vendor bulk then HID (oep-core §3.3); usb:UNIT_ID for the device whose USB serial
    is that unit id, whatever its VID:PID (fn 0's describe must then say the same unit_id, or it is closed:
    UnitIdMismatch). Every target is probed first by the confirm-only rule (core §3.3): no valid confirm answer, the link
    is closed and NotOepProbe raised (a serial port retries its confirms for port_speed_idle_max_ms + 1 s first).

    timeout: seconds to wait for a reply (default 3; TCP 15). resend: send a request once more with the same corr when
    its reply was lost (default on; off for TCP, where a broker retries towards the probe itself and a second copy from
    here would only race it - a lost reply is then raised to the caller).

    baud: a serial port's boot speed (the board's profile; every port_speed revert goes back to it). port_speed: the
    candidates to try in order (opt-in, core §3.5; True = raise_speed's default, 500000): the session is taken
    (core.take, `lease_ms`, `owner`) and left open for the caller - who goes on in it, never opening another (a new
    session would end this one, and the rate with it) - and `raise_speed(hst, candidates, flows=, verify=, record=,
    max_tries=)`
    runs (the minimal form unless `verify` / `flows` ask for the full one; `record` off by default); its report is
    `hst.link.speed`. A probe without port_speed, or a link that is not a serial port this host opened, stays at its
    speed (the report says why)."""
    from . import host
    if target.startswith("tcp://"):
        addr, _, port = target[len("tcp://"):].rpartition(":")
        lk = SerialLink.on_stream(TcpStream(addr or "127.0.0.1", int(port)), "length",
                                  TCP_TIMEOUT if timeout is None else timeout)
        lk.resend = False if resend is None else resend
        lk.transport = "tcp"
    elif target == "usb" or target.startswith("usb:"):
        timeout = 3.0 if timeout is None else timeout
        parts = target.split(":")[1:]
        if len(parts) == 1:                                   # usb:UNIT_ID (any length: a VID comes with its PID)
            vid, pid = find_usb(parts[0])
            hst = open_usb_host(vid, pid, parts[0], timeout)
            check_unit_id(hst, parts[0])
            return hst
        vid = int(parts[0], 16) if parts else USB_VID
        pid = int(parts[1], 16) if len(parts) > 1 else USB_PID
        return open_usb_host(vid, pid, parts[2] if len(parts) > 2 else None, timeout)
    else:
        lk = SerialLink(target, 3.0 if timeout is None else timeout, baud)
        lk.transport = "serial"
    if resend is not None:
        lk.resend = resend
    hst = host.Host(lk.send)
    lk.attach_host(hst)
    if port_speed:
        from . import core
        core.take(hst, lease_ms, owner=owner)
        raise_speed(hst, DEFAULT_CANDIDATES if port_speed is True else port_speed, flows=flows, verify=verify,
                    record=record, max_tries=max_tries)
    return hst


# ---- port_speed (core §3.5) -------------------------------------------------------------------------------------------
OP_PORT_SPEED = reg.CORE.op["port_speed"]
SPEED_STEP = reg.CORE.enum["port_speed_step"]
PORT_SPEED_TAG = reg.CORE.tlv["describe"]["port_speed"]
UNIT_ID_TAG = reg.CORE.tlv["describe"]["unit_id"]
UART_BRIDGE = reg.CORE.enum["transport_kind"]["uart_bridge"]


DEFAULT_CANDIDATES = (500000,)   # host guide §7.2: one candidate that passed the measured bridges in small duplex use
FLOWS = ("in", "out", "duplex")  # probe -> host (link_source), host -> probe (link_sink), both interleaved
VERIFY_MS = 2000           # the probe waits this long for the commit (guide §7.2 / §7.3.2: 2000)
CONFIRM_TRIES, CONFIRM_WAIT_S = 3, 0.1   # after the switch: confirm, 100 ms each, up to 3 (core §3.5 obligation 2)
FLOW_FRAMES = 16           # full form: at least this many frames per flow (guide §7.3.2 item 3-3)
FLOW_FAIL_MIN = 3          # ... a flow fails only on broken + lost of at least this ...
VERIFY_FLOOR = 0.05        # ... and a ratio over max(2 x baseline, this) (item 3-4)
BASELINE_FRAMES = 60       # the boot speed's ratio: this session's frames, or this many measured per flow (item 2)
BASELINE_MAX = 0.10        # a flow whose baseline is over this is measured again at n = 1; still over: not raised
SWITCH_SETTLE_S = 0.02     # after a baud change, before the first byte at the new rate (obligation 2: 20 ms or more)
PROBATION_BYTES = 32 * 1024   # in use, a new rate's first period (guide §7.3.2 item 4): this many bytes both ways ...
PROBATION_S = 1.0             # ... and this long since the commit, judged as the verify judges a flow
SETTLE_S = 2.0             # results measured this soon after a breakdown at another rate are not failures (unknown)


@dataclass
class FlowResult:
    """One flow run at one rate (guide §7.5 record): the flow, its in-flight n, frames, broken, lost, KB/s, pass."""
    flow: str
    n: int
    frames: int = 0
    broken: int = 0
    lost: int = 0
    kb_s: float = 0.0
    passed: bool = False
    gone: bool = False         # the probe stopped answering at this rate (it went back): the flow stopped there

    @property
    def ratio(self) -> float:
        return (self.broken + self.lost) / self.frames if self.frames else 0.0

    @property
    def name(self) -> str:
        return f"{self.flow}@{self.n}"


@dataclass
class SpeedTrial:
    """One candidate: what the probe said it runs at (`actual`), the rate the host switched to (`switched`), the flows
    verified (full form; a flow run again at n = 1 appears twice), whether it was committed and why not."""
    rate: int
    actual: int | None = None
    switched: int | None = None
    flows: list[FlowResult] = field(default_factory=list)
    committed: bool = False
    why: str = ""
    n_cap: int = 0             # the in-flight cap the rate passed with (1 when a flow needed n = 1; 0: none)
    probation: str = ""        # committed: "running", "passed" or "failed" (in use, its first period); "off": none
    probation_bytes: int = 0   # the bytes moved at the rate in its probation so far
    settling: bool = False     # measured within settle_s of a breakdown at another rate: a failure is noted unknown

    def flow(self, name: str) -> FlowResult | None:
        """The last result of flow `name` ("in" / "out" / "duplex")."""
        return next((f for f in reversed(self.flows) if f.flow == name), None)

    def _kb_s(self, name: str) -> float | None:
        f = self.flow(name)
        return f.kb_s if f is not None and f.frames else None

    @property
    def in_kb_s(self) -> float | None:
        return self._kb_s("in")

    @property
    def out_kb_s(self) -> float | None:
        return self._kb_s("out")

    @property
    def duplex_kb_s(self) -> float | None:
        return self._kb_s("duplex")


@dataclass
class StepDown:
    """A step down in use (guide §7.3.2 item 5): when, from which rate, why, the ratio that decided it (the window's or
    the probation's; None: no answer), the rate the link went to (`to`: the next lower candidate that passed, or the
    boot speed), and whether the rate was still in its probation (then it counts as a verify failure)."""
    at: float
    rate: int
    why: str
    ratio: float | None = None
    to: int | None = None
    probation: bool = False


@dataclass
class Probation:
    """A committed rate's first period in use (guide §7.3.2 item 4): ends (passed) at a good frame once `bytes` have
    moved both ways and `seconds` have passed since `started`; FLOW_FAIL_MIN or more of its frames broken or lost and a
    ratio over `threshold` (max(2 x baseline, VERIFY_FLOOR)) fail it."""
    rate: int
    trial: SpeedTrial
    bytes: int
    seconds: float
    threshold: float
    started: float
    settling: bool = False
    moved: int = 0
    frames: int = 0
    bad: int = 0


@dataclass
class SpeedPlan:
    """What a step down in use may go to: the candidates the last raise_speed would try (after the record and
    max_tries), for the session it ran in, and how to try some of them (`go`, raise_speed's own procedure)."""
    rates: list[int]
    session: int | None
    go: Callable[[list[int]], object]


@dataclass
class SpeedReport:
    """raise_speed's answer, kept as `link.speed`: the boot speed, the rate in force now (`rate`), the committed one
    (`chosen`, None: the boot speed), every candidate in order, and why nothing was tried (`supported` False).
    `verified`: the full form ran (flows measured); `baseline`: flow -> the boot speed's ratio (from this session's
    `baseline_frames` frames, or measured: `baseline_flows`). `lost`: a raised rate was later found gone (the link went
    back to the boot speed). `stepped_down`: in use, the link left a raised rate (`down_why`; every one in
    `step_downs`, each with the rate it went to - the next lower candidate, or the boot speed). `skipped`: candidates
    the record left out as failed; `retried`: the slowest candidate, tried although the record marks every one failed;
    `capped`: candidates max_tries left out. in_kb_s / out_kb_s / duplex_kb_s: the chosen rate's measured throughput
    (None without a measurement) - for budgeting a transfer."""
    base: int
    supported: bool
    rate: int
    chosen: int | None = None
    trials: list[SpeedTrial] = field(default_factory=list)
    why: str = ""
    lost: bool = False
    stepped_down: bool = False     # in use, the raised rate was left for the rest of the session (`down_why`)
    down_why: str = ""
    verified: bool = False
    baseline: dict[str, float] = field(default_factory=dict)
    baseline_frames: int = 0       # this session's frames at the boot speed the baseline came from (0: measured)
    baseline_flows: list[FlowResult] = field(default_factory=list)
    step_downs: list[StepDown] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    retried: int | None = None     # the record marks every candidate failed: this one (the slowest) tried once anyway
    capped: list[int] = field(default_factory=list)   # candidates max_tries left out

    def _chosen(self) -> SpeedTrial | None:
        return next((t for t in reversed(self.trials) if t.committed and t.rate == self.chosen), None)

    @property
    def in_kb_s(self) -> float | None:
        t = self._chosen()
        return t.in_kb_s if t else None

    @property
    def out_kb_s(self) -> float | None:
        t = self._chosen()
        return t.out_kb_s if t else None

    @property
    def duplex_kb_s(self) -> float | None:
        t = self._chosen()
        return t.duplex_kb_s if t else None

    def to_text(self) -> str:
        if not self.supported:
            return f"port_speed not supported: {self.why} (stays at {self.rate})\n"
        lines = []
        if self.verified:
            where = (f"from {self.baseline_frames} frames of this session" if self.baseline_frames
                     else f"measured, {BASELINE_FRAMES} frames per flow")
            lines.append(f"baseline at {self.base} ({where}): " + ", ".join(
                f"{k} {v:.1%}" for k, v in self.baseline.items()) if self.baseline else f"baseline at {self.base}: none")
        if self.skipped:
            lines.append("skipped (the record says failed): " + ", ".join(str(r) for r in self.skipped))
        if self.retried:
            lines.append(f"the record says every candidate failed: {self.retried} (the slowest) tried once")
        if self.capped:
            lines.append("left out (max_tries): " + ", ".join(str(r) for r in self.capped))
        lines.append(f"{'rate':>9} {'actual':>9}  {'flow':<9} {'frames':>6} {'broken':>6} {'lost':>5} {'ratio':>6} "
                     f"{'KB/s':>7}  result")
        for t in self.trials:
            head = f"{t.rate:>9} {t.actual if t.actual else '-':>9}  "
            result = (f"committed{f' (in flight {t.n_cap})' if t.n_cap else ''}"
                      f"{f', probation {t.probation} after {t.probation_bytes} bytes' if t.probation in ('passed', 'failed') else ''}"
                      f"{f'; then {t.why}' if t.why else ''}" if t.committed else t.why)
            if t.settling and not t.committed:
                result += " (soon after a breakdown: unknown)"
            if not t.flows:
                lines.append(head + f"{'-':<9} {'-':>6} {'-':>6} {'-':>5} {'-':>6} {'-':>7}  {result}")
                continue
            for i, f in enumerate(t.flows):
                lines.append((head if i == 0 else " " * len(head)) + f"{f.name:<9} {f.frames:>6} {f.broken:>6} "
                             f"{f.lost:>5} {f.ratio:>6.1%} {f.kb_s:>7.1f}  {'passed' if f.passed else 'failed'}")
            lines.append(" " * len(head) + f"-> {result}")
        for s in self.step_downs:
            lines.append(f"stepped down from {s.rate}{' (in probation)' if s.probation else ''}: {s.why}"
                         f"{f' -> {s.to}' if s.to else ''}")
        if self.stepped_down and not self.chosen:
            lines.append("the boot speed for the rest of the session")
        lines.append(f"in force: {self.rate}" + (" (raised)" if self.chosen else " (the boot speed)"))
        return "\n".join(lines) + "\n"


def _speed_port(hst) -> tuple[int | None, str]:
    """The probe's UART bridge (its transport index) when it declares port_speed; else None and why not."""
    from . import core
    tlvs = core.describe(hst, 0)
    if not any(tag == PORT_SPEED_TAG and value[:1] == b"\x01" for tag, value in tlvs):
        return None, "the probe does not declare port_speed"
    bridges = [index for index, kind, _ in core.transports(hst) if kind == UART_BRIDGE]
    if not bridges:
        return None, "the probe has no UART bridge"
    return bridges[0], ""


def _unit_id(hst) -> str:
    """The probe's unit_id (core §7.5, mandatory) - the record's key with the port."""
    from . import core
    for tag, value in core.describe(hst, 0):
        if tag & 0x7F == UNIT_ID_TAG and value:
            return value.decode("ascii", "replace")
    return "?"


def _answer_wait(size: int, rate: int, n: int) -> float:
    """How long one answer of a flow is waited for: four frames' worth on the line per request in flight, at least 0.3 s."""
    return max(0.3, 4 * (size + 24) * 10 / rate * n + 0.1)


def _keep(hst, lk: SerialLink) -> None:
    """A keepalive at the rate in force (no resend) so the lease outlasts a flow; a failure is the flow's to find."""
    if hst.session is None or lk.keepalive_frame is None:
        return
    saved = lk.resend
    lk.resend = False
    lk._own += 1
    try:
        lk._send(lk.keepalive_frame())
    except LINK_ERRORS:
        pass
    finally:
        lk.resend = saved
        lk._own -= 1


def _flow_run(hst, lk: SerialLink, flow: str, n: int, size: int, frames: int, rate: int) -> FlowResult:
    """`frames` of one flow at the rate in force, `n` in flight, `size` bytes each, counted as the guide counts
    (§7.3.2): broken = an answer came but its content is wrong, lost = no answer (a broken COBS frame on the held port
    is read as one: the link drops it). After lost frames the link is put in step again with a confirm; when none is
    answered the probe is not at this rate any more (it went back) and the flow stops there, the frames not sent
    counted lost (`gone`). The same pattern as linktest.run, whose numbers this matches."""
    res = FlowResult(flow, n)
    limits = hst.limits or hst.confirm()
    data = bytes(k & 0xFF for k in range(size))
    saved = lk.timeout, lk.resend
    lk.timeout, lk.resend = _answer_wait(size, rate, n), False
    _keep(hst, lk)
    moved, t0 = 0, time.perf_counter()
    try:
        while res.frames < frames:
            batch = min(2 * n, frames - res.frames)
            kinds = [flow if flow != "duplex" else ("in" if (res.frames + k) % 2 == 0 else "out") for k in range(batch)]
            msgs = [m.Request(hst.next_corr(), m.CORE_FN, m.OP_LINK_SOURCE if k == "in" else m.OP_LINK_SINK,
                              struct.pack("<I", size) if k == "in" else data).pack() for k in kinds]
            replies: list[bytes] = []
            try:
                lk._exchange_once(msgs, n, limits["window"], replies)
            except LINK_ERRORS:
                pass
            for kind, raw in zip(kinds, replies):
                r = m.Result.unpack(raw)
                if r.succeeded and (r.payload == data if kind == "in" else r.payload[:4] == struct.pack("<I", size)):
                    moved += size
                else:
                    res.broken += 1
            res.lost += len(msgs) - len(replies)
            res.frames += len(msgs)
            if len(replies) < len(msgs) and not any(lk.confirm_raw(CONFIRM_WAIT_S) for _ in range(CONFIRM_TRIES)):
                res.lost += frames - res.frames
                res.frames, res.gone = frames, True
                break
    finally:
        lk.timeout, lk.resend = saved
    seconds = time.perf_counter() - t0
    res.kb_s = moved / seconds / 1000 if seconds else 0.0
    return res


def resolve_flows(flows, n_max: int) -> list[tuple[str, int]]:
    """The caller's flows as (flow, n): n 0 / None = the most this link keeps in flight (`n_max`), more is capped
    there; None = every flow at n_max (in, out, duplex)."""
    if flows is None:
        return [(f, n_max) for f in FLOWS]
    out = []
    for item in flows:
        flow, n = (item, 0) if isinstance(item, str) else (item[0], item[1] if len(item) > 1 else 0)
        if flow not in FLOWS:
            raise ValueError(f"flow {flow!r}: one of {FLOWS}")
        out.append((flow, max(1, min(int(n or 0) or n_max, n_max))))
    return out


def _baseline(hst, lk: SerialLink, report: SpeedReport, flows: list[tuple[str, int]], given: float | None,
              size: int) -> str:
    """The boot speed's ratio per flow (guide §7.3.2 item 2): `given`, or this session's frames at the boot speed when
    BASELINE_FRAMES or more were exchanged and under BASELINE_MAX, else BASELINE_FRAMES measured per flow at its n (over
    BASELINE_MAX: again at n = 1, which then caps that flow). -> "" or why the port is not raised at all."""
    if given is not None:
        report.baseline = {flow: float(given) for flow, _ in flows}
        return ""
    c = lk.base_counts
    total = sum(c.values())
    if total >= BASELINE_FRAMES and (c["broken"] + c["lost"]) / total <= BASELINE_MAX:
        report.baseline_frames = total
        report.baseline = {flow: (c["broken"] + c["lost"]) / total for flow, _ in flows}
        return ""
    for i, (flow, n) in enumerate(flows):
        res = _flow_run(hst, lk, flow, n, size, BASELINE_FRAMES, lk.base_baud)
        report.baseline_flows.append(res)
        if res.ratio > BASELINE_MAX and n > 1:
            res = _flow_run(hst, lk, flow, 1, size, BASELINE_FRAMES, lk.base_baud)
            report.baseline_flows.append(res)
            if res.ratio <= BASELINE_MAX:
                flows[i] = (flow, 1)
        if res.ratio > BASELINE_MAX:
            return (f"the boot speed {lk.base_baud} itself loses {res.ratio:.0%} of {res.name} frames (over "
                    f"{BASELINE_MAX:.0%}): not raised")
        report.baseline[flow] = res.ratio
    return ""


def _verify_flows(hst, lk: SerialLink, rate: int, trial: SpeedTrial, flows: list[tuple[str, int]],
                  baseline: dict[str, float], frames: int, size: int) -> bool:
    """Every flow at the new rate (guide §7.3.2 items 3-3 / 3-4): `frames` or more, failing on broken + lost of
    FLOW_FAIL_MIN or more and a ratio over max(2 x baseline, VERIFY_FLOOR); a failed flow at n > 1 runs again at n = 1
    (then the trial's n_cap is 1). One failed flow fails the candidate (trial.why says which)."""
    for flow, n in flows:
        threshold = max(2 * baseline.get(flow, 0.0), VERIFY_FLOOR)
        for k in ([n, 1] if n > 1 else [n]):
            res = _flow_run(hst, lk, flow, k, size, frames, rate)
            res.passed = not res.gone and not (res.broken + res.lost >= FLOW_FAIL_MIN and res.ratio > threshold)
            trial.flows.append(res)
            if res.passed or res.gone:
                break
        if res.gone:
            trial.why = f"{res.name}: no answer at {rate} any more (the probe went back)"
            return False
        if not res.passed:
            trial.why = (f"{res.name}: {res.broken + res.lost} of {res.frames} frames broken or lost "
                         f"({res.ratio:.0%}, over {threshold:.0%})")
            return False
        if k < n:
            trial.n_cap = 1
    return True


def raise_speed(hst, candidates=DEFAULT_CANDIDATES, *, flows=None, verify: bool | None = None,
                baseline: float | None = None, frames: int = FLOW_FRAMES, verify_ms: int | None = None,
                idle_ms: int = IDLE_MAX_MS, port: int | None = None, record=False, max_tries: int | None = None,
                probation_bytes: int = PROBATION_BYTES, probation_s: float = PROBATION_S,
                settle_s: float = SETTLE_S) -> SpeedReport:
    """port_speed (oep-core §3.5) on the UART bridge this host opened, by the host guide's §7 procedure: try
    `candidates` in order and commit the first that passes. The session must be open (the rate lasts as long as it does).

    The minimal form (§7.2, the default; about 50 ms, no measurement): try -> switch to the requested baud (the
    probe's answer only when the OS refuses it) -> 20 ms -> confirm (100 ms, up to 3) -> commit. The full form
    (`verify=True`, or `flows` given; §7.3): first the boot speed's baseline per flow (`baseline` given, this session's
    frames at the boot speed when 60 or more, else 60 frames measured per flow at its n - over 10 % again at n = 1,
    still over: not raised), then per candidate every flow for `frames` (16) frames of max_frame - 16 bytes - the quick
    gate; a flow fails on broken + lost >= 3 and a ratio over max(2 x baseline, 5 %), runs again at n = 1 first (then
    n = 1 is the link's cap), and one failed flow fails the candidate. `flows`: ("in" | "out" | "duplex", n) pairs (n 0
    = the most this link keeps in flight; default: all three at that n) - verify only what the session will use (§7.3.1).

    A failed candidate: revert (step 2, at the new rate; its answer need not come), the boot speed, confirms up to
    port_speed_idle_max_ms + 1 s (ConnectionError when none is answered). verify_ms: how long the probe waits for the
    commit (default VERIFY_MS 2000, kept a second under the lease). idle_ms: once committed, the probe reverts after this
    long with no good frame (default and at most 3000; 0 and more mean that); the link's keepalive interval is set
    under half of it.

    In use (§7.3.2 item 4): the first period at a committed rate is its probation - until `probation_bytes` (32 KiB,
    both ways) have moved and `probation_s` (1 s) have passed; 0 and 0: none - judged as the verify judges a flow
    (3 or more broken or lost over max(2 x baseline, 5 %)) or a missed answer: either steps down at once and counts as
    a verify failure. After it the last 3 s are judged (none under 50 frames): over max(2 x baseline, 10 %) broken or
    lost, or a missed answer, steps down. A step down: revert, the boot speed, then the next lower candidate of this
    call that has not failed in this session gets a fresh try -> confirm -> verify -> commit (the boot speed when none
    is left). A rate that failed in this session (verify, probation or in use) is not tried again in it, nor any rate
    above it (`report.step_downs` says where each step went).

    `record`: True = the default `speed_record` file, or a path, or a SpeedRecord - passed rates go first, failed ones
    are skipped (`report.skipped`; when every candidate is marked failed, the slowest is tried once anyway:
    `report.retried`), and this run's outcomes are written; a failure measured within `settle_s` (2 s) of a breakdown
    at another rate is written unknown (off by default in the library; on in `oep speed`). max_tries: the most
    candidates tried in this call after the record ordered and filtered them (None = all; the rest:
    `report.capped`; a step down in use goes only to these). port: the transport index (default: the probe's first
    UART bridge). -> the report, also kept as `hst.link.speed`."""
    lk = getattr(hst, "link", None)
    base = getattr(lk, "base_baud", None)
    report = SpeedReport(base or 0, False, getattr(lk, "baud", None) or 0)
    if lk is None or lk.framing != "cobs" or getattr(lk, "transport", None) != "serial" or base is None:
        report.why = "the link is not a serial port this host opened"
        if lk is not None:
            lk.speed = report
        return report
    lk.speed = report
    where, why = _speed_port(hst)
    if where is None:
        report.why = why
        return report
    if hst.session is None:
        raise _host.OepError("raise_speed needs an open session (the rate lasts as long as the session)")
    port = where if port is None else port
    report.supported = True
    verify = flows is not None if verify is None else verify
    report.verified = verify
    lease_ms = hst.lease_ms or 0
    wait = verify_ms if verify_ms is not None else min(VERIFY_MS, max(500, lease_ms - 1000) if lease_ms else VERIFY_MS)
    wait = min(65535, wait)
    idle_ms = idle_ms if 0 < idle_ms <= IDLE_MAX_MS else IDLE_MAX_MS
    if lk.unusable_session != hst.session:
        lk.unusable, lk.failed, lk.unusable_session = {}, {}, hst.session
        lk.broke_at = lk.broke_rate = None
    rec = None
    if record:
        from .speed_record import SpeedRecord
        rec = record if isinstance(record, SpeedRecord) else SpeedRecord(None if record is True else record)
        lk.record, lk.record_key = rec, (lk.port_path or "<stream>", _unit_id(hst))
    run = _Run(flows, verify, frames, wait, idle_ms, port, rec, max(0, int(probation_bytes)), max(0.0, probation_s),
               settle_s)
    lk.fallback = False                                     # every failure here is handled here
    try:
        return _raise(hst, lk, report, list(candidates), run, baseline, max_tries)
    finally:
        lk.fallback = True


@dataclass
class _Run:
    """One raise_speed call's settings, kept for its step downs in use."""
    flows: object
    verify: bool
    frames: int
    wait: int
    idle_ms: int
    port: int
    rec: object
    probation_bytes: int
    probation_s: float
    settle_s: float
    size: int = 0


def _barred(lk: SerialLink, rate: int) -> str:
    """Why `rate` is not tried in this session: it broke in use in it, or it is above a rate that did (no up and
    down)."""
    if rate in lk.unusable:
        return f"broke in use earlier in this session ({lk.unusable[rate]})"
    ceiling = min(lk.unusable, default=None)
    if ceiling is not None and rate > ceiling:
        return f"above {ceiling}, which broke in use in this session"
    return ""


def _raise(hst, lk: SerialLink, report: SpeedReport, candidates: list[int], run: _Run, baseline: float | None,
           max_tries: int | None) -> SpeedReport:
    limits = hst.limits or hst.confirm()
    run.size = limits["max_frame"] - 16
    n_max = lk.inflight_for(limits)
    if run.rec is not None:
        passed, failed = run.rec.lookup(*lk.record_key)
        if candidates and all(r in failed for r in candidates):
            report.retried = min(candidates)                # every one marked failed: the slowest once more
            report.skipped = [r for r in candidates if r != report.retried]
            candidates = [report.retried]
        else:
            report.skipped = [r for r in candidates if r in failed]
            candidates = ([r for r in candidates if r in passed] +
                          [r for r in candidates if r not in passed and r not in failed])
    rates = []
    for rate in candidates:
        why = _barred(lk, rate)
        if why:
            report.trials.append(SpeedTrial(rate, why=why))
        else:
            rates.append(rate)
    if max_tries is not None and len(rates) > max(0, max_tries):
        report.capped, rates = rates[max(0, max_tries):], rates[:max(0, max_tries)]
    if not rates:
        return report
    if run.verify:
        run.flows = resolve_flows(run.flows, n_max)
        report.why = _baseline(hst, lk, report, run.flows, baseline, run.size)
        if report.why:
            return report
    session = hst.session
    lk.speed_plan = SpeedPlan(list(rates), session,
                              lambda lower: _try(hst, lk, report, lower, run) if hst.session == session else None)
    return _try(hst, lk, report, rates, run)


def _try(hst, lk: SerialLink, report: SpeedReport, rates: list[int], run: _Run) -> SpeedReport:
    """Each of `rates` in order until one is committed (raise_speed's own loop, also a step down's in use)."""
    base = lk.base_baud
    rec, port, wait, idle_ms = run.rec, run.port, run.wait, run.idle_ms

    def note(trial: SpeedTrial, passed: bool, phase: str) -> None:
        if rec is not None:
            rec.note(*lk.record_key, trial.rate, None if trial.settling and not passed else passed, phase)

    def failed(trial: SpeedTrial, phase: str) -> None:
        """A candidate the line failed: not again in this session (nor above it), the record says so (unknown when
        measured soon after a breakdown at another rate), and the next results settle from now."""
        lk.failed[trial.rate] = trial.why
        note(trial, False, phase)
        lk.broke_at, lk.broke_rate = time.monotonic(), trial.rate

    def back(rate: int, revert: bool) -> None:
        """A candidate that did not pass: revert at the rate now (its answer need not come), the boot speed, confirmed
        within port_speed_idle_max_ms + 1 s (or verify_ms and a second when that is longer)."""
        if revert:
            saved = lk.timeout, lk.resend
            lk.timeout, lk.resend = 0.3, False
            try:
                hst.call(m.CORE_FN, OP_PORT_SPEED, struct.pack("<BIBHI", port, rate, SPEED_STEP["revert"], 0, 0))
            except (TimeoutError, _host.Rejected, cobs.CorruptFrame, FramingLost):
                pass                                        # lost at that rate, or the probe is back already
            finally:
                lk.timeout, lk.resend = saved
        if not lk.back_to_base(max(OPEN_RETRY_S, wait / 1000 + 1.0)):
            raise ConnectionError(f"after trying {rate}: no answer at the boot speed {base}")
        report.rate = base

    for rate in rates:
        trial = SpeedTrial(rate)
        report.trials.append(trial)
        trial.why = _barred(lk, rate)
        if trial.why:
            continue
        trial.settling = (lk.broke_at is not None and lk.broke_rate != rate
                          and time.monotonic() - lk.broke_at < run.settle_s)
        try:
            r = hst.call(m.CORE_FN, OP_PORT_SPEED, struct.pack("<BIBHI", port, rate, SPEED_STEP["try"], wait, idle_ms))
        except TimeoutError:
            trial.why = "no answer to the try"              # it may have switched: wait it out at the boot speed
            back(rate, False)
            continue
        except _host.Rejected as e:
            if e.result.detail == m.UNKNOWN_OPERATION:
                report.supported, report.why = False, "the probe does not take port_speed (unknown_operation)"
                report.trials.pop()
                return report
            if e.result.detail != m.UNSUPPORTED:
                trial.why = str(e)
                break                                       # wrong port, locked, ...: nothing else will do better
            trial.why = "unsupported: the probe's UART cannot make it"
            note(trial, False, "try")
            continue
        trial.actual = m.Reader(r.payload).u32()
        try:
            trial.switched = lk.set_baud(rate, trial.actual)   # the requested baud; the probe's only if the OS refuses
        except (ValueError, OSError, serial.SerialException) as e:
            trial.why = f"the OS refuses {rate} and {trial.actual}: {e}"
            lk.baud = rate                                  # wait the probe out (verify_ms), then the boot speed
            time.sleep(wait / 1000)
            back(rate, False)
            note(trial, False, "try")
            continue
        if not any(lk.confirm_raw(CONFIRM_WAIT_S) for _ in range(CONFIRM_TRIES)):
            trial.why = "no confirm at the new rate"
            back(rate, True)
            failed(trial, "confirm")
            continue
        if run.verify and not _verify_flows(hst, lk, rate, trial, run.flows, report.baseline, run.frames, run.size):
            back(rate, True)
            failed(trial, "verify")
            continue
        try:
            hst.call(m.CORE_FN, OP_PORT_SPEED, struct.pack("<BIBHI", port, rate, SPEED_STEP["commit"], 0, idle_ms))
        except (TimeoutError, _host.Rejected) as e:
            trial.why = f"the commit failed: {e}"
            back(rate, False)
            continue
        trial.committed = True
        report.rate, report.chosen = rate, rate
        lk.inflight_cap = trial.n_cap
        lk.speed_port = port
        lk.baseline_ratio = max(report.baseline.values(), default=0.0)
        lk.keepalive_s = min(KEEPALIVE_S, idle_ms / 1000 / 2.5)   # under half of idle_ms (core §3.5 obligation 4)
        lk.window.clear()
        lk.step_due = ""
        note(trial, True, "verify" if run.verify else "confirm")
        if run.probation_bytes or run.probation_s:
            trial.probation = "running"
            lk.probation = Probation(rate, trial, run.probation_bytes, run.probation_s,
                                     max(2 * lk.baseline_ratio, VERIFY_FLOOR), time.monotonic(), trial.settling)
        else:
            trial.probation, lk.probation = "off", None
        return report
    return report
