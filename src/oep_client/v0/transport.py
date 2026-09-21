"""Length-prefixed frames over a pyserial-like object (read/write/in_waiting/timeout)."""

from __future__ import annotations

import struct
import time


class FrameTransport:
    """The stream must be reliable (USB CDC, USB-Serial/JTAG, TCP). No CRC, no resync:
    when framing is lost the caller reopens the connection."""

    def __init__(self, stream, max_frame: int = 0xFFFF):
        self._stream = stream
        self._buffer = bytearray()
        self.max_frame = max_frame
        self.bytes_sent = 0
        self.bytes_received = 0

    def send(self, message: bytes) -> None:
        if not message or len(message) > self.max_frame:
            raise ValueError(f"message length {len(message)} outside 1..{self.max_frame}")
        self._stream.write(struct.pack("<H", len(message)) + message)
        self.bytes_sent += len(message) + 2

    def _fill(self, deadline: float) -> bool:
        """Read whatever is available; block for the first byte until deadline."""
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
        self.bytes_received += len(data)
        return True

    def recv(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while True:
            if len(self._buffer) >= 2:
                length = self._buffer[0] | (self._buffer[1] << 8)
                if length == 0:
                    del self._buffer[:2]
                    continue
                if length > self.max_frame:
                    raise ConnectionError(f"frame length {length} exceeds max_frame {self.max_frame}; framing lost")
                if len(self._buffer) >= 2 + length:
                    message = bytes(self._buffer[2:2 + length])
                    del self._buffer[:2 + length]
                    return message
            if not self._fill(deadline):
                if time.monotonic() >= deadline:
                    return None

    def discard_input(self) -> None:
        self._buffer.clear()
        reset = getattr(self._stream, "reset_input_buffer", None)
        if reset:
            reset()
