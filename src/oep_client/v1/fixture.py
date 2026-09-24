"""oep.fixture.gpio / uart / capture: the v0 payloads under their v1 names (oep-spec v1-core-wire-delta: the fixture
payloads stay as they were until a need to change them appears). Packed here directly - no v0 codec import."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import host as h
from .core import Interface


class Gpio(Interface):
    """oep.fixture.gpio. The open-drain modes never drive a line high: the way to move a target's reset line."""
    NAME = "oep.fixture.gpio"
    CONFIGURE, READ_BANK = 0x01, 0x02
    INPUT, INPUT_PULLUP, INPUT_PULLDOWN, INPUT_PULLUPDOWN, OUTPUT_LOW, OUTPUT_HIGH, OPEN_DRAIN_LOW, OPEN_DRAIN_RELEASE = range(8)

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    @staticmethod
    def _configure_body(channel: int, mode: int) -> bytes:
        return struct.pack("<BB", channel, mode)

    def configure(self, channel: int, mode: int) -> None:
        self._call(self.CONFIGURE, self._configure_body(channel, mode))

    def pull_low(self, channel: int) -> None:
        self.configure(channel, self.OPEN_DRAIN_LOW)

    def release(self, channel: int) -> None:
        self.configure(channel, self.OPEN_DRAIN_RELEASE)

    def request_release(self, channel: int) -> tuple[int, int, bytes]:
        """The release as a raw request, to pipeline with whatever must follow it at once."""
        return self.request(self.CONFIGURE, self._configure_body(channel, self.OPEN_DRAIN_RELEASE))

    def pulse_low(self, channel: int, low_s: float = 0.02) -> None:
        """Open-drain low, then released to Hi-Z (never driven high)."""
        self.pull_low(channel)
        try:
            time.sleep(low_s)
        finally:
            self.release(channel)


class FixtureUartIO(Interface):
    """oep.fixture.uart as a plain byte stream, after a plan gave it RX / TX. Reads consume, so they take the lock."""
    NAME = "oep.fixture.uart"
    CONFIGURE, WRITE, READ = 0x01, 0x02, 0x03
    MAX_READ, MAX_WRITE = 480, 256            # fit the smallest probe frame in use (V003: 512 bytes)

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    def configure(self, baud: int) -> None:
        self._call(self.CONFIGURE, struct.pack("<I", baud))

    def read(self, n: int = 512) -> bytes:
        return self._call(self.READ, struct.pack("<H", min(n, self.MAX_READ))).payload

    def write(self, data: bytes) -> None:
        while data:
            written = struct.unpack_from("<H", self._call(self.WRITE, data[:self.MAX_WRITE]).payload)[0]
            data = data[written:]
            if not written:
                time.sleep(0.005)             # the UART has not taken the last chunk yet


@dataclass
class CaptureStatus:
    flags: int
    samples: int
    CONFIGURED, RUNNING, COMPLETE, ERROR = 1, 2, 4, 8


class Capture(Interface):
    """oep.fixture.capture: sampled logic capture, one byte per sample (bit k = plan role / line k)."""
    NAME = "oep.fixture.capture"
    CONFIGURE, ARM, STATUS, READ = 0x01, 0x02, 0x03, 0x04
    READ_CHUNK = 900

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    def configure(self, sample_rate_hz: int, samples: int) -> tuple[int, int, int]:
        """-> (actual sample rate, samples, lines)."""
        return struct.unpack_from("<IIB", self._call(self.CONFIGURE, struct.pack("<II", sample_rate_hz, samples)).payload)

    def arm(self) -> None:
        self._call(self.ARM)

    def status(self) -> CaptureStatus:
        return CaptureStatus(*struct.unpack_from("<BI", self._call(self.STATUS, locked=False).payload))

    def wait(self, timeout: float = 5.0) -> CaptureStatus:
        deadline = time.monotonic() + timeout
        while True:
            st = self.status()
            if st.flags & (CaptureStatus.COMPLETE | CaptureStatus.ERROR) or time.monotonic() > deadline:
                return st

    def read_all(self, samples: int) -> bytes:
        """The whole capture, pipelined in READ_CHUNK pieces (reads are lock-free and re-sent once if corrupt)."""
        reqs = [self.request(self.READ, struct.pack("<IH", off, min(self.READ_CHUNK, samples - off)))
                for off in range(0, samples, self.READ_CHUNK)]
        return b"".join(r.payload for r in self.host.pipeline_calls(reqs, locked=False))
