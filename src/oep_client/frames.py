"""Length-prefixed frames on a reliable byte stream (USB vendor bulk, HID, TCP): u16 length, then the message (oep-spec
oep-core §3). No CRC: a length that cannot be right, or a frame that stops half way, means the boundaries are lost -
FramingLost, and the link resyncs. Not on TCP (transports §5, C-07): a pause inside a frame is normal there and the
frame is read on; a stream that keeps its boundaries says so with `keeps_boundaries = True`."""

from __future__ import annotations

import struct
import time

from . import registry as reg

STALL_S = reg.TIMING["probe_frame_gap_ms"] / 1000   # a frame whose bytes stop this long is not coming


class FramingLost(ConnectionError):
    """The frame boundaries are lost: an impossible length, a frame that stalled half way, a result for another
    request. The link reads and discards until the input is quiet, then confirms (transports §5)."""


class LengthFrames:
    def __init__(self, stream, max_frame: int = 0xFFFF):
        self._stream = stream
        self._buffer = bytearray()
        self._last_rx = time.monotonic()
        self.max_frame = max_frame
        # TCP keeps the boundaries: a frame that pauses is read on, never taken for a lost one (transports §5)
        self.stall_s: float | None = None if getattr(stream, "keeps_boundaries", False) else STALL_S

    def send_many(self, messages) -> None:
        """Several frames in one write. A frame may be split over writes (transports §2), but no sender pauses
        probe_frame_gap_ms inside one off TCP - and separate writes over usbipd can be 100 ms apart - so this host
        writes them whole, together."""
        chunk = bytearray()
        for message in messages:
            if not message or len(message) > self.max_frame:
                raise ValueError(f"message length {len(message)} outside 1..{self.max_frame}")
            chunk += struct.pack("<H", len(message)) + message
        if chunk:
            self._stream.write(bytes(chunk))

    def _fill(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        self._stream.timeout = min(remaining, 0.05)
        data = self._stream.read(1)
        if not data:
            return False
        waiting = getattr(self._stream, "in_waiting", 0)
        if waiting:
            data += self._stream.read(waiting)
        self._buffer += data
        self._last_rx = time.monotonic()
        return True

    def recv(self, timeout: float) -> bytes | None:
        """The next message, or None when nothing arrived by the timeout. Raises FramingLost for a length above
        max_frame or a frame whose bytes stopped for STALL_S (length 0 is the reserved keepalive: skipped). With no
        stall rule (TCP) a frame still coming at the timeout is kept for the next call and None returned."""
        deadline = time.monotonic() + timeout
        while True:
            if len(self._buffer) >= 2:
                length = self._buffer[0] | (self._buffer[1] << 8)
                if length == 0:
                    del self._buffer[:2]
                    continue
                if length > self.max_frame:
                    raise FramingLost(f"frame length {length} exceeds {self.max_frame}")
                if len(self._buffer) >= 2 + length:
                    message = bytes(self._buffer[2:2 + length])
                    del self._buffer[:2 + length]
                    return message
            if not self._fill(deadline):
                if self.stall_s is None:
                    if time.monotonic() >= deadline:
                        return None                                # TCP: the rest comes later, the buffer is kept
                    continue
                if self._buffer and time.monotonic() - self._last_rx >= self.stall_s:
                    raise FramingLost(f"a frame stopped after {len(self._buffer)} bytes")
                if time.monotonic() >= deadline:
                    if self._buffer:
                        raise FramingLost(f"a frame stopped after {len(self._buffer)} bytes")
                    return None

    def discard_until_quiet(self, quiet_s: float, limit_s: float) -> bool:
        """Read and throw away until nothing arrives for quiet_s. -> False if the input never went quiet in limit_s."""
        self._buffer.clear()
        end = time.monotonic() + limit_s
        while time.monotonic() < end:
            self._stream.timeout = quiet_s
            if not self._stream.read(max(1, getattr(self._stream, "in_waiting", 0))):
                return True
        return False

    def discard_input(self) -> None:
        self._buffer.clear()
        reset = getattr(self._stream, "reset_input_buffer", None)
        if reset:
            reset()
