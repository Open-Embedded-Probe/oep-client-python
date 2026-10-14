"""The byte side of a virtual bench probe's serial port (transports §1, §4): COBS frames, and nothing else, on one port.

`VirtualSerialPort(endpoint, index)` is serial port `index` of an `endpoint.Endpoint`. Bytes from the host go to
`feed`; what the probe sends comes from `output`. It does what a probe does:

- a candidate runs from a 0x00 to the next 0x00; if it decodes and its CRC matches it is a request, otherwise it
  is dropped, and the closing 0x00 starts the next candidate; bytes outside a candidate are dropped; a candidate that
  stops for 200 ms (`tick`) is dropped; a candidate that is only its 0x00 (0x00 0x00, or the closing 0x00 of a frame
  when nothing follows) is nothing (a serial transport carries OEP frames only, transports §4)
- a message over max_frame (or a candidate longer than any frame) is dropped with what follows until 200 ms pass
  with nothing (transports §1)
- answers go out as 0x00 <COBS> 0x00

`answer_filter(n, message) -> bytes | None` may change how the n-th answer (1-based) goes on the wire: the framed
bytes to send, or None to send nothing (fault injection for tests).

port_speed (oep-if-link §3): every closed candidate tells the endpoint whether it was a frame (`Endpoint.speed_frame`), a
switch or revert a request asked for happens once its answer is queued (at the old speed) - so does the restart oep.probe.restart's
restart asks for (oep-if-restart §2: the answer stays queued, the bytes read behind the request are dropped) - and the endpoint's
`broken_rates` break frames at the port's rate now: a candidate from the host is then not a frame, an answer or push
goes out with a spoiled CRC (a `duplex` one only while a request of its size comes in with such an answer still
unread: the request, or that answer, breaks). `VirtualSerialStream` is the port as a pyserial-shaped stream for an in-process host, with
the host's own `baudrate`: while it differs from the probe's, every byte either way arrives garbled.
"""

from __future__ import annotations

import struct
from typing import Callable

from . import cobs, registry as reg

GAP_MS = reg.TIMING["probe_frame_gap_ms"]
UART_BRIDGE = reg.CORE.enum["transport_kind"]["uart_bridge"]   # the kind whose line has a rate (core §7.5)


