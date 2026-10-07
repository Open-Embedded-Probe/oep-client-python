"""oep.target.console revision 1: the target's console as a position stream on a debug connection (oep-spec
oep-if-console, oep-if-common §1), and ConsoleIO, the same as a plain byte stream.

The position streams of oep.target.console and oep.fixture.uart share their read / marks / clear / mark / write
operations (same numbers and meanings; the UART has no stream byte): `PositionStream` holds them, `prefix` is the
stream byte or nothing. Every answer is a fixed part, a counted list or data, then TLVs the host skips (core §2.3).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import host as h, message as m, registry as reg
from .core import Interface, describe

_CON = reg.TARGET_CONSOLE
_COMMON = reg.COMMON.enum


@dataclass
class Mark:
    serial: int          # per stream, wraps (u32): marks are read on by serial
    position: int
    kind: int
    time_ns: int         # the probe's one clock: ns since its boot (u64, core §2.6a)
    detail: int

    @property
    def name(self) -> str:
        return MARK_NAMES.get(self.kind, f"kind 0x{self.kind:02x}")


MARK_BYTES = 22          # serial u32, position u64, kind u8, time_ns u64, detail u8 (common §1.3)
MARK_NAMES = {v: k.replace("_", "-") for k, v in _COMMON["mark_kind"].items()}
MARK_KIND = dict(_COMMON["mark_kind"])
MARK_DETAIL = {k[len("mark_detail_"):]: dict(v) for k, v in _COMMON.items() if k.startswith("mark_detail_")}
READ_FLAGS = _COMMON["read_flags"]


@dataclass
class Chunk:
    start: int
    more: bool
    gap: bool
    data: bytes
    tail: m.Tail = field(default_factory=m.Tail, compare=False)

    def __iter__(self):                      # (start, more, gap, data), as the first version returned
        return iter((self.start, self.more, self.gap, self.data))


@dataclass(frozen=True)
class StreamInfo:
    """One row of the console's streams answer (oep-if-console §1): the stream, its connection and mechanism, who uses
    it (bit0 a host session, bit1 a slot) and whether it is open (0) or closed but still readable (1)."""
    stream: int
    connection: int
    mechanism: int
    users: int
    state: int

    @property
    def open(self) -> bool:
        return self.state == _CON.enum["stream_state"]["open"]


class PositionStream(Interface):
    """read / marks / clear / mark / write of a position stream (oep-if-common §1)."""
    READ, MARKS, CLEAR, MARK, WRITE = (_CON.op[k] for k in ("read", "marks", "clear", "mark", "write"))
    FROM_POSITION, FROM_OLDEST, FROM_NOW, FROM_MARK = (_COMMON["read_from"][k]
                                                       for k in ("position", "oldest", "now", "last_mark"))

    def _stream_prefix(self) -> bytes:
        return b""

    def read(self, start: int = FROM_OLDEST, arg: int = 0, maximum: int = 1000) -> Chunk:
        """-> Chunk(start position, more, gap, data). start: FROM_* ; arg: a position or a mark kind (0: any).
        Lock-free; reading does not consume. The answer is start(u64) flags(u8) len(u16) data [TLV]."""
        p = self._call(self.READ, self._stream_prefix() + struct.pack("<BQH", start, arg, maximum), locked=False).payload
        rd = m.Reader(p)
        pos, flags = rd.take("QB")
        data = rd.counted("H")
        return Chunk(pos, bool(flags & READ_FLAGS["more"]), bool(flags & READ_FLAGS["gap"]), data, rd.tail())

    def read_from(self, position: int, maximum: int = 1000) -> Chunk:
        return self.read(self.FROM_POSITION, position, maximum)

    def marks_page(self, from_serial: int = 0) -> tuple[list[Mark], bool]:
        """One answer's marks with serial >= from_serial (in serial order). -> (marks, more)."""
        rd = m.Reader(self._call(self.MARKS, self._stream_prefix() + struct.pack("<I", from_serial), locked=False).payload)
        more, count = rd.take("BB")
        marks = [Mark(*rd.take("IQBQB")) for _ in range(count)]   # count x mark, no element length (core §2.3)
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
        """-> bytes accepted: what went into the probe's send queue from data's start, min(count, its free space)
        (common §1.4; delivery is not implied) - a console's queue is the probe's own size (not declared), handed to
        the target 2 (dmseq) or 3 (DMDATA) bytes at a time; SDI takes nothing (console §2, §3). Fewer than asked is
        completed partial, not an error; nothing accepted (the queue full) is completed failed (raised as Failed):
        `StreamIO.write` loops on it."""
        r = self._request(self.WRITE, self._stream_prefix() + struct.pack("<H", len(data)) + data)
        if r.resolution != m.COMPLETED or r.detail not in (m.SUCCESS, m.PARTIAL):
            raise h.Failed(r)
        rd = m.Reader(r.payload)
        accepted = rd.u16()
        rd.tail()
        return accepted


