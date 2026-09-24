"""Length-prefixed frames on a reliable byte stream (USB CDC, USB-Serial/JTAG): u16 length, then the message."""

from __future__ import annotations

import struct
import time


class LengthFrames:
    """No CRC and no resync inside a frame: a length that cannot be right means framing is lost."""

    def __init__(self, stream, max_frame: int = 0xFFFF):
        self._stream = stream
        self._buffer = bytearray()
        self.max_frame = max_frame

    def send_many(self, messages) -> None:
        """Several frames in one write: the peer finds the boundaries from the length prefixes."""
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
                    raise ConnectionError(f"frame length {length} exceeds {self.max_frame}; framing lost")
                if len(self._buffer) >= 2 + length:
                    message = bytes(self._buffer[2:2 + length])
                    del self._buffer[:2 + length]
                    return message
            if not self._fill(deadline) and time.monotonic() >= deadline:
                return None

    def discard_input(self) -> None:
        self._buffer.clear()
        reset = getattr(self._stream, "reset_input_buffer", None)
        if reset:
            reset()
