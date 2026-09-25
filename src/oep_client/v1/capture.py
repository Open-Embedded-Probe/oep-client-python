"""oep.fixture.capture / oep.fixture.analog, the basic set of oep-spec docs/logic-capture.ja.md (§3.0 layouts, §4
segments, §5 operations). Draft: tag and op numbers follow the proposal and may still move."""

from __future__ import annotations

import struct
import time
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction

from . import host as h
from .core import Interface, confirm

# configure TLVs; bit 7 of a tag = critical (the probe must reject what it cannot do)
MODE, RATE, SAMPLES, SEGMENTS, TRIGGER, PRETRIGGER, FRONTEND = 0x40, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47
ACTUAL_RATE, LAYOUT, ACTUAL_SAMPLES, ACTUAL_SEGMENTS, TIMING, SCALE, BLOCKING, IGNORED = range(0x50, 0x58)
CRITICAL = 0x80
ONE_SHOT, REPEAT, STREAMING = 1, 2, 3
IMMEDIATE, LEVEL, EDGE, CROSS_UP, CROSS_DOWN = range(5)
# events (v1 wire §4.5, role 0x05)
EVENT_SEGMENT, EVENT_STOPPED, EVENT_TRIGGERED = 0x01, 0x02, 0x03


def tlvs(payload: bytes) -> list[tuple[int, bytes]]:
    out, at = [], 0
    while at + 2 <= len(payload):
        tag, n = payload[at], payload[at + 1]
        out.append((tag, payload[at + 2:at + 2 + n]))
        at += 2 + n
    return out


@dataclass
class Segment:
    serial: int
    position: int
    samples: int
    start_us: int
    trigger_index: int | None
    flags: int

    @classmethod
    def unpack(cls, b: bytes) -> "Segment":
        serial, position, samples, start_us, trig, flags = struct.unpack_from("<IIIIIB", b)
        return cls(serial, position, samples, start_us, None if trig == 0xFFFFFFFF else trig, flags)


@dataclass
class Config:
    """What configure answered: the probe's actual values (logic-capture §5.3)."""
    rate: Fraction = Fraction(0)
    width: int = 0                               # logic: bits per sample (w)
    positions: list[int] = field(default_factory=list)   # logic: bit of channel k within a sample
    slot: int = 0                                # analog: s, o, b, order
    offset: int = 0
    bits: int = 0
    order: list[int] = field(default_factory=list)
    samples: int = 0
    segments: int = 0
    jitter_kind: int = 0
    jitter_ns: int = 0
    skew_ns: list[int] = field(default_factory=list)
    zero: int = 0
    scale_nv: int = 0
    blocking_ms: int = 0
    ignored: list[int] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        """Length of one segment in the stream (§3.0 rule 4)."""
        if self.width:
            return (self.samples * self.width + 7) // 8
        return self.samples * len(self.order) * self.slot // 8


