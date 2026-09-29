"""The byte side of a fake probe's serial port (oep-core §3.1, §3.4): COBS frames and raw bytes on one port.

`FakeSerialPort(endpoint, index)` is serial port `index` of an `endpoint.Endpoint`. Bytes from the host go to
`feed`; what the probe sends comes from `output`. It does what a probe does:

- a candidate runs from a 0x00 to the next 0x00; if it decodes and its CRC matches it is a request, otherwise it
  (with its leading 0x00) is raw bytes, and the closing 0x00 starts the next candidate; bytes outside a candidate
  are raw at once; a candidate that stops for 200 ms (`tick`) is raw; an empty candidate (0x00 0x00) is nothing
- raw bytes go to the endpoint's bind for this port (`Endpoint.port_input`)
- answers go out as 0x00 <COBS> 0x00, ahead of raw chunks (`Endpoint.port_output`); one writer, never a raw byte
  inside a frame

`answer_filter(n, message) -> bytes | None` may change how the n-th answer (1-based) goes on the wire: the framed
bytes to send, or None to send nothing (fault injection for tests).
"""

from __future__ import annotations

from typing import Callable

from . import cobs, registry as reg

GAP_MS = reg.TIMING["probe_frame_gap_ms"]


class FakeSerialPort:
    def __init__(self, ep, index: int, answer_filter: Callable[[int, bytes], bytes | None] | None = None):
        self.ep, self.index = ep, index
        self.answer_filter = answer_filter
        self.cand: bytearray | None = None           # the candidate so far, its leading 0x00 included
        self.last_ms = 0
        self.limit = 2 * ep.probe.max_frame + 16     # longer than any COBS frame of max_frame: raw
        self.frames: list[bytes] = []                # framed answers waiting to go out
        self.answers = 0

    def feed(self, data: bytes) -> None:
        now = self.ep.now()
        self._gap(now)
        raw = bytearray()
        for b in data:
            if b == 0:
                if self.cand is not None:
                    self._close(raw)
                self.cand = bytearray(b"\x00")
            elif self.cand is not None:
                self.cand.append(b)
                if len(self.cand) > self.limit:
                    raw += self.cand
                    self.cand = None
            else:
                raw.append(b)
        self.last_ms = now
        self._raw(raw)

    def _close(self, raw: bytearray) -> None:
        body = bytes(self.cand[1:])
        self.cand = None
        if not body:
            return                                   # 0x00 0x00: an empty frame
        try:
            msg = cobs.unframe(body)
        except cobs.CorruptFrame:
            raw += b"\x00" + body                    # not a frame: raw, the leading 0x00 too
            return
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
            self.frames.append(wire)

    def _gap(self, now: int) -> None:
        if self.cand is not None and now - self.last_ms >= GAP_MS:
            raw, self.cand = bytes(self.cand), None
            self._raw(bytearray(raw))

    def _raw(self, raw: bytearray) -> None:
        if raw:
            self.ep.port_input(self.index, bytes(raw))

    def tick(self) -> None:
        self._gap(self.ep.now())
        self.ep.tick()

    def output(self, room: int = 4096) -> bytes:
        """What the probe sends now: waiting answers first, then raw bytes by the bind (up to `room` in all)."""
        out = bytearray()
        while self.frames and len(out) + len(self.frames[0]) <= max(room, len(self.frames[0])):
            out += self.frames.pop(0)
            if len(out) >= room:
                return bytes(out)
        while len(out) < room:
            chunk = self.ep.port_output(self.index, min(self.ep.CHUNK, room - len(out)))
            if not chunk:
                break
            out += chunk
        return bytes(out)