class VirtualSerialPort:
    def __init__(self, ep, index: int, answer_filter: Callable[[int, bytes], bytes | None] | None = None):
        self.ep, self.index = ep, index
        self.answer_filter = answer_filter
        self.cand: bytearray | None = None           # the candidate so far, its leading 0x00 included
        self.last_ms = 0
        self.limit = 2 * ep.probe.max_frame + 16     # longer than any COBS frame of max_frame: raw
        self.frames: list[bytes] = []                # framed answers waiting to go out
        self.notice: list[tuple | None] = []         # per waiting frame: (fn, its subscription's epoch), None: an answer
        self.dropped_notices = 0                     # notifications dropped before they started (core §10.4 rule 2)
        self.discarding = False
        self.answers = 0
        self.garble: Callable[[bytes, int], bytes] | None = None   # (frame, the rate it goes at) -> what arrives
        self.spoiled: set[int] = set()               # the waiting answers a duplex BrokenRate already broke

    def feed(self, data: bytes) -> None:
        now = self.ep.now()
        self._gap(now)
        if self.discarding:
            self.last_ms = now
            return
        raw = bytearray()
        boots = self.ep.reboots
        for b in data:
            if b == 0:
                if self.cand is not None:
                    self._close(raw)
                    if self.discarding:
                        self.last_ms = now
                        return
                    if self.ep.reboots != boots:
                        # the frame was a restart (oep-if-restart §2): its answer is queued, the probe restarted after it, and
                        # what came behind it on the line is lost with the old boot (as `reboot` drops)
                        self.cand = None
                        self.last_ms = now
                        return
                self.cand = bytearray(b"\x00")
            elif self.cand is not None:
                self.cand.append(b)
                if len(self.cand) > self.limit:            # longer than any frame of max_frame: dropped with what
                    self.discarding, self.cand, self.last_ms = True, None, now   # follows until a gap (transports §1)
                    return
            else:
                raw.append(b)
        self.last_ms = now
        self._raw(raw)

    def _close(self, raw: bytearray) -> None:
        body = bytes(self.cand[1:])
        self.cand = None
        if not body:
            return                                   # 0x00 0x00: an empty frame
        size = len(body) + 2
        duplex = self._both_ways(size)
        try:
            if self.ep.breaks(self.index, size, to_host=False, duplex=duplex):
                raise cobs.CorruptFrame("the line's rate breaks this frame")
            msg = cobs.unframe(body)
        except cobs.CorruptFrame:
            raw += b"\x00" + body                    # not a frame: raw, the leading 0x00 too
            self.ep.speed_frame(self.index, False)
            return
        if len(msg) > self.ep.probe.max_frame:       # over max_frame: dropped with what follows until a gap (§1)
            self.discarding, self.cand = True, None
            return
        self.ep.speed_frame(self.index, True)
        self._raw(raw)                               # the raw bytes before the frame go first
        raw.clear()
        try:
            result = self.ep.handle(msg, self.index)
        except ValueError:
            return                                   # not a request (a role the probe does not take): dropped
        if result is None:
            return
        self.answers += 1
        wire = cobs.frame(result)
        if self.answer_filter is not None:
            wire = self.answer_filter(self.answers, result)
        if wire:
            self.frames.append(self._line(wire))
            self.notice.append(None)
        self.ep.after_answer()                       # now the answer is out: a port_speed switch or revert, a restart

    def reboot(self) -> None:
        """The probe behind the port restarted (`Endpoint.reboot`): the candidate it was reading and the answers it had
        not sent yet are gone."""
        self.cand = None
        self.discarding = False
        self.frames.clear()
        self.notice.clear()
        self.spoiled.clear()

    def _both_ways(self, size: int) -> bool:
        """A request of `size` bytes came in: with a BrokenRate that breaks only both ways at once (`duplex`), whether
        an answer of its min_size or more is still unread (both ways busy) - and then that answer breaks if the rate
        breaks frames towards the host."""
        b = self.ep.duplex_rate(self.index)
        if b is None or size < b.min_size:
            return False
        big = [k for k, f in enumerate(self.frames) if len(f) >= b.min_size]
        if not big:
            return False
        k = big[-1]                                  # the answer on the line now (the latest); never spoiled twice
        if k not in self.spoiled and self.ep.breaks(self.index, len(self.frames[k]), to_host=True, duplex=True):
            self.frames[k] = _spoil(self.frames[k])
            self.spoiled.add(k)
        return True

    def _gap(self, now: int) -> None:
        if self.discarding and now - self.last_ms >= GAP_MS:
            self.discarding = False
        if self.cand is not None and now - self.last_ms >= GAP_MS:
            raw, self.cand = bytes(self.cand), None
            if len(raw) > 1:                         # only its 0x00: a delimiter (a frame's closing 0x00), not raw
                self._raw(bytearray(raw))

    def _raw(self, raw: bytearray) -> None:
        """Bytes that are no frame: dropped (transports §4)."""

    def tick(self) -> None:
        self._gap(self.ep.now())
        self.ep.tick()
        self._drop_replaced()
        limit = 2 * self.ep.probe.max_frame          # notifications waiting at most (core §10.4 rule 2)
        for f in self.ep.pushes():                   # events and data the probe sends by itself (core §10)
            wire = self._line(cobs.frame(f))
            waiting = sum(len(w) for w, n in zip(self.frames, self.notice) if n is not None)
            if waiting + len(wire) > limit:
                self.dropped_notices += 1            # dropped before it starts: its seq shows the gap (§10.2)
                continue
            fn = struct.unpack_from("<H", f, 1)[0]
            self.frames.append(wire)
            self.notice.append((fn, self.ep.sub_epoch.get(fn, 0)))

    def _drop_replaced(self) -> None:
        """The waiting notifications of a subscription replaced or ended since: dropped unsent (core §10.3; a frame
        handed to `output` has started and goes whole)."""
        keep = [k for k, n in enumerate(self.notice) if n is None or self.ep.sub_epoch.get(n[0], 0) == n[1]]
        if len(keep) == len(self.frames):
            return
        self.frames = [self.frames[k] for k in keep]
        self.notice = [self.notice[k] for k in keep]
        self.spoiled = {keep.index(k) for k in self.spoiled if k in keep}

    def _line(self, wire: bytes) -> bytes:
        """A framed message as the line delivers it at the port's rate now (`Endpoint.broken_rates`, `garble`)."""
        if self.ep.breaks(self.index, len(wire), to_host=True):
            wire = _spoil(wire)
        return self.garble(wire, self.ep.port_baud(self.index)) if self.garble else wire

    def output(self, room: int = 4096) -> bytes:
        """What the probe sends now: the waiting answers (up to `room`, at least one frame)."""
        self._drop_replaced()
        out = bytearray()
        while self.frames and len(out) + len(self.frames[0]) <= max(room, len(self.frames[0])):
            out += self.frames.pop(0)
            self.notice.pop(0)
            self.spoiled = {k - 1 for k in self.spoiled if k}
            if len(out) >= room:
                return bytes(out)
        return bytes(out)