def _config(payload: bytes, analog: bool) -> Config:
    c = Config()
    for tag, v in tlvs(payload):
        if tag == ACTUAL_RATE:
            num, den = struct.unpack("<II", v)
            c.rate = Fraction(num, den)
        elif tag == LAYOUT and not analog:
            c.width, n = v[0], v[1]
            c.positions = list(v[2:2 + n])
        elif tag == LAYOUT and analog:
            c.slot, c.offset, c.bits, n = v[0], v[1], v[2], v[3]
            c.order = list(v[4:4 + n])
        elif tag == ACTUAL_SAMPLES:
            c.samples = struct.unpack("<I", v)[0]
        elif tag == ACTUAL_SEGMENTS:
            c.segments = struct.unpack("<I", v)[0]
        elif tag == TIMING:
            c.jitter_kind, c.jitter_ns = v[0], struct.unpack_from("<I", v, 1)[0]
            c.skew_ns = [struct.unpack_from("<I", v, 5 + 4 * i)[0] for i in range((len(v) - 5) // 4)]
        elif tag == SCALE:
            c.zero, c.scale_nv = struct.unpack("<II", v)
        elif tag == BLOCKING:
            c.blocking_ms = struct.unpack("<I", v)[0]
        elif tag == IGNORED:
            c.ignored = list(v)
    return c


class LogicCapture(Interface):
    """Basic logic capture. Channels are the plan's roles 0..C-1."""
    NAME = "oep.fixture.capture"
    ANALOG = False
    CONFIGURE, START, STOP, FORCE, STATUS, READ, SEGMENTS, RELEASE, QUERY_OP = range(1, 10)

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)
        self.config: Config | None = None

    def configure(self, *, rate: int, mode: int = ONE_SHOT, samples: int | None = None, segments: int | None = None,
                  trigger: tuple[int, int, int] | None = None, pretrigger: int | None = None, query: bool = False,
                  critical: set[int] = frozenset()) -> Config:
        """-> the probe's actual values. `critical`: tags the probe must honour or reject."""
        def tlv(tag: int, value: bytes) -> bytes:
            return bytes([tag | (CRITICAL if tag in critical else 0), len(value)]) + value
        body = tlv(MODE, bytes([mode])) + tlv(RATE, struct.pack("<I", rate))
        if samples is not None:
            body += tlv(SAMPLES, struct.pack("<I", samples))
        if segments is not None:
            body += tlv(SEGMENTS, struct.pack("<I", segments))
        if trigger is not None:
            body += tlv(TRIGGER, struct.pack("<BBH", *trigger))
        if pretrigger is not None:
            body += tlv(PRETRIGGER, struct.pack("<I", pretrigger))
        # query is its own operation: the lock is decided per operation, before the payload is looked at
        op = self.QUERY_OP if query else self.CONFIGURE
        c = _config(self._call(op, body, locked=not query).payload, self.ANALOG)
        if not query:
            self.config = c
        return c

    def start(self) -> int:
        """-> blocking_ms (0: the probe keeps answering while it captures)."""
        return struct.unpack("<I", self._call(self.START).payload)[0]

    def stop(self) -> None:
        self._call(self.STOP)

    def status(self) -> tuple[int, int, int, int]:
        """-> state, segments done, write position, flags."""
        return struct.unpack("<BIIB", self._call(self.STATUS, locked=False).payload)

    def release(self, serial: int) -> None:
        """Repeat: segments up to `serial` may be reused."""
        self._call(self.RELEASE, struct.pack("<I", serial))

    def segments(self, from_serial: int = 0) -> list[Segment]:
        p = self._call(self.SEGMENTS, struct.pack("<I", from_serial), locked=False).payload
        return [Segment.unpack(p[1 + 21 * i:]) for i in range(p[0])]

    def wait(self, timeout: float = 5.0) -> list[Segment]:
        """Poll status until the one-shot is done (or failed). -> its segments."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.status()[0]
            if state == 4:
                return self.segments()
            if state == 6:
                raise h.Failed(None)
            time.sleep(0.002)
        raise TimeoutError("capture did not finish")

    def read(self, position: int, length: int) -> bytes:
        """Bytes [position, position+length) of the stream, pipelined in frame-sized reads."""
        chunk = max(1, confirm(self.host)["max_frame"] - 16)
        reqs = [self.request(self.READ, struct.pack("<II", position + off, min(chunk, length - off)))
                for off in range(0, length, chunk)]
        out = bytearray()
        for off, r in zip(range(0, length, chunk), self.host.pipeline_calls(reqs, locked=False)):
            got_pos, flags = struct.unpack_from("<IB", r.payload)
            data = r.payload[5:]
            want = min(chunk, length - off)
            while len(data) < want:                       # a short answer: read on from where it stopped
                more = self._call(self.READ, struct.pack("<II", position + off + len(data), want - len(data)),
                                  locked=False).payload[5:]
                if not more:
                    raise h.ProtocolError(f"read at {position + off + len(data)} returned nothing")
                data += more
            out += data
        return bytes(out)

    def read_segment(self, segment: Segment) -> bytes:
        c = self.config
        return self.read(segment.position, (segment.samples * c.width + 7) // 8)

    # ---- the §3.0 layout ---------------------------------------------------------------------------------
    def channel(self, data: bytes, k: int, samples: int | None = None) -> list[int]:
        """Channel k's values, one per sample (§3.0 rules 1-3)."""
        c = self.config
        n = samples if samples is not None else len(data) * 8 // c.width
        bit0 = c.positions[k]
        return [(data[(i * c.width + bit0) >> 3] >> ((i * c.width + bit0) & 7)) & 1 for i in range(n)]

    def to_sr(self, path: str, data: bytes, samples: int, names: list[str] | None = None) -> None:
        """A sigrok session file: one byte per sample, bit k = channel k (so up to 8 channels here)."""
        c = self.config
        n_ch = len(c.positions)
        if n_ch > 8:
            raise ValueError("to_sr writes one byte per sample; more than 8 channels need unitsize 2")
        names = names or [f"D{k}" for k in range(n_ch)]
        chans = [self.channel(data, k, samples) for k in range(n_ch)]
        out = bytes(sum(chans[k][i] << k for k in range(n_ch)) for i in range(samples))
        rate = c.rate.numerator // c.rate.denominator
        meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1",
                f"total probes={n_ch}", f"samplerate={rate} Hz", "total analog=0"]
        meta += [f"probe{k + 1}={nm}" for k, nm in enumerate(names)] + ["unitsize=1", ""]
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("version", "2")
            z.writestr("metadata", "\n".join(meta))
            z.writestr("logic-1-1", out)
