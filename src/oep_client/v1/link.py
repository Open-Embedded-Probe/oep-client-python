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
defaults - DTR and RTS asserted - which reset none of the measured probes (host guide §1). No sleep after open.
"""

from __future__ import annotations

import collections
import select
import socket
import time

import serial

from . import cobs, message as m, registry as reg
from .frames import FramingLost, LengthFrames

RESYNC_QUIET_S = reg.TIMING["resync_quiet_ms"] / 1000
USB_VID, USB_PID = 0x303A, 0x0002   # the reference P4 probe until the OEP PID is taken (probe guide §3.8)


class CorrMismatch(FramingLost):
    """A result for a request other than the one waited for."""


class PortBusy(OSError):
    """Another program holds the serial port (it was opened exclusively): only one host at a time on a serial port."""


def open_serial(port: str, baud: int = 115200):
    """The port opened exclusively (flock and TIOCEXCL): a second open by anyone fails with PortBusy."""
    try:
        stream = serial.Serial(port, baud, timeout=0.05, exclusive=True)
    except serial.SerialException as e:
        if "busy" in str(e).lower() or "lock" in str(e).lower() or getattr(e, "errno", None) == 16:
            raise PortBusy(f"{port} is open in another program: {e}") from e
        raise
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

    def __init__(self, port: str, timeout: float = 3.0):
        self._setup(open_serial(port), "cobs", timeout)

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
        self.retries = 0
        self.corrupt = 0
        self.stale = 0                             # replies that answered another request
        self.noise = 0                             # serial ports: bytes that were not a frame (the probe's raw side)
        self.resyncs = 0
        self.ended_blind = False
        self.dropped = 0                           # probe-initiated frames of a role this client does not handle
        self.pushes: collections.deque[bytes] = collections.deque()   # role 0x06 frames, oldest first
        self.events: collections.deque[bytes] = collections.deque()   # role 0x05 frames, oldest first
        self.corr_source = self._own_corr          # the host's correlation counter once bound (open_host)
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

    def _recv(self) -> bytes:
        if self.framing == "length":
            reply = self.frames.recv(self.timeout)
            if reply is None:
                raise TimeoutError("no result from the probe")
            return reply
        deadline = time.monotonic() + self.timeout
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
                self.timeout = max(0.0, deadline - time.monotonic())
                try:
                    frame = self._recv()
                except TimeoutError:
                    return n
                except FramingLost:
                    self.timeout = saved
                    self.resync()
                    return n
                except cobs.CorruptFrame:
                    self.corrupt += 1
                    continue
                n += 1
                if not self._route(frame):
                    self.stale += 1
                if until_one or time.monotonic() >= deadline:
                    return n
        finally:
            self.timeout = saved

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
        corr = self._corr(message)
        self.ended_blind = False
        for attempt in (0, 1):
            self._write([message])
            try:
                return self._recv_for(corr)
            except (cobs.CorruptFrame, TimeoutError, FramingLost) as e:
                if isinstance(e, cobs.CorruptFrame):
                    self.corrupt += 1
                self._recover()
                if attempt or (message[0] & m.ROLE_SESSION and self.ended_blind):
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
        replies: list[bytes] = []
        try:
            return self._exchange_once(messages, max_inflight, window_bytes, replies)
        except (cobs.CorruptFrame, TimeoutError, FramingLost):
            if self.ended_blind:
                raise
            self.retries += 1
            rest = messages[len(replies):]
            return replies + self._exchange_once(rest, max_inflight, window_bytes, [])

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
        return lambda msgs: self.exchange(msgs, limits["max_inflight"], limits["window"])

    def attach_host(self, hst) -> None:
        """Bind to a host: its correlation counter and blind stops for the resync, and after a confirm, the probe's
        limits (in-flight, window, max_frame) for pipelining and the framing check."""
        self.corr_source = hst.next_corr
        self.blind = hst.blind_stop
        hst.link = self
        limits = hst.confirm()
        hst.exchange = self.bind(limits)
        if self.framing == "length" and limits.get("max_frame"):
            self.frames.max_frame = limits["max_frame"]

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


def open_host(target: str, timeout: float = 3.0):
    """A Host on `target`, with the link's pipelining bound to the probe's limits (core confirm): Host.pipeline and
    everything built on it (flash, capture reads) then keep several requests in flight.

    target: a serial port path (COM3 on Windows); tcp://HOST:PORT for a local broker (length frames); usb[:VID:PID[:SERIAL]]
    (hex) for the probe's USB device, vendor bulk then HID (oep-core §3.3)."""
    from . import host
    if target.startswith("tcp://"):
        addr, _, port = target[len("tcp://"):].rpartition(":")
        lk = SerialLink.on_stream(TcpStream(addr or "127.0.0.1", int(port)), "length", timeout)
        lk.transport = "tcp"
    elif target == "usb" or target.startswith("usb:"):
        parts = target.split(":")[1:]
        vid = int(parts[0], 16) if parts else USB_VID
        pid = int(parts[1], 16) if len(parts) > 1 else USB_PID
        return open_usb_host(vid, pid, parts[2] if len(parts) > 2 else None, timeout)
    else:
        lk = SerialLink(target, timeout)
        lk.transport = "serial"
    hst = host.Host(lk.send)
    lk.attach_host(hst)
    return hst
