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

port_speed (oep-core §3.5, optional, opt-in): `raise_speed` (or `open_host(..., port_speed=[rates])`) asks a probe that
declares it for faster rates on the UART bridge this host opened: try a rate, switch the port, verify it with a sized
transfer both ways (link_source / link_sink with max_frame-sized frames) that counts broken frames and measures the
throughput, then both ways at once (the two interleaved, pipelined as the in/out passes did), then commit it, or revert and wait the probe out and re-confirm at the boot speed. The report stays on the
link (`link.speed`). Once raised, a request whose answer never comes even after its resend sends the link back to the
boot speed (the probe reverts by itself) and goes once more there: the link never wedges at a rate the probe left;
while raised each wait is at most a quarter of the lease, so this ends inside it. In use, STRIKE_MAX (3) broken frames
or resends within STRIKE_WINDOW_S (5 s) step down: port_speed revert at the raised rate, the boot speed, a confirm.
Either way the rate is not used again in that session (`link.speed.stepped_down`, `down_why`).
The probe also goes back after port_speed_idle_max_ms (3 s) with no good frame, so while raised the link sends a
keepalive before a request when it has been quiet for 1 s, and `keep_alive()` does the same for a caller that sits
idle for long. A host opening a serial port retries its first confirm for that maximum and a little (4 s in all): a
host that raised the speed and died leaves the probe at its rate until then.
"""

from __future__ import annotations

import collections
import select
import socket
import struct
import time
from dataclasses import dataclass, field

import serial

from . import cobs, host as _host, message as m, registry as reg
from .frames import FramingLost, LengthFrames

RESYNC_QUIET_S = reg.TIMING["resync_quiet_ms"] / 1000
USB_VID, USB_PID = 0x303A, 0x0002   # the reference P4 probe until the OEP PID is taken (probe guide §3.8)


class CorrMismatch(FramingLost):
    """A result for a request other than the one waited for."""


class PortBusy(OSError):
    """Another program holds the serial port (it was opened exclusively): only one host at a time on a serial port."""


BASE_BAUD = 115200   # the boot speed of every reference UART bridge (the board's profile decides; core §3.5)
IDLE_MAX_MS = reg.TIMING["port_speed_idle_max_ms"]   # a committed rate goes back after this with no good frame (§3.5)
KEEPALIVE_S = 1.0    # raised: a keepalive once the link has been quiet this long (well inside IDLE_MAX_MS)
OPEN_RETRY_S = IDLE_MAX_MS / 1000 + 1.0   # opening a serial port: confirm retried this long (a raised rate left over)
OPEN_TRY_S = 0.5     # each of those confirms waits this long (at most the link's timeout)
STRIKE_MAX, STRIKE_WINDOW_S = 3, 5.0   # raised, in use: this many broken frames / resends within the window step down
STEP_DOWN_WAIT_S = 0.2   # the step down's revert (step 2) at the raised rate waits this long, never sent again
RAISED_WAIT_MIN_S = 0.3  # raised, in use: each wait for an answer is a quarter of the lease, at least this
LINK_ERRORS = (cobs.CorruptFrame, TimeoutError, FramingLost)


def open_serial(port: str, baud: int = BASE_BAUD):
    """The port opened exclusively (flock and TIOCEXCL): a second open by anyone fails with PortBusy. The driver's
    low-latency mode on where it has one (the FTDI latency timer 16 -> 1 ms; a pty or a driver without it: ignored)."""
    try:
        stream = serial.Serial(port, baud, timeout=0.05, exclusive=True)
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
    return stream


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

    @classmethod
    def on_stream(cls, stream, framing: str = "length", timeout: float = 3.0) -> SerialLink:
        """A link on any pyserial-shaped stream: "cobs" for a serial port (a pty, a scripted stream), "length" for a
        USB bulk pair, HID or TCP."""
        lk = cls.__new__(cls)
        lk._setup(stream, framing, timeout)
        return lk

    def _setup(self, stream, framing: str, timeout: float) -> None:
        self.stream = stream
        self.framing = framing
        self.timeout = timeout
        self.resend = True                         # a lost reply: the request once more with the same corr (core §5.2)
        self.retries = 0
        self.inflight_cap = 0                      # port_speed: the in-flight requests the raised rate verified with
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
        self.strikes: list[float] = []             # raised, in use: when frames broke / requests went again (window)
        self.step_due = ""                         # raised, in use: why the link steps down at the next safe point
        self.speed_port: int | None = None         # the transport index the raised rate is on (the revert names it)
        self.unusable: dict[int, str] = {}         # rates stepped down from in this session -> why (not tried again)
        self.unusable_session: int | None = None   # the session `unusable` belongs to
        self.session_frame = None                  # (op, payload) -> a core request in the session, once bound
        self.lease_s = lambda: None                # the session's lease, once bound (raised: bounds every wait)
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
            self.stream.write(b"".join(cobs.frame(msg) for msg in messages))
        self.last_tx = time.monotonic()

    def _recv(self) -> bytes:
        if self.framing == "length":
            reply = self.frames.recv(self.timeout)
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
                    return cobs.unframe(raw)
                except cobs.CorruptFrame:
                    self.noise += len(raw)           # raw bytes of the port, or a broken frame: noise, no resend
                    if self.held():
                        # a session holds this port: the probe sends no raw bytes on it (oep-core §3.4), so this was a
                        # broken frame - most likely the reply. Send again now instead of waiting out the timeout (a
                        # second waited 1 s each on an M5Stack ATOM's FTDI, 2026-10-01); a repeat is answered from
                        # the probe's retry table (§5.2)
                        raise
                    continue
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
        try:
            while True:
                if self._raised():                 # a long pump at a raised rate keeps the line alive (core §3.5)
                    self.timeout = saved
                    self._keep_raised()
                self.timeout = max(0.0, deadline - time.monotonic())
                if self._raised():
                    self.timeout = min(self.timeout, KEEPALIVE_S)
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
        nothing (a late answer is read past by its corr)."""
        if self.framing == "length":
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
            self.set_baud(self.base_baud)          # the probe went back right after this answer (core §3.5)
            if self.speed is not None:
                self.speed.rate, self.speed.chosen = self.base_baud, None
        self._step_down_if_due()
        return reply

    @staticmethod
    def _reverts(message: bytes, reply: bytes) -> bool:
        """A completed end, or port_speed's revert: the probe is back at its boot speed once this answer is out."""
        if len(message) < 6 or len(reply) < 4 or reply[3] != m.COMPLETED or message[3] | message[4] << 8 != m.CORE_FN:
            return False
        if message[5] == m.OP_END:
            return True
        at = 6 + (4 if message[0] & m.ROLE_SESSION else 0) + 5     # port(u8) baud(u32) step(u8)
        return message[5] == OP_PORT_SPEED and len(message) > at and message[at] == SPEED_STEP["revert"]

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
    def set_baud(self, rate: int) -> None:
        """The host side of the serial port to `rate` (pyserial's baudrate), what was read so far dropped."""
        if hasattr(self.stream, "baudrate"):
            self.stream.baudrate = rate
        self.baud = rate
        if self.framing == "cobs":
            time.sleep(SWITCH_SETTLE_S)               # the probe switches once its answer is out: let both ends settle
            self._buf.clear()
            self.stream.reset_input_buffer()

    def confirm_raw(self, timeout: float) -> bool:
        """A confirm straight on the link (not through the host): True when its answer came within `timeout`."""
        corr = self.corr_source()
        self._write([m.Request(corr, m.CORE_FN, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([0, 0xFF])).pack()])
        saved = self.timeout
        self.timeout = timeout
        try:
            self._recv_for(corr)
            return True
        except (cobs.CorruptFrame, TimeoutError, FramingLost):
            return False
        finally:
            self.timeout = saved

    def _confirm_within(self, wait_s: float, each_s: float) -> bool:
        """Confirms, each waiting `each_s`, until one is answered (True) or `wait_s` has passed (False)."""
        deadline = time.monotonic() + wait_s
        while True:
            if self.confirm_raw(each_s):
                return True
            if time.monotonic() >= deadline:
                return False

    def back_to_base(self, wait_s: float = 3.0) -> bool:
        """The host at the boot speed again, and the probe confirmed there: confirms every 0.25 s up to `wait_s` (a
        probe still trying waits out its verify_ms; one committed reverts at the broken candidates these make)."""
        self.inflight_cap = 0
        if self.base_baud is None:
            return False
        self.set_baud(self.base_baud)
        return self._confirm_within(wait_s, 0.25)

    def _raised(self) -> bool:
        return self.base_baud is not None and self.baud != self.base_baud

    def keep_alive(self) -> bool:
        """While a raised rate is in force and a session holds the port: a keepalive when the link has been quiet for
        KEEPALIVE_S (1 s). The probe goes back to the boot speed after port_speed_idle_max_ms (3 s) with no good frame
        (core §3.5); every request already does this before it goes out, so only a caller that sits idle for long
        (waiting on a person, a sleep between requests) calls it - often is fine, it sends nothing otherwise.
        True when a keepalive went out."""
        if not self._raised() or self.keepalive_frame is None or not self.held():
            return False
        if time.monotonic() - self.last_tx < KEEPALIVE_S:
            return False
        self.last_tx = time.monotonic()            # before sending: send() asks again and must not recurse
        self.send(self.keepalive_frame())
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
        request again there) must end well inside the lease."""
        if not self._in_use():
            return self.timeout
        lease = self.lease_s()
        return self.timeout if lease is None else min(self.timeout, max(RAISED_WAIT_MIN_S, lease / 4))

    def _strike(self, e: Exception) -> None:
        """Raised and in use: a frame broke or a request is sent again (`e`). STRIKE_MAX within STRIKE_WINDOW_S: the
        link steps down at the next safe point (core §3.5 item 7)."""
        if not self._in_use():
            return
        now = time.monotonic()
        self.strikes = [t for t in self.strikes if now - t < STRIKE_WINDOW_S] + [now]
        if len(self.strikes) >= STRIKE_MAX and not self.step_due:
            self.step_due = (f"{len(self.strikes)} broken frames / resends within {STRIKE_WINDOW_S:g} s at {self.baud} "
                             f"(the last: {type(e).__name__})")

    def _step_down_if_due(self) -> None:
        if self.step_due and self._in_use():
            self._step_down(self.step_due)

    def _step_down(self, why: str) -> None:
        """Leave the raised rate for the rest of the session: port_speed step 2 at it (STEP_DOWN_WAIT_S, never sent
        again: a probe that already went back cannot hear it, and a committed one reverts at the broken candidates
        the confirms make), the host at the boot speed, a confirm there. ConnectionError when none is answered."""
        rate = self.baud
        self.step_due = ""
        self.strikes.clear()
        if self.session_frame is not None and self.speed_port is not None:
            saved = self.timeout, self.resend, self.fallback
            self.timeout, self.resend, self.fallback = STEP_DOWN_WAIT_S, False, False
            try:
                self._send(self.session_frame(OP_PORT_SPEED, struct.pack("<BIBHI", self.speed_port, 0,
                                                                         SPEED_STEP["revert"], 0, 0)))
            except LINK_ERRORS:
                pass
            finally:
                self.timeout, self.resend, self.fallback = saved
        if not self.back_to_base():
            raise ConnectionError(f"the probe answers neither at {rate} nor at the boot speed {self.base_baud}")
        self._stepped(rate, why)

    def _stepped(self, rate: int, why: str) -> None:
        """`rate` is not used again in this session (raise_speed skips it), and the report says so."""
        self.unusable[rate] = why
        if self.speed is not None:
            self.speed.rate, self.speed.chosen = self.base_baud, None
            self.speed.stepped_down, self.speed.down_why = True, why

    def _speed_fallback(self, e: Exception | None = None) -> bool:
        """A request failed (its resend too) while a raised rate was in use. Broken frames (the probe still answers
        at that rate) or a step down already due: step down (`_step_down`). No answer at all: the probe went back
        (idle_ms, broken candidates, a lapse) - back to the boot speed, confirmed. Either way the rate is not used
        again in this session. True = send again there."""
        if not self._in_use() or self.base_baud is None:
            return False
        rate = self.baud
        if self.step_due or not isinstance(e, TimeoutError):
            self._step_down(self.step_due or f"frames kept breaking at {rate} ({type(e).__name__})")
            return True
        if not self.back_to_base():
            raise ConnectionError(f"the probe answers neither at {self.baud} nor at the boot speed {self.base_baud}")
        self.speed_lost += 1
        self.step_due = ""
        self.strikes.clear()
        if self.speed is not None:
            self.speed.lost = True
        self._stepped(rate, f"no answer at {rate} (the probe went back by itself)")
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

    def bind(self, limits: dict):
        """This link's exchange with the probe's limits (core confirm), for Host(exchange=...)."""
        return lambda msgs: self.exchange(msgs, min(limits["max_inflight"], self.inflight_cap or 255), limits["window"])

    def attach_host(self, hst) -> None:
        """Bind to a host: its correlation counter and blind stops for the resync, and after a confirm, the probe's
        limits (in-flight, window, max_frame) for pipelining and the framing check."""
        self.corr_source = hst.next_corr
        self.blind = hst.blind_stop
        self.held = lambda: hst.session is not None
        self.session_frame = lambda op, payload: m.Request(hst.next_corr(), m.CORE_FN, op, payload, hst.session).pack()
        self.keepalive_frame = lambda: self.session_frame(m.OP_KEEPALIVE, b"")
        self.lease_s = lambda: hst.lease_ms / 1000 if hst.session is not None and hst.lease_ms else None
        hst.link = self
        if self.framing == "cobs" and getattr(self, "transport", None) == "serial":
            self.wait_boot_speed()
        limits = hst.confirm()
        hst.exchange = self.bind(limits)
        if self.framing == "length" and limits.get("max_frame"):
            self.frames.max_frame = limits["max_frame"]

    def wait_boot_speed(self, wait_s: float | None = None) -> None:
        """A serial port just opened: confirm at the boot speed, retried for OPEN_RETRY_S (port_speed_idle_max_ms and
        a second; at least the link's timeout) - a host that raised the speed and died leaves the probe at that rate
        until its idle limit runs out (core §3.5 item 6). TimeoutError when none was answered."""
        wait_s = max(OPEN_RETRY_S, self.timeout) if wait_s is None else wait_s
        if not self._confirm_within(wait_s, min(self.timeout, OPEN_TRY_S)):
            raise TimeoutError(f"no answer to confirm at {self.baud} for {wait_s:.1f} s")

    def close(self) -> None:
        self.stream.close()


def open_usb_host(vid: int = USB_VID, pid: int = USB_PID, serial: str | None = None, timeout: float = 3.0,
                  transports: tuple[str, ...] = ("vendor", "hid")):
    """A Host on the probe's USB device (the P4's HS OTG port), trying its ways in in the oep-core §3.3 order: vendor bulk,
    then vendor-defined HID (when raw USB is not permitted or the probe offers no vendor interface). A CDC port is
    opened by path (open_host). Length-prefixed frames on all of them."""
    from . import host
    errors = []
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
        lk.attach_host(hst)
        return hst
    raise FileNotFoundError(f"no way in to {vid:04x}:{pid:04x}: " + "; ".join(errors))


OEP_VID_PID = (reg.USB["reference_vid"], reg.USB["reference_pid"])   # the reference firmware's; never a way to tell a probe


def is_oep_device(vid: int, pid: int, product: str | None) -> bool:
    """core §3.3: an OEP probe is a USB device whose iProduct starts with "OEP" (registry usb.iproduct_prefix); the
    VID:PID tells nothing (vid and pid are taken for the callers that have them)."""
    return (product or "").startswith(reg.USB["iproduct_prefix"])


def find_usb(unit_id: str) -> tuple[int, int]:
    """The VID:PID of the OEP probe whose USB serial number is `unit_id` (core §3.3, §7.5) - how a host that kept a
    probe's unit_id (oep://<unit_id>/<slot>) finds it again whatever VID:PID it enumerates with."""
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
                    if (h.getSerialNumber() or "").lower() == want and \
                            is_oep_device(dev.getVendorID(), dev.getProductID(), h.getProduct()):
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
                product = usb.util.get_string(dev, dev.iProduct) or ""
            except Exception:
                continue
            if serial.lower() == want and is_oep_device(dev.idVendor, dev.idProduct, product):
                return dev.idVendor, dev.idProduct
    raise FileNotFoundError(f"no OEP probe with unit id (USB serial) {unit_id}")


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
              port_speed: list[int] | None = None, lease_ms: int = 3000, owner: str | None = None):
    """A Host on `target`, with the link's pipelining bound to the probe's limits (core confirm): Host.pipeline and
    everything built on it (flash, capture reads) then keep several requests in flight.

    target: a serial port path (COM3 on Windows); tcp://HOST:PORT for a local broker (length frames); usb[:VID:PID[:SERIAL]]
    (hex) for the probe's USB device, vendor bulk then HID (oep-core §3.3); usb:UNIT_ID for the OEP probe whose USB
    serial is that unit id, whatever its VID:PID.

    timeout: seconds to wait for a reply (default 3; TCP 15). resend: send a request once more with the same corr when
    its reply was lost (default on; off for TCP, where a broker retries towards the probe itself and a second copy from
    here would only race it - a lost reply is then raised to the caller).

    baud: a serial port's boot speed (the board's profile; every port_speed revert goes back to it). port_speed: rates to
    try, in order (opt-in, core §3.5): the session is taken (core.take, `lease_ms`, `owner`) and left open for the
    caller - who goes on in it, never opening another (a new session would end this one, and the rate with it) - and
    `raise_speed` runs; its report is `hst.link.speed`. A probe without port_speed, or a link that is not a serial port
    this host opened, stays at its speed (the report says why)."""
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
            return open_usb_host(vid, pid, parts[0], timeout)
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
        raise_speed(hst, port_speed)
    return hst


