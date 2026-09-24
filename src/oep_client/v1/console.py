"""oep.target.console: the target's console as a position stream on the debug connection (oep-spec
docs/console-stream.ja.md), and ConsoleIO, the same as a plain byte stream."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import host as h
from .core import Interface


@dataclass
class Mark:
    position: int
    kind: int
    time_ms: int
    detail: int


MARK_NAMES = {1: "reset", 2: "restart", 3: "attach", 4: "detach", 5: "lost", 6: "clear", 7: "host", 8: "link-lost"}


class Console(Interface):
    """oep.target.console: a stream on the debug connection; reads and marks need no lock."""
    NAME = "oep.target.console"
    OPEN, READ, MARKS, CLEAR, MARK, WRITE, CLOSE = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
    SDI, DMDATA, DMSEQ = 0, 1, 2
    FROM_POSITION, FROM_OLDEST, FROM_NOW, FROM_MARK = 0, 1, 2, 3

    def __init__(self, hst: h.Host, name: str = "oep.target.console"):
        super().__init__(hst, name)
        self.stream = 1

    def open(self, conn: int, mechanism: int = DMSEQ) -> int:
        self.stream = self._call(self.OPEN, bytes([conn, mechanism])).payload[0]
        return self.stream

    def read(self, start: int = FROM_OLDEST, arg: int = 0, maximum: int = 1000) -> tuple[int, bool, bool, bytes]:
        """-> (start position, more, gap, data). start: FROM_* ; arg: a position or a mark kind."""
        p = self._call(self.READ, struct.pack("<BBIH", self.stream, start, arg, maximum), locked=False).payload
        pos, flags = struct.unpack_from("<IB", p)
        return pos, bool(flags & 1), bool(flags & 2), p[5:]

    def read_from(self, position: int, maximum: int = 1000) -> tuple[int, bool, bool, bytes]:
        return self.read(self.FROM_POSITION, position, maximum)

    def marks(self, since: int = 0) -> list[Mark]:
        p = self._call(self.MARKS, struct.pack("<BI", self.stream, since), locked=False).payload
        return [Mark(*struct.unpack_from("<IBIB", p, 1 + 10 * i)) for i in range(p[0])]

    def clear(self) -> None:
        self._call(self.CLEAR, bytes([self.stream]))

    def mark(self, value: int) -> None:
        self._call(self.MARK, bytes([self.stream, value]))

    def write(self, data: bytes) -> int:
        return struct.unpack_from("<H", self._call(self.WRITE, bytes([self.stream]) + data).payload)[0]

    def close(self) -> None:
        self._call(self.CLOSE, bytes([self.stream]))


class ConsoleIO:
    """A console stream read from a position onwards, as a plain byte stream."""

    def __init__(self, console: Console, start: int | None = None):
        self.console = console
        self.position = console.read(Console.FROM_NOW)[0] if start is None else start
        self.lost = 0

    def read(self, n: int = 512) -> bytes:
        start, more, gap, data = self.console.read_from(self.position, min(n, 1000))
        if gap:
            self.lost += start - self.position
        self.position = start + len(data)
        return data

    def write(self, data: bytes) -> None:
        while data:
            took = self.console.write(data[:64])
            data = data[took:]
            if not took:
                time.sleep(0.005)   # the target has not taken the last chunk yet
