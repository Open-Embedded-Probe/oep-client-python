"""v1 transport over a serial port or a USB vendor bulk pair (oep-spec v1-core-wire-delta §1).

Framing follows the path: length-prefixed frames on a reliable stream (USB CDC, USB-Serial/JTAG, vendor bulk), COBS +
CRC-16 behind a USB-UART bridge, where bytes are dropped or changed without an error (oep-spec probe-development-guide
§3). The bridge is recognised by its USB VID:PID unless `framing` says otherwise.

The port opens with pyserial's defaults - DTR and RTS asserted - which reset none of the measured probes
(host-development-guide §1). No sleep after open: the probe does not restart.

Length frames carry no CRC, so lost boundaries are recovered by the §1 resync: on a result for another request, an
impossible length or a frame that stops half way, read and discard until the input is quiet for 50 ms, prove the link
with a confirm, and go on. When pushes keep the input from going quiet, the host's unsubscribe and end (harmless twice)
are sent blind. A request without a session id (lock-free reads, open) is sent once more after a corrupt or missing
reply; a state-changing request is never re-sent, because a re-send may run it twice (session-and-exclusivity).
Behind COBS, frames carry their own boundaries and CRC: a reply to an earlier request is read past there.
"""

from __future__ import annotations

import collections
import os
import time

import serial
from serial.tools import list_ports

from . import cobs, message as m, registry as reg
from .frames import FramingLost, LengthFrames

# USB-UART bridges: their bytes are not protected end to end, so the probe behind them speaks COBS + CRC.
UART_BRIDGES = {
    (0x1A86, 0x7523): "CH340", (0x1A86, 0x7522): "CH340K", (0x1A86, 0x55D3): "CH343", (0x1A86, 0x55D4): "CH9102",
    (0x10C4, 0xEA60): "CP210x", (0x0403, 0x6001): "FT232R", (0x0403, 0x6015): "FT231X", (0x067B, 0x2303): "PL2303",
}

RESYNC_QUIET_S = reg.TIMING["resync_quiet_ms"] / 1000


class CorrMismatch(FramingLost):
    """A result for a request other than the one waited for."""


def framing_for(port: str) -> str:
    real = os.path.realpath(port)
    for p in list_ports.comports():
        if os.path.realpath(p.device) == real and p.vid is not None:
            return "cobs" if (p.vid, p.pid) in UART_BRIDGES else "length"
    return "length"


class SerialLink:
    NOISY_S = 1.0          # a resync that is still not quiet after this sends the blind stops

    def __init__(self, port: str, timeout: float = 3.0, exclusive: bool = True, framing: str | None = None):
        # exclusive: a second process on the same Linux tty would interleave bytes with ours
        stream = serial.Serial(port, 115200, timeout=0.05, exclusive=exclusive)
        self._setup(stream, framing or framing_for(port), timeout)

    @classmethod
    def on_stream(cls, stream, framing: str = "length", timeout: float = 3.0) -> SerialLink:
        """A link on any pyserial-shaped stream (a USB bulk pair, a test's scripted stream)."""
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
        self.resyncs = 0
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
                del self._buf[:end + 1]
                if not raw:
                    continue
                return cobs.unframe(raw)             # raises CorruptFrame
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
        request raises CorrMismatch (the §1 resync follows); COBS: it is read past."""
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
        """v1 wire §1: read and discard until the input is quiet for 50 ms, then prove the link with a confirm (a read,
        safe to send), then go on. Never re-sends a state-changing request. When the input does not go quiet (pushes
        keep coming), the host's unsubscribe and end go out blind, once."""
        self.resyncs += 1
        if self.framing != "length":
            time.sleep(RESYNC_QUIET_S)
            self._buf.clear()
            self.stream.reset_input_buffer()
            return
        blind_sent = False
        for _ in range(tries):
            while not self.frames.discard_until_quiet(RESYNC_QUIET_S, self.NOISY_S):
                if blind_sent:
                    raise ConnectionError("resync: the input never went quiet, even after unsubscribe and end")
                stops = self.blind()
                blind_sent = True
                if stops:
                    self._write(stops)
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
        """After a lost, broken or missing reply: leave the link in step for the next request."""
        if self.framing == "length":
            self.resync()
        else:
            time.sleep(RESYNC_QUIET_S)
            self._buf.clear()
            self.stream.reset_input_buffer()

    def send(self, message: bytes) -> bytes:
        corr = self._corr(message)
        for attempt in (0, 1):
            self._write([message])
            try:
                return self._recv_for(corr)
            except (cobs.CorruptFrame, TimeoutError, FramingLost) as e:
                if isinstance(e, cobs.CorruptFrame):
                    self.corrupt += 1
                self._recover()
                if attempt or message[0] & m.ROLE_SESSION:      # state-changing: never re-sent
                    raise
                self.retries += 1

    def exchange(self, messages: list[bytes], max_inflight: int, window_bytes: int) -> list[bytes]:
        """Pipelined: keep up to max_inflight requests and window_bytes outstanding, results in order.

        The probe answers in the order it received; each reply is still matched by correlation id. Frames admitted
        together go out in one write (E160). No re-send here; after an error the link resyncs so the next request
        does not read this exchange's leftovers, and the error is raised.
        """
        replies: list[bytes] = []
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


def open_usb_host(vid: int = 0x303A, pid: int = 0x4021, serial: str | None = None, timeout: float = 3.0,
                  transports: tuple[str, ...] = ("vendor", "hid")):
    """A Host on the probe's USB device (the P4's HS OTG port), trying its ways in in the v1 wire §1 order: vendor bulk,
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


def open_host(port: str, **kwargs):
    """A Host on this port with the link's pipelining bound to the probe's limits (core confirm): Host.pipeline and
    everything built on it (flash, capture reads) then keep several requests in flight."""
    from . import host
    lk = SerialLink(port, **kwargs)
    hst = host.Host(lk.send)
    lk.attach_host(hst)
    return hst
