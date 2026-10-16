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
# the common kinds (lost 1, clear 2, host 3) and the console's own (reset 0x40, restart 0x41; console §2)
MARK_KIND = {**_COMMON["mark_kind"], **_CON.enum["mark_kind"]}
MARK_NAMES = {v: k.replace("_", "-") for k, v in MARK_KIND.items()}
MARK_DETAIL = {k[len("mark_detail_"):]: dict(v) for k, v in _COMMON.items() if k.startswith("mark_detail_")}
for _k, _v in _CON.enum.items():                     # the console's details: reset, restart, lost's target_timeout
    if _k.startswith("mark_detail_"):
        MARK_DETAIL.setdefault(_k[len("mark_detail_"):], {}).update(_v)
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
    """One row of the console's streams answer (oep-if-console §1): a live stream, its connection and mechanism."""
    stream: int
    connection: int
    mechanism: str | int           # its name in the probe's list (core §7.7); a number the list lacks as it came


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
        """One answer's marks from `from_serial` on, in serial order (common §1.3: from_serial included; from the
        serial the next mark gets, none and more 0; a serial no longer kept starts at the oldest kept - the serials then
        jump by the marks lost). -> (marks, more)."""
        rd = m.Reader(self._call(self.MARKS, self._stream_prefix() + struct.pack("<I", from_serial), locked=False).payload)
        more, count = rd.take("BB")
        marks = [Mark(*rd.take("IQBQB")) for _ in range(count)]   # count x mark, no element length (core §2.3)
        rd.tail()
        return marks, bool(more)

    def marks(self, from_serial: int = 0) -> list[Mark]:
        """Every mark from `from_serial` on, following `more`: each next page from the last serial + 1 (mod 2^32), until
        more 0 (no mark lost or repeated when several share a position)."""
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
        the target 2 (dmseq) or 3 (DMDATA) bytes at a time; an SDI stream's write is unsupported (console §1, §3).
        Fewer than asked is completed partial, not an error; nothing accepted (the queue full) is completed failed
        (raised as Failed): `StreamIO.write` loops on it."""
        r = self._request(self.WRITE, self._stream_prefix() + struct.pack("<H", len(data)) + data)
        if r.resolution != m.COMPLETED or r.detail not in (m.SUCCESS, m.PARTIAL):
            raise h.Failed(r)
        rd = m.Reader(r.payload)
        accepted = rd.u16()
        rd.tail()
        return accepted


class Console(PositionStream):
    """oep.target.console: a session's stream on a debug connection, one live stream per connection; reads, marks and
    the streams list need no lock. close, the connection's release and the session's end release the stream, its
    buffer and its send queue: read what is wanted first (console §2). A closed stream's number answers no_resource
    and is never used again. marks, clear, mark and write are optional (`offers`): without marks, read from a mark is
    unsupported; an SDI stream takes no write (unsupported)."""
    NAME = "oep.target.console"
    REVISION = 1
    OPEN, CLOSE, STREAMS = _CON.op["open"], _CON.op["close"], _CON.op["streams"]
    SDI, DMDATA, DMSEQ = "sdi", "dmdata", "dmseq"            # the standard mechanisms' names (console §3)

    def __init__(self, hst: h.Host, name: str = "oep.target.console"):
        super().__init__(hst, name)
        self.stream = 1

    def _stream_prefix(self) -> bytes:
        return struct.pack("<I", self.stream)

    TAG_MECHANISM = _CON.tlv["describe"]["mechanism"]

    def mechanisms(self) -> list[str]:
        """The mechanisms the probe opens, by name, in its order: a named list (describe tag 0x40 repeated, console §1,
        core §7.7); the number of the k-th is k (from 1)."""
        return [v.decode("utf-8", "replace") for tag, v in describe(self.host, self.fn) if tag == self.TAG_MECHANISM]

    def mechanism_number(self, mechanism: str | int) -> int:
        """A mechanism's number on the wire: its place in the probe's list (from 1); a name the probe does not list: 0
        (the probe refuses it unsupported)."""
        if isinstance(mechanism, int):
            return mechanism
        names = self.mechanisms()
        return names.index(mechanism) + 1 if mechanism in names else 0

    def open(self, conn: int, mechanism: str | int = DMSEQ) -> int:
        """-> a new stream (u32) on the connection (console §2). mechanism: a name (looked up in describe) or the
        number. A mechanism the probe lacks is rejected unsupported; a connection with a live stream (any mechanism) is
        rejected unavailable (cause 6)."""
        rd = m.Reader(self._call(self.OPEN, struct.pack("<IB", conn, self.mechanism_number(mechanism))).payload)
        self.stream = rd.u32()
        rd.tail()
        return self.stream

    def close(self) -> None:
        """Release the stream, its buffer and its send queue (console §2)."""
        self._call(self.CLOSE, struct.pack("<I", self.stream))

    def streams(self) -> list[StreamInfo]:
        """The probe's live console streams, in the order they were made (oep-if-console §1, lock-free; paged by
        first(u16) / more, each page from the number already read, until more 0): how a host without the lock finds a
        stream's number."""
        out: list[StreamInfo] = []
        names = self.mechanisms()                                  # the mechanism by name (core §7.7)
        name = lambda n: names[n - 1] if 1 <= n <= len(names) else n
        while True:
            rd = m.Reader(self._call(self.STREAMS, struct.pack("<H", len(out)), locked=False).payload)
            more, count = rd.take("BB")
            out += [StreamInfo(stream, conn, name(mech)) for stream, conn, mech in (rd.take("IIB") for _ in range(count))]
            rd.tail()
            if not more or not count or len(out) > 0xFFFF:
                return out


class StreamIO:
    """A position stream read from a position onwards, as a plain byte stream (console or fixture UART). `write`
    sends in chunks and goes on from what each answer accepted (a console's send queue empties 2 or 3 bytes a poll);
    `stall_s`: how long it waits with nothing accepted before it gives up (TimeoutError); an SDI console's write is
    refused unsupported (console §1)."""
    MAX_READ, MAX_WRITE = 1000, 64
    STALL_S = 2.0

    def __init__(self, source: PositionStream, start: int | None = None):
        self.source = source
        self.position = source.read(PositionStream.FROM_NOW, 0, 0).start if start is None else start
        self.lost = 0

    def _limits(self) -> tuple[int, int]:
        """(read, write) chunk sizes that fit the probe's frame: a write is request header 10 (session_id included) + the
        stream's prefix (the console's stream u32, none on a fixture UART) + count 2; a read's result is header 5 +
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
                                   "went): the target is not taking input")
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