# ---- port_speed (core §3.5) -------------------------------------------------------------------------------------------
OP_PORT_SPEED = reg.CORE.op["port_speed"]
SPEED_STEP = reg.CORE.enum["port_speed_step"]
PORT_SPEED_TAG = reg.CORE.tlv["describe"]["port_speed"]
UART_BRIDGE = reg.CORE.enum["transport_kind"]["uart_bridge"]


@dataclass
class SpeedTrial:
    """One rate tried: what the probe said it runs at, the verify's bytes, KB/s (1000 B/s) and broken frames each way
    (in = probe to host, link_source; out = host to probe, link_sink; duplex = both at once), and whether it was committed (why not)."""
    rate: int
    actual: int | None = None
    in_bytes: int = 0
    out_bytes: int = 0
    in_kb_s: float | None = None
    out_kb_s: float | None = None
    broken_in: int = 0
    broken_out: int = 0
    duplex_bytes: int = 0      # both ways at once (link_source and link_sink interleaved), at the in-flight that passed
    duplex_kb_s: float | None = None
    broken_duplex: int = 0
    committed: bool = False
    why: str = ""
    inflight: int = 0          # the requests kept in flight the verify passed with (0: none passed)


@dataclass
class SpeedReport:
    """raise_speed's answer, kept as `link.speed`: the boot speed, the rate in force now (`rate`), the committed one
    (`chosen`, None: the boot speed), every trial in order, and why nothing was tried (`supported` False).
    `lost`: a raised rate was later found gone (the link went back to the boot speed). `stepped_down`: in use, the
    link left the raised rate for the rest of the session (frames kept breaking, or no answer came), `down_why` says
    why. in_kb_s / out_kb_s / duplex_kb_s: the chosen rate's measured throughput (None at the boot speed) - for
    budgeting a transfer."""
    base: int
    supported: bool
    rate: int
    chosen: int | None = None
    trials: list[SpeedTrial] = field(default_factory=list)
    why: str = ""
    lost: bool = False
    stepped_down: bool = False     # in use, the raised rate was left for the rest of the session (`down_why`)
    down_why: str = ""

    def _chosen(self) -> SpeedTrial | None:
        return next((t for t in self.trials if t.committed), None) if self.chosen else None

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
        lines = [f"{'rate':>9} {'actual':>9} {'in KB/s':>8} {'out KB/s':>8} {'duplex KB/s':>11} "
                 f"{'broken in/out/duplex':>20}  result"]
        for t in self.trials:
            kb = lambda v, w=8: f"{v:{w}.1f}" if v is not None else f"{'-':>{w}}"   # noqa: E731
            lines.append(f"{t.rate:>9} {t.actual if t.actual else '-':>9} {kb(t.in_kb_s)} {kb(t.out_kb_s)} "
                         f"{kb(t.duplex_kb_s, 11)} {f'{t.broken_in}/{t.broken_out}/{t.broken_duplex}':>20}  "
                         f"{(f'committed (in flight {t.inflight})' if t.committed else t.why)}")
        if self.stepped_down:
            lines.append(f"stepped down: {self.down_why} - the boot speed for the rest of the session")
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