class Console(PositionStream):
    """oep.target.console: streams on a debug connection, one live stream per connection; reads, marks and the streams
    list need no lock. A stream lives while anything uses it (the sessions that opened it, a slot's bind): close and a
    lease lapse take one share; a closed stream (connection lost, every user gone) stays readable until the same
    place is opened again, when it comes back under the same number."""
    NAME = "oep.target.console"
    REVISION = 1
    OPEN, CLOSE, STREAMS = _CON.op["open"], _CON.op["close"], _CON.op["streams"]
    SDI, DMDATA, DMSEQ = (_CON.enum["mechanism"][k] for k in ("sdi", "dmdata", "dmseq"))
    NONE = _CON.enum["mechanism"]["none"]          # a slot's "no console" (never opened)

    def __init__(self, hst: h.Host, name: str = "oep.target.console"):
        super().__init__(hst, name)
        self.stream = 1
        self.existing = False

    def _stream_prefix(self) -> bytes:
        return struct.pack("<H", self.stream)

    TAG_MECHANISMS = _CON.tlv["describe"]["mechanisms"]

    def mechanisms(self) -> list[int]:
        """The mechanisms the probe opens (describe tag 0x40)."""
        return [b for tag, v in describe(self.host, self.fn) if tag & 0x7F == self.TAG_MECHANISMS for b in v]

    def open(self, conn: int, mechanism: int = DMSEQ) -> int:
        """-> the stream. An open stream of the same (connection, mechanism) comes back as it is (self.existing):
        position and marks carry on; so does a closed one of the same place and mechanism, under its old number. A
        mechanism the probe lacks is rejected unsupported; another mechanism on a connection whose stream is live is
        rejected unavailable (cause 6)."""
        rd = m.Reader(self._call(self.OPEN, struct.pack("<HB", conn, mechanism)).payload)
        self.stream, flags = rd.take("HB")
        self.existing = bool(flags & _CON.enum["open_flags"]["existing"])
        rd.tail()
        return self.stream

    def close(self) -> None:
        """Take this session's share of the stream (it closes when nobody uses it any more). Closed already: ok."""
        self._call(self.CLOSE, struct.pack("<H", self.stream))

    def streams(self) -> list[StreamInfo]:
        """The probe's console streams, live and closed-but-readable, in the order they were made (oep-if-console §1,
        lock-free; paged by first(u8) / more like connections): how a host without the lock finds a stream's number."""
        out: list[StreamInfo] = []
        while True:
            rd = m.Reader(self._call(self.STREAMS, bytes([len(out)]), locked=False).payload)
            more, count = rd.take("BB")
            out += [StreamInfo(*rd.take("HHBBB")) for _ in range(count)]
            rd.tail()
            if not more or not count or len(out) > 0xFF:
                return out


class StreamIO:
    """A position stream read from a position onwards, as a plain byte stream (console or fixture UART). `write`
    sends in chunks and goes on from what each answer accepted (a console's send queue empties 2 or 3 bytes a poll);
    `stall_s`: how long it waits with nothing accepted before it gives up (TimeoutError) - an SDI console accepts
    nothing ever (console §3.1)."""
    MAX_READ, MAX_WRITE = 1000, 64
    STALL_S = 2.0

    def __init__(self, source: PositionStream, start: int | None = None):
        self.source = source
        self.position = source.read(PositionStream.FROM_NOW, 0, 0).start if start is None else start
        self.lost = 0

    def _limits(self) -> tuple[int, int]:
        """(read, write) chunk sizes that fit the probe's frame: a write is request header 10 (session_id included) + the
        stream's prefix (the console's stream u16, none on a fixture UART) + count 2; a read's result is header 5 +
        start 8 + flags 1 + len 2."""
        frame = self.source.host.confirmed()["max_frame"]
        write = frame - 12 - len(self.source._stream_prefix())
        return max(1, min(self.MAX_READ, frame - 16)), max(1, min(self._write_cap(), write))

    def _write_cap(self) -> int:
        return self.MAX_WRITE

    def read(self, n: int = 512) -> bytes:
        c = self.source.read_from(self.position, min(n, self._limits()[0]))
        if c.gap:
            self.lost += c.start - self.position   # u64 positions: no wrap
        self.position = c.start + len(c.data)
        return c.data

    def write(self, data: bytes, stall_s: float | None = None) -> None:
        """Every byte of `data`, chunk by chunk, each next one from where the last answer's accepted left off. Nothing
        accepted (the slot still holds earlier bytes, common §1.4): a short pause and again, at most `stall_s`
        (default STALL_S) with nothing accepted - then TimeoutError, saying how many bytes went."""
        chunk = self._limits()[1]
        stall = self.STALL_S if stall_s is None else stall_s
        total, since = len(data), time.monotonic()
        while data:
            try:
                took = self.source.write(data[:chunk])
            except h.Failed as e:
                if e.result is None or not e.result.ran:
                    raise
                took = 0                           # accepted 0: the slot was full
            data = data[took:]
            if took:
                since = time.monotonic()
            elif time.monotonic() - since > stall:
                raise TimeoutError(f"the stream accepted nothing for {stall} s ({total - len(data)} of {total} bytes "
                                   "went): the target is not taking input (an SDI console never does)")
            else:
                time.sleep(0.005)   # the target has not taken the last chunk yet


class ConsoleIO(StreamIO):
    """A console stream read from a position onwards (default: from now), as a plain byte stream. A write chunk is
    at most what one frame carries; what the probe's send queue takes is its `accepted` (host guide §14: the rest goes
    after a short wait)."""

    def __init__(self, console: Console, start: int | None = None):
        super().__init__(console, start)
        self.console = console

    def _write_cap(self) -> int:
        return 0xFFFF                              # the frame bounds it (`_limits`); the send queue takes what it can
