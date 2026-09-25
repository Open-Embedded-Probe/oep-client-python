"""v1 draft transport over a serial port.

Framing follows the path: length-prefixed frames on a reliable stream (USB CDC, USB-Serial/JTAG), COBS + CRC-16
behind a USB-UART bridge, where bytes are dropped or changed without an error (oep-spec probe-development-guide
§3). The bridge is recognised by its USB VID:PID unless `framing` says otherwise.

The port opens with pyserial's defaults - DTR and RTS asserted - which reset none of the measured probes
(host-development-guide §1). No sleep after open: the probe does not restart.

A corrupt or missing reply to a request without a session id (lock-free reads, open) is asked for once more; a
state-changing request is never re-sent here, because a re-send may run it twice (session-and-exclusivity).
Replies are matched to requests by correlation id: one that belongs to an earlier request (the late answer to a
re-sent read, the tail of an exchange that failed) is read past, so the link cannot fall one reply behind.
"""

from __future__ import annotations

import collections
import os
import time

import serial
from serial.tools import list_ports

from . import cobs
from .frames import LengthFrames

# USB-UART bridges: their bytes are not protected end to end, so the probe behind them speaks COBS + CRC.
UART_BRIDGES = {
    (0x1A86, 0x7523): "CH340", (0x1A86, 0x7522): "CH340K", (0x1A86, 0x55D3): "CH343", (0x1A86, 0x55D4): "CH9102",
    (0x10C4, 0xEA60): "CP210x", (0x0403, 0x6001): "FT232R", (0x0403, 0x6015): "FT231X", (0x067B, 0x2303): "PL2303",
}


def framing_for(port: str) -> str:
    real = os.path.realpath(port)
    for p in list_ports.comports():
        if os.path.realpath(p.device) == real and p.vid is not None:
            return "cobs" if (p.vid, p.pid) in UART_BRIDGES else "length"
    return "length"


class SerialLink:
    def __init__(self, port: str, timeout: float = 3.0, exclusive: bool = True, framing: str | None = None):
        # exclusive: a second process on the same Linux tty would interleave bytes with ours
        self.stream = serial.Serial(port, 115200, timeout=0.05, exclusive=exclusive)
        self.framing = framing or framing_for(port)
        self.timeout = timeout
        self.retries = 0
        self.corrupt = 0
        self.stale = 0                             # replies read past because they answered an earlier request
        self.dropped = 0                           # probe-initiated frames of a role this client does not handle
        self.pushes: collections.deque[bytes] = collections.deque()   # experimental role 0x06 frames, oldest first
        self.events: collections.deque[bytes] = collections.deque()   # experimental role 0x05 frames, oldest first
        if self.framing == "length":
            self.frames = LengthFrames(self.stream)
            self.frames.discard_input()
        else:
            self._buf = bytearray()
            self.stream.reset_input_buffer()

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
        """Frames that are not results: data pushes and events are kept, other roles dropped. True if `frame` was one of them.
        Only a result (role 0x02) carries a correlation id; matching anything else by its bytes 1-2 would take a
        push for a reply whenever its fn happened to equal the id."""
        if frame and frame[0] == 0x02:
            return False
        if frame and frame[0] == 0x06:
            self.pushes.append(frame)
        elif frame and frame[0] == 0x05:
            self.events.append(frame)
        else:
            self.dropped += 1
        return True

    def _recv_for(self, corr: int) -> bytes:
        """The reply to request `corr`, reading past replies left over from earlier requests (and routing pushes)."""
        while True:
            reply = self._recv()
            if self._route(reply):
                continue
            if len(reply) >= 3 and self._corr(reply) == corr:
                return reply
            self.stale += 1

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
                n += 1
                if not self._route(frame):
                    self.stale += 1
                if until_one or time.monotonic() >= deadline:
                    return n
        finally:
            self.timeout = saved

    def _clear(self) -> None:
        if self.framing == "length":
            self.frames.discard_input()
        else:
            self._buf.clear()
            self.stream.reset_input_buffer()

    def send(self, message: bytes) -> bytes:
        corr = self._corr(message)
        for attempt in (0, 1):
            self._write([message])
            try:
                return self._recv_for(corr)
            except (cobs.CorruptFrame, TimeoutError) as e:
                if isinstance(e, cobs.CorruptFrame):
                    self.corrupt += 1
                if attempt or message[0] & 0x80:      # state-changing: never re-sent here
                    raise
                self.retries += 1

    def exchange(self, messages: list[bytes], max_inflight: int, window_bytes: int) -> list[bytes]:
        """Pipelined: keep up to max_inflight requests and window_bytes outstanding, results in order.

        The probe answers in the order it received (v1 draft); each reply is still matched by correlation id.
        Frames admitted together go out in one write (E160). No re-send here; after an error the input is cleared
        so the next request does not read this exchange's leftovers.
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
        except (cobs.CorruptFrame, TimeoutError):
            time.sleep(0.05)
            self._clear()
            raise
        return replies

    def bind(self, limits: dict):
        """This link's exchange with the probe's limits (core confirm), for Host(exchange=...)."""
        return lambda msgs: self.exchange(msgs, limits["max_inflight"], limits["window"])

    def close(self) -> None:
        self.stream.close()


def open_host(port: str, **kwargs):
    """A Host on this port with the link's pipelining bound to the probe's limits (core confirm): Host.pipeline and
    everything built on it (flash, capture reads) then keep several requests in flight."""
    from . import core, host
    lk = SerialLink(port, **kwargs)
    hst = host.Host(lk.send)
    hst.exchange = lk.bind(core.confirm(hst))
    hst.link = lk
    return hst