def _verify(hst, lk: SerialLink, rate: int, trial: SpeedTrial, verify_bytes: int, verify_s: float,
            inflight: int | None = None, ways: tuple[str, ...] = ("in", "out"), duplex_s: float = 1.0,
            duplex_frames: int = 64) -> bool:
    """Max_frame-sized frames, pipelined as `inflight` (default: as the probe allows), for each of `ways`: "in"
    (link_source) and "out" (link_sink) up to half of verify_bytes or verify_s each; "duplex" both interleaved
    (link_source, link_sink, ...) for duplex_s and at least duplex_frames, so frames go both ways at once. Stops at
    the first frame that breaks (lost, or its content wrong)."""
    limits = hst.limits or hst.confirm()
    max_frame, window = limits["max_frame"], limits["window"]
    inflight = max(1, inflight or limits["max_inflight"])
    n_in = max_frame - m.RESULT_HEADER                    # link_source: a whole result frame
    n_out = max_frame - 6                                 # link_sink: a whole request frame (no session)
    wire = (max_frame + 8) * 10 / rate                    # one frame on the line, seconds
    saved = lk.timeout, lk.resend
    lk.timeout, lk.resend = max(0.3, 4 * wire * inflight + 0.1), False
    pattern = bytes(k & 0xFF for k in range(n_in))
    sink_body = bytes((k * 7) & 0xFF for k in range(n_out))

    def request(k: int, way: str) -> tuple[bytes, bool]:
        source = way == "in" or (way == "duplex" and k % 2 == 0)
        body, op = (struct.pack("<I", n_in), m.OP_LINK_SOURCE) if source else (sink_body, m.OP_LINK_SINK)
        return m.Request(hst.next_corr(), m.CORE_FN, op, body).pack(), source

    def more(way: str, moved: int, frames: int, t0: float) -> bool:
        if way == "duplex":
            return time.perf_counter() - t0 < duplex_s or frames < duplex_frames
        return moved < verify_bytes / 2 and time.perf_counter() - t0 < verify_s / 2

    try:
        for way in ways:
            moved, frames, broken, t0 = 0, 0, 0, time.perf_counter()
            while more(way, moved, frames, t0):
                batch = [request(k, way) for k in range(inflight * 2)]
                replies: list[bytes] = []
                try:
                    lk._exchange_once([msg for msg, _ in batch], inflight, window, replies)
                except (cobs.CorruptFrame, TimeoutError, FramingLost):
                    pass                                  # the frames not answered are the broken ones
                good = 0
                for r, (_, source) in zip(replies, batch):
                    res = m.Result.unpack(r)
                    if not (res.succeeded and (res.payload == pattern if source
                                               else res.payload[:4] == struct.pack("<I", n_out))):
                        break
                    good += 1
                    moved += n_in if source else n_out
                frames += good
                if good < len(batch):
                    broken = len(batch) - good
                    break
            kb_s = moved / max(time.perf_counter() - t0, 1e-6) / 1000
            if way == "in":
                trial.in_bytes, trial.in_kb_s, trial.broken_in = moved, kb_s, broken
            elif way == "out":
                trial.out_bytes, trial.out_kb_s, trial.broken_out = moved, kb_s, broken
            else:
                trial.duplex_bytes, trial.duplex_kb_s, trial.broken_duplex = moved, kb_s, broken
            if broken:
                return False
        return True
    finally:
        lk.timeout, lk.resend = saved


