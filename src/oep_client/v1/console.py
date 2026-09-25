"""oep.target.console revision 1: the target's console as a position stream on a debug connection (oep-spec
v1-core-wire-delta §5.7, console-stream.ja.md), and ConsoleIO, the same as a plain byte stream.

The position streams of oep.target.console and oep.fixture.uart share their read / marks / clear / mark / write
operations (same numbers and meanings; the UART has no stream byte): `PositionStream` holds them, `prefix` is the
stream byte or nothing.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import host as h, message as m, registry as reg
from .core import Interface

_CON = reg.TARGET_CONSOLE


@dataclass
class Mark:
    serial: int          # per stream, wraps (u32): marks are read on by serial
    position: int
    kind: int
    time_ms: int         # probe uptime in ms (u32, wraps after ~49.7 days)
    detail: int


MARK_NAMES = {v: k.replace("_", "-") for k, v in _CON.enum["mark_kind"].items()}


@dataclass
class Chunk:
    start: int
    more: bool
    gap: bool
    data: bytes

    def __iter__(self):                      # (start, more, gap, data), as the first version returned
        return iter((self.start, self.more, self.gap, self.data))


class PositionStream(Interface):
    """read / marks / clear / mark / write of a position stream (console §5.7, fixture.uart §5.8)."""
    READ, MARKS, CLEAR, MARK, WRITE = (_CON.op[k] for k in ("read", "marks", "clear", "mark", "write"))
    FROM_POSITION, FROM_OLDEST, FROM_NOW, FROM_MARK = (_CON.enum["read_from"][k]
                                                       for k in ("position", "oldest", "now", "last_mark"))

    def _stream_prefix(self) -> bytes:
        return b""

    def read(self, start: int = FROM_OLDEST, arg: int = 0, maximum: int = 1000) -> Chunk:
        """-> Chunk(start position, more, gap, data). start: FROM_* ; arg: a position or a mark kind (0: any).
        Lock-free; reading does not consume."""
        p = self._call(self.READ, self._stream_prefix() + struct.pack("<BIH", start, arg, maximum), locked=False).payload
        rd = m.Reader(p)
        pos, flags = rd.take("IB")
        return Chunk(pos, bool(flags & 1), bool(flags & 2), rd.rest())   # data ends the result: no tail (§0)

    def read_from(self, position: int, maximum: int = 1000) -> Chunk:
        return self.read(self.FROM_POSITION, position, maximum)

    def marks_page(self, from_serial: int = 0) -> tuple[list[Mark], bool]:
        """One answer's marks with serial >= from_serial (in serial order). -> (marks, more)."""
        rd = m.Reader(self._call(self.MARKS, self._stream_prefix() + struct.pack("<I", from_serial), locked=False).payload)
        more, count = rd.take("BB")
        marks = [Mark(*rd.take("IIBIB")) for _ in range(count)]
        rd.tail()
        return marks, bool(more)

    def marks(self, from_serial: int = 0) -> list[Mark]:
        """Every mark from `from_serial` on, following `more` (no mark lost or repeated when several share a position)."""
        out: list[Mark] = []
        while True:
            page, more = self.marks_page(from_serial)
            out += page
            if not more or not page:
                return out
            from_serial = (page[-1].serial + 1) & 0xFFFFFFFF

    def clear(self) -> None:
        self._call(self.CLEAR, self._stream_prefix())

    def mark(self, value: int) -> None:
        """A host mark (kind host, detail = value)."""
        self._call(self.MARK, self._stream_prefix() + bytes([value]))

    def write(self, data: bytes) -> int:
        """-> bytes accepted (the probe does not buffer; fewer than asked is completed partial, not an error)."""
        r = self._request(self.WRITE, self._stream_prefix() + struct.pack("<H", len(data)) + data)
        if r.resolution != m.COMPLETED or r.detail not in (m.SUCCESS, m.PARTIAL):
            raise h.Failed(r)
        rd = m.Reader(r.payload)
        accepted = rd.u16()
        rd.tail()
        return accepted


class Console(PositionStream):
    """oep.target.console: streams on a debug connection, one per (connection, mechanism); reads and marks need no
    lock. A stream whose connection is lost is closed with a link-lost mark and stays readable until the next open."""
    NAME = "oep.target.console"
    REVISION = 1
    OPEN, CLOSE = _CON.op["open"], _CON.op["close"]
    SDI, DMDATA, DMSEQ = (_CON.enum["mechanism"][k] for k in ("sdi", "dmdata", "dmseq"))

    def __init__(self, hst: h.Host, name: str = "oep.target.console"):
        super().__init__(hst, name)
        self.stream = 1
        self.existing = False

    def _stream_prefix(self) -> bytes:
        return bytes([self.stream])

    def open(self, conn: int, mechanism: int = DMSEQ) -> int:
        """-> the stream. An open stream of the same (connection, mechanism) comes back as it is (self.existing):
        position and marks carry on. An unknown mechanism is rejected unsupported."""
        rd = m.Reader(self._call(self.OPEN, bytes([conn, mechanism])).payload)
        self.stream, flags = rd.take("BB")
        self.existing = bool(flags & 1)
        rd.tail()
        return self.stream

    def close(self) -> None:
        self._call(self.CLOSE, bytes([self.stream]))


class StreamIO:
    """A position stream read from a position onwards, as a plain byte stream (console or fixture UART)."""
    MAX_READ, MAX_WRITE = 1000, 64

    def __init__(self, source: PositionStream, start: int | None = None):
        self.source = source
        self.position = source.read(PositionStream.FROM_NOW, 0, 0).start if start is None else start
        self.lost = 0

    def _limits(self) -> tuple[int, int]:
        """(read, write) chunk sizes that fit the probe's frame: request header 6 + session 4 + stream 1 + count 2,
        result header 5 + start 4 + flags 1."""
        frame = self.source.host.confirmed()["max_frame"]
        return max(1, min(self.MAX_READ, frame - 10)), max(1, min(self.MAX_WRITE, frame - 13))

    def read(self, n: int = 512) -> bytes:
        c = self.source.read_from(self.position, min(n, self._limits()[0]))
        if c.gap:
            self.lost += m.serial_diff(c.start, self.position)
        self.position = (c.start + len(c.data)) & 0xFFFFFFFF
        return c.data

    def write(self, data: bytes) -> None:
        chunk = self._limits()[1]
        while data:
            took = self.source.write(data[:chunk])
            data = data[took:]
            if not took:
                time.sleep(0.005)   # the target has not taken the last chunk yet


class ConsoleIO(StreamIO):
    """A console stream read from a position onwards (default: from now), as a plain byte stream."""

    def __init__(self, console: Console, start: int | None = None):
        super().__init__(console, start)
        self.console = console
