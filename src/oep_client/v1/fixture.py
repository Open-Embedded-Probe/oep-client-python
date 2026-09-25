"""oep.fixture.gpio and oep.fixture.uart, revision 1 (oep-spec v1-core-wire-delta §5.8). The capture is in `capture`
(oep.fixture.capture revision 1 = logic-capture's basic set).

Plan roles stay as they were: gpio 1 = line; uart 1 = RX, 2 = TX. Only channels the plan assigned can be used.
"""

from __future__ import annotations

import struct
import time

from . import host as h, message as m, registry as reg
from .console import PositionStream, StreamIO
from .core import Interface

_GPIO, _UART = reg.FIXTURE_GPIO, reg.FIXTURE_UART
_MODE = _GPIO.enum["mode"]


class Gpio(Interface):
    """oep.fixture.gpio. `set` applies (channel, mode) pairs in order in one request (pull NRST, then release it); a
    channel the plan did not assign or a mode the probe lacks rejects the whole list as unavailable (payload: its
    index). The open-drain modes never drive a line high: the way to move a target's reset line."""
    NAME = "oep.fixture.gpio"
    REVISION = 1
    SET, READ = _GPIO.op["set"], _GPIO.op["read"]
    INPUT, INPUT_PULLUP, INPUT_PULLDOWN = _MODE["input"], _MODE["input_pullup"], _MODE["input_pulldown"]
    OUTPUT_LOW, OUTPUT_HIGH = _MODE["output_low"], _MODE["output_high"]
    OPEN_DRAIN_LOW, OPEN_DRAIN_RELEASE = _MODE["open_drain_low"], _MODE["open_drain_release"]

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    @staticmethod
    def set_body(pairs: list[tuple[int, int]]) -> bytes:
        return struct.pack("<B", len(pairs)) + b"".join(struct.pack("<HB", ch, mode) for ch, mode in pairs)

    def set(self, pairs: list[tuple[int, int]]) -> None:
        """[(channel, mode)], applied in order."""
        self._call(self.SET, self.set_body(pairs))

    def configure(self, channel: int, mode: int) -> None:
        self.set([(channel, mode)])

    def read(self, channels: list[int]) -> list[int]:
        """-> one level (0 / 1) per channel. Lock-free."""
        rd = m.Reader(self._call(self.READ, struct.pack("<B", len(channels))
                                 + b"".join(struct.pack("<H", c) for c in channels), locked=False).payload)
        levels = list(rd.bytes(len(channels)))
        rd.tail()
        return levels

    def pull_low(self, channel: int) -> None:
        self.set([(channel, self.OPEN_DRAIN_LOW)])

    def release(self, channel: int) -> None:
        self.set([(channel, self.OPEN_DRAIN_RELEASE)])

    def request_release(self, channel: int) -> tuple[int, int, bytes]:
        """The release as a raw request, to pipeline with whatever must follow it at once."""
        return self.request(self.SET, self.set_body([(channel, self.OPEN_DRAIN_RELEASE)]))

    def pulse_low(self, channel: int, low_s: float = 0.02) -> None:
        """Open-drain low, then released to Hi-Z (never driven high)."""
        self.pull_low(channel)
        try:
            time.sleep(low_s)
        finally:
            self.release(channel)


class FixtureUart(PositionStream):
    """oep.fixture.uart: one position stream per fn, like the console without a stream byte. Received bytes are kept
    from configure until plan_release, whatever the session; reads are lock-free and do not consume. TX idles high
    before configure and after plan_release."""
    NAME = "oep.fixture.uart"
    REVISION = 1
    CONFIGURE = _UART.op["configure"]
    TAG_FORMAT = _UART.tlv["configure"]["format"]
    # format bits: data bits (0 = 8, 1 = 7), parity (0 none, 1 even, 2 odd) << 2, stop bits (0 = 1, 1 = 2) << 4
    EIGHT_N_1 = 0x00

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    @staticmethod
    def format_byte(data_bits: int = 8, parity: str = "N", stop_bits: int = 1) -> int:
        return ({8: 0, 7: 1}[data_bits] | {"N": 0, "E": 1, "O": 2}[parity.upper()] << 2 | {1: 0, 2: 1}[stop_bits] << 4)

    def configure(self, baud: int, fmt: int | None = None) -> int:
        """-> the actual baud. fmt (format_byte()) goes as a critical TLV: a probe that cannot set it refuses rather
        than running 8N1; None leaves the default 8N1."""
        body = struct.pack("<I", baud)
        if fmt is not None:
            body += m.tlv(self.TAG_FORMAT, bytes([fmt]), critical=True)
        rd = m.Reader(self._call(self.CONFIGURE, body).payload)
        actual = rd.u32()
        rd.tail()
        return actual


class FixtureUartIO(StreamIO):
    """oep.fixture.uart as a plain byte stream, after a plan gave it RX / TX: configure() starts reading from the
    stream's position at that moment (earlier bytes are skipped); write() splits and waits for the UART."""
    MAX_READ, MAX_WRITE = 480, 256            # fit the smallest probe frame in use (V003: 512 bytes)

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        self.uart = FixtureUart(hst, fn, name)
        self.source, self.position, self.lost = self.uart, None, 0
        self.baud = 0

    def configure(self, baud: int, fmt: int | None = None) -> int:
        self.baud = self.uart.configure(baud, fmt)
        self.position = self.uart.read(PositionStream.FROM_NOW, 0, 0).start
        return self.baud

    def read(self, n: int = 512) -> bytes:
        if self.position is None:                  # not configured here: read from the oldest byte kept
            self.position = self.uart.read(PositionStream.FROM_OLDEST, 0, 0).start
        return super().read(n)