SWITCH_SETTLE_S = 0.02   # after a baud change, before the first byte at the new rate (the ATOM's FTDI lost it at once)


def raise_speed(hst, rates: list[int], *, verify_bytes: int = 32768, verify_s: float = 1.0,
                verify_ms: int | None = None, idle_ms: int = IDLE_MAX_MS, port: int | None = None,
                duplex_s: float = 1.0, duplex_frames: int = 64) -> SpeedReport:
    """port_speed (oep-core §3.5), opt-in: try `rates` in order on the UART bridge this host opened, and commit the
    first that passes. Each: try (answered at the speed now) -> the host switches -> verify both ways with
    max_frame-sized frames for verify_bytes or verify_s in all (broken frames counted, KB/s measured each way),
    pipelined as the probe allows and, when that breaks, one at a time -> both ways at once at the in-flight that
    passed (link_source and link_sink interleaved, duplex_s and at least duplex_frames; a broken frame fails the rate:
    "broke both ways at once") -> commit at the new speed when nothing broke; else revert (step 2, at the new speed) and back to the boot speed,
    re-confirmed there (waiting out the probe's verify_ms when the revert was lost), and the next rate. A rate the
    probe's UART cannot make is skipped (unsupported). The session must be open (the rate lasts as long as it does).

    verify_ms: how long the probe waits for the commit (default: verify_s + 1.5 s, at most 65535). idle_ms: once
    committed, the probe reverts after this long with no good frame (default and at most port_speed_idle_max_ms,
    3000; 0 and anything longer mean that maximum). The link keeps the line alive meanwhile (`keep_alive`), and
    steps down to the boot speed for the rest of the session when frames break or requests go again STRIKE_MAX times
    within STRIKE_WINDOW_S, or an answer does not come at all (`report.stepped_down`); a rate stepped down from in
    this session is not tried again. port: the transport index (default: the probe's first UART bridge). -> the report, also kept as `hst.link.speed`."""
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
    wait = (verify_ms if verify_ms is not None
            else min(65535, int((verify_s + duplex_s) * 1000) + 1500))
    if lk.unusable_session != hst.session:
        lk.unusable, lk.unusable_session = {}, hst.session
    lk.fallback = False                                     # every failure here is handled here
    idle_ms = idle_ms if 0 < idle_ms <= IDLE_MAX_MS else IDLE_MAX_MS
    try:
        return _raise(hst, lk, report, rates, port, base, wait, verify_bytes, verify_s, idle_ms, duplex_s, duplex_frames)
    finally:
        lk.fallback = True


