"""v1 draft transport: length-prefixed frames over a serial port (USB CDC, USB-Serial/JTAG), one request at a time.

The port opens with pyserial's defaults - DTR and RTS asserted - which reset none of the measured probes
(oep-spec docs/host-development-guide.ja.md §1). No sleep after open: the probe does not restart.
"""

from __future__ import annotations

import serial

from ..v0.transport import FrameTransport


class SerialLink:
    def __init__(self, port: str, timeout: float = 3.0, exclusive: bool = True):
        # exclusive: a second process on the same Linux tty would interleave bytes with ours
        self.stream = serial.Serial(port, 115200, timeout=0.05, exclusive=exclusive)
        self.frames = FrameTransport(self.stream)
        self.frames.discard_input()
        self.timeout = timeout

    def send(self, message: bytes) -> bytes:
        self.frames.send(message)
        return self._recv()

    def exchange(self, messages: list[bytes], max_inflight: int, window_bytes: int) -> list[bytes]:
        """Pipelined: keep up to max_inflight requests and window_bytes outstanding, results in order.

        The probe answers in the order it received (v0 / v1 draft), so replies pair with requests by position;
        the caller still checks correlations. Frames admitted together go out in one write (E160).
        """
        replies: list[bytes] = []
        outstanding: list[int] = []        # sizes of requests in flight
        batch: list[bytes] = []
        for msg in messages:
            size = len(msg) + 2
            while outstanding and (len(outstanding) >= max_inflight or sum(outstanding) + size > window_bytes):
                if batch:
                    self.frames.send_many(batch)
                    batch = []
                replies.append(self._recv())
                outstanding.pop(0)
            batch.append(msg)
            outstanding.append(size)
        if batch:
            self.frames.send_many(batch)
        while outstanding:
            replies.append(self._recv())
            outstanding.pop(0)
        return replies

    def _recv(self) -> bytes:
        reply = self.frames.recv(self.timeout)
        if reply is None:
            raise TimeoutError("no result from the probe")
        return reply

    def close(self) -> None:
        self.stream.close()