def _spoil(wire: bytes) -> bytes:
    """A framed message with its CRC's high byte spoiled (a frame the line broke); bytes that are no frame as they are."""
    try:
        body = bytearray(cobs.decode(wire[1:-1]))
    except cobs.CorruptFrame:
        return wire
    body[-1] ^= 0xFF
    return b"\x00" + cobs.encode(bytes(body)) + b"\x00"


def _garble(data: bytes) -> bytes:
    """Bytes sent at one rate and read at another: a different byte for every byte, 0x00s among them (so the reader
    sees broken candidates, not silence)."""
    return bytes((b * 37 + 11) & 0xFF if b % 5 else 0 for b in data)


class VirtualSerialStream:
    """Serial port `index` of an endpoint as a pyserial-shaped stream (read / write / in_waiting / baudrate /
    reset_input_buffer), for a host in the same process. Every read and write lets the probe run (`tick`). The host's
    `baudrate` (what it opened the port with, then set) is compared with the probe's rate on that port
    (`Endpoint.port_baud`): while they differ, what either side sends arrives garbled."""

    def __init__(self, ep, index: int, baudrate: int = 115200, answer_filter=None):
        self.ep, self.index = ep, index
        self.port = VirtualSerialPort(ep, index, answer_filter)
        self.port.garble = lambda wire, rate: wire if self._matched() else _garble(wire)   # sent at that rate
        self.baudrate = baudrate
        self.timeout = 0.05
        self._rx = bytearray()
        self.closed = False

    def _matched(self) -> bool:
        """The rates match - or the port is no UART bridge: a USB serial port takes any line coding (transports §4)."""
        return self.baudrate == self.ep.port_baud(self.index) or self.ep.transports.get(self.index) != UART_BRIDGE

    def _pull(self) -> None:
        self.port.tick()
        self._rx += self.port.output()             # frames already as the line delivered them (garble)

    @property
    def in_waiting(self) -> int:
        self._pull()
        return len(self._rx)

    def read(self, n: int = 1) -> bytes:
        import time
        deadline = time.monotonic() + (self.timeout or 0)
        self._pull()
        while not self._rx and time.monotonic() < deadline:
            time.sleep(0.001)
            self._pull()
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def write(self, data: bytes) -> int:
        self.port.feed(bytes(data) if self._matched() else _garble(bytes(data)))
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._pull()
        self._rx.clear()

    def close(self) -> None:
        self.closed = True