def _raise(hst, lk, report, rates, port, base, wait, verify_bytes, verify_s, idle_ms, duplex_s,
           duplex_frames) -> SpeedReport:
    for rate in rates:
        trial = SpeedTrial(rate)
        report.trials.append(trial)
        if rate in lk.unusable:
            trial.why = f"stepped down from earlier in this session ({lk.unusable[rate]})"
            continue
        try:
            r = hst.call(m.CORE_FN, OP_PORT_SPEED, struct.pack("<BIBHI", port, rate, SPEED_STEP["try"], wait, 0))
        except TimeoutError:
            trial.why = "no answer to the try"              # it may have switched: wait it out at the boot speed
            if not lk.back_to_base(wait / 1000 + 1.5):
                raise ConnectionError(f"after trying {rate}: no answer at the boot speed {base}") from None
            continue
        except _host.Rejected as e:
            if e.result.detail == m.UNKNOWN_OPERATION:
                report.supported, report.why = False, "the probe does not take port_speed (unknown_operation)"
                report.trials.pop()
                return report
            trial.why = "unsupported: the probe's UART cannot make it" if e.result.detail == m.UNSUPPORTED else str(e)
            if e.result.detail != m.UNSUPPORTED:
                break                                       # wrong port, locked, ...: nothing else will do better
            continue
        trial.actual = m.Reader(r.payload).u32()
        lk.set_baud(rate)
        # the switch-over itself may cost the first frame (bytes in flight while both ends change): one confirm, sent
        # again a couple of times, finds the new rate before anything is measured (oep-core §3.5)
        ok = any(lk.confirm_raw(0.2) for _ in range(3))
        heard = ok
        # pipelined first (what the host will use); a link that loses bytes while both ways carry at once (an FTDI at
        # 500 kbaud and up behind usbip, 2026-10-01) gets a second verify with one request at a time
        full = max(1, (hst.limits or hst.confirm())["max_inflight"])
        tries = [full] if full == 1 else [full, 1]
        broke = "frames broke"
        for n in tries if ok else []:
            ok = _verify(hst, lk, rate, trial, verify_bytes, verify_s, n)
            if ok:
                trial.inflight = n
                break
            any(lk.confirm_raw(0.2) for _ in range(3))     # the broken frames' leftovers read past
        if ok:
            # both ways at once at the in-flight that passed: a line that carries each way alone can still break
            # under sustained duplex use (a CH340 at 921600 broke a frame every ~0.5 s, 2026-10-01)
            ok = _verify(hst, lk, rate, trial, verify_bytes, verify_s, trial.inflight, ("duplex",), duplex_s,
                         duplex_frames)
            if not ok:
                broke, trial.inflight = "broke both ways at once", 0
                any(lk.confirm_raw(0.2) for _ in range(3))
        if ok:
            try:
                hst.call(m.CORE_FN, OP_PORT_SPEED, struct.pack("<BIBHI", port, rate, SPEED_STEP["commit"], 0, idle_ms))
                trial.committed = True
                report.rate, report.chosen = rate, rate
                lk.inflight_cap = trial.inflight if trial.inflight < full else 0
                lk.speed_port = port
                lk.strikes.clear()
                lk.step_due = ""
                return report
            except (TimeoutError, _host.Rejected) as e:
                trial.why = f"the commit failed: {e}"
        else:
            trial.why = broke if heard else "no confirm at the new rate"
            saved = lk.timeout, lk.resend
            lk.timeout, lk.resend = 0.3, False
            try:
                hst.call(m.CORE_FN, OP_PORT_SPEED, struct.pack("<BIBHI", port, rate, SPEED_STEP["revert"], 0, 0))
            except (TimeoutError, _host.Rejected, cobs.CorruptFrame, FramingLost):
                pass                                        # lost at that rate: the probe goes back by itself
            finally:
                lk.timeout, lk.resend = saved
        if not lk.back_to_base(wait / 1000 + 1.5):
            raise ConnectionError(f"after trying {rate}: no answer at the boot speed {base}")
        report.rate = base
    return report

