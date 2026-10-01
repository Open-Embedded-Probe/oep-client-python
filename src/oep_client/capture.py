"""oep.fixture.logic / oep.fixture.analog / oep.fixture.capture-group revision 1 (oep-spec docs/oep-if-capture.ja.md:
§1 layouts, §2 segments, §3 operations, §3.8 calibration, §4 groups). Numbers from `registry`.

Times are the probe's one clock (ns since its boot, comparable within one boot_id): estimates with an uncertainty, the
probe's known corrections applied. Analog values are always raw; the probe's 1st-order scale, its calibration data and
its reference are for the host to choose from.

Every start begins a new generation (u32, from 1): segment serials and positions count from 0 inside it, and read and
release name it, so a read sent for the last capture never returns the next one's bytes (oep-if-capture §3.2). The
client keeps it (`LogicCapture.generation`, from start / status / the group's start) and passes it on; `read_segment`
takes the segment's own.

configure: a TLV the probe cannot honour is refused (rejected unsupported, 0x0B, payload = the tag) when the host marked
it critical, and otherwise ignored and listed in the answer's ignored TLV (0x7F, oep-core §2.3)."""

from __future__ import annotations

import struct
import time
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction

from . import cobs, host as h, message as m, registry as reg
from .frames import FramingLost
from .core import Interface, confirm

_CAP = reg.FIXTURE_LOGIC
_ANA = reg.FIXTURE_ANALOG
_GRP = reg.FIXTURE_CAPTURE_GROUP
# configure TLVs; bit 7 of a tag = critical (the probe must reject what it cannot do)
MODE, RATE, SAMPLES, SEGMENTS, TRIGGER, PRETRIGGER = (
    _CAP.tlv["configure"][k] for k in ("mode", "rate", "samples", "segments", "trigger", "pretrigger"))
FRONTEND = _ANA.tlv["configure"]["frontend"]             # analog only
ACTUAL_RATE, LAYOUT, ACTUAL_SAMPLES, ACTUAL_SEGMENTS, TIMING, SCALE, BLOCKING, SKEW, FRONTEND_USED, REFERENCE, \
    RATE_ACCURACY = (_ANA.tlv["configure_answer"][k] for k in (
        "actual_rate", "layout", "actual_samples", "actual_segments", "timing", "scale", "blocking_ms", "skew",
        "frontend_used", "reference", "rate_accuracy"))
FACTORY, VREFINT = _ANA.tlv["calibration_answer"]["factory"], _ANA.tlv["calibration_answer"]["vrefint"]
STATUS_ERROR = _CAP.tlv["status_answer"]["error"]          # status's TLV: why the state is 6
DATA_GENERATION = _CAP.tlv["data"]["generation"]           # a data frame's TLV: its generation (always in streaming)
GROUP_GENERATIONS = _GRP.tlv["start_answer"]["generations"]
REFERENCE_SOURCE = {v: k for k, v in _ANA.enum["reference_source"].items()}
IGNORED = m.TAG_IGNORED
CRITICAL = m.TAG_CRITICAL
ONE_SHOT, REPEAT, STREAMING = (_CAP.enum["mode"][k] for k in ("one_shot", "repeat", "streaming"))
IMMEDIATE, LEVEL, EDGE, CROSS_UP, CROSS_DOWN = range(5)
STATE = _CAP.enum["state"]
SEGMENT_SLIPPED = _CAP.enum["segment_flag"]["slipped"]
STATUS_FLAGS = _CAP.enum["status_flag"]                    # dropped, slipped (reset at start)
ERRORS = {v: k for k, v in _CAP.enum["error"].items()}     # state 6's reason
# events (oep-core §11, role 0x05)
EVENT_SEGMENT, EVENT_STOPPED, EVENT_TRIGGERED = (_CAP.event[k] for k in ("segment", "stopped", "triggered"))


def tlvs(payload: bytes) -> list[tuple[int, bytes]]:
    return m.split_tlvs(payload)


SEGMENT_BYTES = 37   # serial u32, position u64, samples u32, start_ns u64, start_uncertainty_ns u32, trigger_index u32,
                     # flags u8, generation u32 (oep-if-capture §2)


@dataclass
class Segment:
    serial: int
    position: int
    samples: int
    start_ns: int                    # the first sample's time on the probe's clock (an estimate)
    start_uncertainty_ns: int        # +- of start_ns (a guide, not a promise)
    trigger_index: int | None
    flags: int
    generation: int = 0              # the start this segment belongs to (read / release name it)

    @property
    def slipped(self) -> bool:
        """flags bit2: the time base bent inside the segment (samples later than the timing answer, oep-if-capture §2)."""
        return bool(self.flags & SEGMENT_SLIPPED)

    @classmethod
    def unpack(cls, b: bytes) -> "Segment":
        serial, position, samples, start_ns, uncertainty, trig, flags, generation = struct.unpack_from("<IQIQIIBI", b)
        return cls(serial, position, samples, start_ns, uncertainty, None if trig == 0xFFFFFFFF else trig, flags,
                   generation)


@dataclass
class Status:
    """status's answer (oep-if-capture §3.2). Iterates as (state, serial_done, write_pos, flags), the first shape."""
    state: int
    serial_done: int                 # segments finished
    write_pos: int                   # bytes taken so far (dropped ones counted: the next byte's position)
    flags: int                       # bit0 dropped, bit1 slipped - since start
    generation: int
    error: int | None = None         # state 6: why (ERRORS)

    def __iter__(self):
        return iter((self.state, self.serial_done, self.write_pos, self.flags))

    def __getitem__(self, i: int):
        return (self.state, self.serial_done, self.write_pos, self.flags)[i]

    @property
    def dropped(self) -> bool:
        return bool(self.flags & STATUS_FLAGS["dropped"])

    @property
    def error_name(self) -> str | None:
        return None if self.error is None else ERRORS.get(self.error, f"error 0x{self.error:02x}")


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
    rate_measured: bool = False                  # rate_accuracy: the rate was measured (else computed from a divider)
    rate_ppm: int = 0                            # its uncertainty (0: unknown)
    skew_ns: dict[int, int] = field(default_factory=dict)      # analog, per channel (role)
    zero: dict[int, int] = field(default_factory=dict)         # analog, per channel
    scale_nv: dict[int, int] = field(default_factory=dict)     # analog, nV per value, per channel
    frontend: dict[int, int] = field(default_factory=dict)     # analog: the frontend each channel took
    reference: tuple[str, int, bool] | None = None             # analog: (source, mV, measured)
    blocking_ms: int = 0
    ignored: list[int] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        """Length of one segment in the stream (§3.0 rule 4)."""
        if self.width:
            return (self.samples * self.width + 7) // 8
        return self.samples * len(self.order) * self.slot // 8


@dataclass
class CaptureRecord:
    """One segment as it was read, for whoever records runs (Host.on_capture): the probe's own words - no pin names,
    no target (the recorder's caller knows those). armed_s / read_s: time.monotonic() at start() and at the read."""
    fn: int
    name: str
    config: Config
    segment: Segment
    data: bytes
    armed_s: float | None
    read_s: float


@dataclass
class Received:
    """What stream() collected: the bytes in arrival order, where the stream skipped (probe-side drops) and lost frames."""
    data: bytearray = field(default_factory=bytearray)
    start: int | None = None                                   # stream position of data[0]
    gaps: list[tuple[int, int]] = field(default_factory=list)  # (index in data where it skipped, bytes skipped)
    seq_lost: int = 0                                          # push frames missing by seq
    frames: int = 0
    stale: int = 0                                             # pushes of an earlier generation, dropped


def unpack_push(frame: bytes) -> tuple[int, int, int, bytes, int | None]:
    """A data frame (core §11.2: role fn seq position(u64) len(u16) data [TLV]) -> (fn, seq, position, data,
    generation): the TLV 0x01 generation, None when the frame carries none."""
    fn, seq, position = struct.unpack_from("<HHQ", frame, 1)
    rd = m.Reader(frame[13:])
    data = rd.counted("H")
    g = rd.tail().get(DATA_GENERATION)
    return fn, seq, position, data, struct.unpack("<I", g)[0] if g is not None and len(g) == 4 else None


def take_pushes(link, fn: int) -> list[tuple[int, int, bytes, int | None]]:
    """Remove this fn's data pushes (role 0x06) from the link: [(seq, position, data, generation)], oldest first."""
    mine, rest = [], []
    for f in link.pushes:
        (mine if struct.unpack_from("<H", f, 1)[0] == fn else rest).append(f)
    link.pushes.clear()
    link.pushes.extend(rest)
    return [unpack_push(f)[1:] for f in mine]


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
        elif tag == RATE_ACCURACY:
            c.rate_measured, c.rate_ppm = v[0] == 1, struct.unpack_from("<I", v, 1)[0]
        elif tag == SCALE and analog:
            role, zero, scale = struct.unpack("<Bii", v)            # signed: an inverting frontend (§3.3)
            c.zero[role], c.scale_nv[role] = zero, scale
        elif tag == SKEW and analog:
            role, ns = struct.unpack("<BI", v)
            c.skew_ns[role] = ns
        elif tag == FRONTEND_USED and analog:
            c.frontend[v[0]] = v[1]
        elif tag == REFERENCE and analog:
            source, mv, how = struct.unpack("<BIB", v)
            c.reference = (REFERENCE_SOURCE.get(source, str(source)), mv, how == 1)
        elif tag == BLOCKING:
            c.blocking_ms = struct.unpack("<I", v)[0]
        elif tag == IGNORED:
            c.ignored = list(v)
    return c


class LogicCapture(Interface):
    """Basic logic capture. Channels are the plan's roles 0..C-1."""
    NAME = "oep.fixture.logic"
    REVISION = 1
    ANALOG = False
    CONFIGURE, START, STOP, FORCE, STATUS, READ, SEGMENTS, RELEASE, QUERY_OP = (
        _CAP.op[k] for k in ("configure", "start", "stop", "force", "status", "read", "segments", "release", "query"))

    generation: int | None = None         # the current capture's generation (start / status / the group's start)

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)
        self.config: Config | None = None
        self.armed_s: float | None = None     # time.monotonic() at the last start()
        self.generation = None

    def configure(self, *, rate: int, mode: int = ONE_SHOT, samples: int | None = None, segments: int | None = None,
                  trigger: tuple[int, int, int] | None = None, pretrigger: int | None = None, query: bool = False,
                  critical: set[int] = frozenset(), frontends: dict[int, int] | None = None) -> Config:
        """-> the probe's actual values (Config.ignored: tags the probe ignored). `critical`: tags the probe must honour
        or reject (host.Unsupported, .tag = the one it cannot)."""
        def tlv(tag: int, value: bytes) -> bytes:
            return bytes([tag | (CRITICAL if tag in critical else 0), len(value)]) + value
        body = tlv(MODE, bytes([mode])) + tlv(RATE, struct.pack("<I", rate))
        if samples is not None:
            body += tlv(SAMPLES, struct.pack("<I", samples))
        if segments is not None:
            body += tlv(SEGMENTS, struct.pack("<I", segments))
        if trigger is not None:
            body += tlv(TRIGGER, struct.pack("<BBI", *trigger))        # type, role, value (u32)
        if pretrigger is not None:
            body += tlv(PRETRIGGER, struct.pack("<I", pretrigger))
        for role, fe in sorted((frontends or {}).items()):   # analog: the input range per channel (describe frontend)
            body += tlv(FRONTEND, bytes([role, fe]))
        # query is its own operation: the lock is decided per operation, before the payload is looked at
        op = self.QUERY_OP if query else self.CONFIGURE
        c = _config(self._call(op, body, locked=not query).payload, self.ANALOG)
        if not query:
            self.config = c
        return c

    def subscribe(self, min_bytes: int = 0, max_delay_ms: int = 0) -> None:
        """Events, and in streaming the data pushes (oep-core §11): send when min_bytes are ready or max_delay_ms after the
        first byte (0, 0: as soon as there is anything)."""
        self.host.subscribe(self.fn, min_bytes, max_delay_ms)

    def unsubscribe(self) -> None:
        self.host.unsubscribe(self.fn)

    def stream(self, link, *, seconds: float | None = None, nbytes: int | None = None,
               into: Received | None = None, keepalive_s: float = 1.0) -> Received:
        """Streaming: collect data pushes until `nbytes` have arrived or `seconds` have passed (at least one is needed).
        A position that does not follow the previous push is a probe-side drop (a gap); a seq that skips is a lost frame.
        A push of another generation than this capture's (a leftover of the start before, oep-if-capture §3.4) is
        dropped. The subscription ends with the lock, so the lock is kept alive every `keepalive_s` while collecting (a
        stream longer than the lease otherwise stopped: 35 MB missing at the end of 10 s at 150 MHz with a 10 s lease)."""
        if seconds is None and nbytes is None:
            raise ValueError("stream() needs seconds or nbytes")
        got = into or Received()
        deadline = time.monotonic() + seconds if seconds is not None else None
        kept = time.monotonic()
        expect_seq = getattr(got, "_seq", None)
        event_seqs = getattr(got, "_events", set())   # events share the fn's seq (they stay on the link for the caller)
        while True:
            for e in link.events:
                if struct.unpack_from("<H", e, 1)[0] == self.fn:
                    event_seqs.add(struct.unpack_from("<H", e, 3)[0])
            for seq, position, data, generation in take_pushes(link, self.fn):
                while expect_seq is not None and expect_seq != seq:
                    if expect_seq in event_seqs:
                        event_seqs.discard(expect_seq)
                    else:
                        got.seq_lost += 1
                    expect_seq = (expect_seq + 1) & 0xFFFF
                expect_seq = (seq + 1) & 0xFFFF
                if generation is not None and self.generation is not None and generation != self.generation:
                    got.stale += 1                              # the generation before: not this capture's bytes
                    continue
                if got.start is None:
                    got.start = position
                else:
                    expected = got.start + len(got.data) + sum(n for _, n in got.gaps)   # u64 positions: no wrap
                    skipped = position - expected
                    if skipped:
                        got.gaps.append((len(got.data), skipped))
                got.data += data
                got.frames += 1
            got._seq, got._events = expect_seq, event_seqs
            if keepalive_s and time.monotonic() - kept >= keepalive_s:
                self.host.keepalive()
                kept = time.monotonic()
            if nbytes is not None and len(got.data) >= nbytes:
                return got
            if deadline is not None and time.monotonic() >= deadline:
                return got
            link.pump(0.02, until_one=True)

    def finish(self, link, got: Received, timeout: float = 5.0) -> Received:
        """Streaming, after stop(): collect the pushes still to come, up to the last byte captured (status's write
        position), or until `timeout`."""
        end = self.status().write_pos
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if got.start is not None:
                reached = got.start + len(got.data) + sum(n for _, n in got.gaps)
                if reached == end:
                    return got
            self.stream(link, seconds=min(0.1, max(0.0, deadline - time.monotonic())), into=got)
        return got

    def start(self) -> int:
        """-> blocking_ms (0: the probe keeps answering while it captures). self.generation: the new capture's."""
        rd = m.Reader(self._call(self.START, expect_ms=self.config.blocking_ms if self.config else 0).payload)
        blocking, self.generation = rd.take("II")
        rd.tail()
        self.armed_s = time.monotonic()
        return blocking

    def stop(self) -> None:
        self._call(self.STOP)

    def force(self) -> None:
        """Waiting for the trigger: start now (the segment's trigger_index marks where)."""
        self._call(self.FORCE)

    def status(self) -> Status:
        """-> Status (state, segments done, write position, flags, generation, error). Lock-free; it also brings the
        generation a host that did not start the capture needs for read."""
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        state, done, pos, flags, generation = rd.take("BIQBI")
        error = rd.tail().get(STATUS_ERROR)
        self.generation = generation
        return Status(state, done, pos, flags, generation, error[0] if error else None)

    def release(self, serial: int, generation: int | None = None) -> None:
        """Repeat: segments up to and including `serial` may be reused (of this generation; another one is rejected
        unavailable). In state 5 (no free segment) the probe goes on by itself once there is room."""
        self._call(self.RELEASE, struct.pack("<II", self._generation(generation), serial))

    def _generation(self, generation: int | None) -> int:
        if generation is not None:
            return generation
        if self.generation is None:
            self.status()                                   # a host that did not start it: ask
        return self.generation

    def segments_page(self, from_serial: int = 0) -> tuple[list[Segment], bool]:
        """One answer's segment records from `from_serial` on. -> (segments, more)."""
        rd = m.Reader(self._call(self.SEGMENTS, struct.pack("<I", from_serial), locked=False).payload)
        more, count = rd.take("BB")
        out = [Segment.unpack(rd.element().bytes(SEGMENT_BYTES)) for _ in range(count)]
        rd.tail()
        return out, bool(more)

    def segments(self, from_serial: int = 0) -> list[Segment]:
        """Every segment record from `from_serial` on, following `more`."""
        out: list[Segment] = []
        while True:
            page, more = self.segments_page(from_serial)
            out += page
            if not more or not page:
                return out
            from_serial = page[-1].serial + 1

    def wait(self, timeout: float = 5.0, keepalive_s: float = 1.0) -> list[Segment]:
        """Poll status until the one-shot is done (or failed). -> its segments. Waiting for a trigger may take longer
        than the lease: the lock is kept alive every `keepalive_s` (status needs no lock, so it does not)."""
        deadline = time.monotonic() + timeout
        kept = time.monotonic()
        while time.monotonic() < deadline:
            if keepalive_s and self.host.session is not None and time.monotonic() - kept >= keepalive_s:
                self.host.keepalive()
                kept = time.monotonic()
            st = self.status()
            state = st.state
            if state == STATE["done"]:
                return self.segments()
            if state == STATE["error"]:
                raise h.Failed(None, f"the capture stopped with an error ({st.error_name or 'unknown'})")
            if state not in STATE.values():
                raise h.ProtocolError(f"capture state {state} is not one this client knows")
            time.sleep(0.002)
        raise TimeoutError("capture did not finish")

    READ_TRIES = 4   # batches of reads sent again after the link's own repeat failed too
    BATCH = 16   # frame-sized reads per pipeline; a keepalive between batches when a session is open

    READ_HEAD = 13   # the answer's position(u64) flags(u8) len(u32) in front of the data

    @staticmethod
    def _read_data(payload: bytes) -> bytes:
        rd = m.Reader(payload)
        rd.take("QB")
        return rd.counted("I")                             # position flags len(u32) data [TLV]

    def read(self, position: int, length: int, generation: int | None = None) -> bytes:
        """Bytes [position, position+length) of the stream, pipelined in frame-sized reads. `generation`: the capture
        they belong to (default: the one this client saw at start / status); the probe refuses another one as
        unavailable (cause 6), so an old read never gets the next capture's bytes.

        The reads need no lock and go without the session id, so a repeat after a broken reply is simply run again
        (reads are not deduplicated, oep-core §5.2). They do not extend the lease, though: a long read (64 KB over a
        115200 bps UART probe takes seconds) sends a keepalive between batches, or the lease lapsed mid-read, the plan
        went with it (core §9) and the capture read back nothing (2026-09-26, V003 jig)."""
        g = self._generation(generation)
        chunk = max(1, confirm(self.host)["max_frame"] - m.RESULT_HEADER - self.READ_HEAD)
        offsets = list(range(0, length, chunk))
        out = bytearray()
        for at in range(0, len(offsets), self.BATCH):
            if at and self.host.session is not None:
                self.host.keepalive()
            batch = offsets[at:at + self.BATCH]
            for attempt in range(self.READ_TRIES):
                reqs = [self.request(self.READ, struct.pack("<IQI", g, position + off, min(chunk, length - off)))
                        for off in batch]
                try:
                    replies = self.host.pipeline_calls(reqs, locked=False)
                    break
                except (cobs.CorruptFrame, TimeoutError, FramingLost):
                    # the link sent the batch once more already; a read changes nothing, so the whole batch can go
                    # again (the V003 jig's CP2102 dropped bytes twice in one 60 KB read, 2026-09-29)
                    if attempt == self.READ_TRIES - 1:
                        raise
            for off, r in zip(batch, replies):
                data = self._read_data(r.payload)
                want = min(chunk, length - off)
                while len(data) < want:                   # a short answer: read on from where it stopped
                    more = self._read_data(self._call(self.READ, struct.pack(
                        "<IQI", g, position + off + len(data), want - len(data)), locked=False).payload)
                    if not more:
                        raise h.ProtocolError(f"read at {position + off + len(data)} returned nothing")
                    data += more
                out += data
        return bytes(out)

    def read_segment(self, segment: Segment) -> bytes:
        """The segment's bytes (of its generation); every Host.on_capture callback gets them as a CaptureRecord (a run
        recorder, e.g. pytest-embedded-wireskein, without this package knowing it)."""
        c = self.config
        n = (segment.samples * c.width + 7) // 8 if c.width else segment.samples * len(c.order) * c.slot // 8
        data = self.read(segment.position, n, segment.generation or None)
        if self.host.on_capture:
            record = CaptureRecord(self.fn, self.name, c, segment, data, self.armed_s, time.monotonic())
            for callback in list(self.host.on_capture):
                callback(record)
        return data

    # ---- the §3.0 layout ---------------------------------------------------------------------------------
    def channel(self, data: bytes, k: int, samples: int | None = None) -> list[int]:
        """Channel k's values, one per sample (§3.0 rules 1-3)."""
        c = self.config
        n = samples if samples is not None else len(data) * 8 // c.width
        bit0 = c.positions[k]
        return [(data[(i * c.width + bit0) >> 3] >> ((i * c.width + bit0) & 7)) & 1 for i in range(n)]

    def to_sr(self, path: str, data: bytes, samples: int, names: list[str] | None = None) -> None:
        """A sigrok session file: bit k of each sample = channel k, one byte per sample up to 8 channels, two (little
        endian, sigrok's unitsize 2) up to 16."""
        c = self.config
        n_ch = len(c.positions)
        unit = 1 if n_ch <= 8 else 2
        if n_ch > 16:
            raise ValueError("to_sr writes up to 16 channels")
        names = names or [f"D{k}" for k in range(n_ch)]
        chans = [self.channel(data, k, samples) for k in range(n_ch)]
        out = b"".join(sum(chans[k][i] << k for k in range(n_ch)).to_bytes(unit, "little") for i in range(samples))
        rate = c.rate.numerator // c.rate.denominator
        meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1",
                f"total probes={n_ch}", f"samplerate={rate} Hz", "total analog=0"]
        meta += [f"probe{k + 1}={nm}" for k, nm in enumerate(names)] + [f"unitsize={unit}", ""]
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("version", "2")
            z.writestr("metadata", "\n".join(meta))
            z.writestr("logic-1-1", out)


@dataclass
class Calibration:
    """What the probe knows for turning an analog value into a voltage (oep-if-capture §3.8), raw: the probe applies
    none of it. factory: (frontend or None, scheme, raw bytes) - scheme names how to read raw; vrefint: (raw, ns), the
    internal reference measured after the last start, and vrefint_nominal_mv its nominal voltage (what the supply is
    worked back from)."""
    factory: list[tuple[int | None, str, bytes]] = field(default_factory=list)
    vrefint: tuple[int, int] | None = None
    vrefint_nominal_mv: int | None = None


class AnalogCapture(LogicCapture):
    """Basic analog capture (oep.fixture.analog): the same operations as the logic one; values are raw (§1.2)."""
    NAME = "oep.fixture.analog"
    ANALOG = True
    CALIBRATION = _ANA.op["calibration"]

    def values(self, data: bytes, k: int, samples: int | None = None) -> list[int]:
        """Channel k's raw values (§1.2: slot s bits little endian, the value in bits o .. o+b-1, frames in `order`)."""
        c = self.config
        width = c.slot // 8
        frame = width * len(c.order)
        n = samples if samples is not None else len(data) // frame
        m_ = c.order.index(k)
        mask = (1 << c.bits) - 1
        return [(int.from_bytes(data[i * frame + m_ * width:i * frame + (m_ + 1) * width], "little") >> c.offset) & mask
                for i in range(n)]

    def millivolts(self, k: int, value: int) -> float:
        """The probe's own 1st-order reading of a raw value of channel k, (value - zero) x scale_nv (a nominal
        reference: see reference and calibration() for others)."""
        c = self.config
        return (value - c.zero.get(k, 0)) * c.scale_nv.get(k, 0) / 1_000_000

    def calibration(self) -> Calibration:
        out = Calibration()
        for tag, v in tlvs(self._call(self.CALIBRATION, locked=False).payload):
            if tag == FACTORY:                               # frontend scheme_len scheme raw_len(u16) raw
                rd = m.Reader(v)
                fe = rd.u8()
                scheme = rd.counted("B").decode("utf-8", "replace")
                out.factory.append((None if fe == 0xFF else fe, scheme, bytes(rd.counted("H"))))
            elif tag == VREFINT:                             # raw(u32) ns(u64) nominal_mv(u32)
                raw, ns, nominal = struct.unpack_from("<IQI", v)
                out.vrefint, out.vrefint_nominal_mv = (raw, ns), nominal
        return out


@dataclass
class GroupStatus:
    state: int
    start_ns: int | None
    trigger_ns: int | None
    trigger_fn: int | None


class CaptureGroup(Interface):
    """oep.fixture.capture-group (§4): tracks (LogicCapture / AnalogCapture, each configured as usual) started together,
    one of them the trigger. Each track is read as usual; a track's offset is its first segment's start_ns minus the
    group's start_ns, and every track's segment marks the trigger's instant (trigger_index)."""
    NAME = "oep.fixture.capture-group"
    REVISION = 1
    BIND, START, STOP, FORCE, STATUS = (_GRP.op[k] for k in ("bind", "start", "stop", "force", "status"))
    TAG_TRIGGER_TRACK = _GRP.tlv["bind"]["trigger_track"]
    EVENT_TRIGGERED, EVENT_STOPPED = _GRP.event["triggered"], _GRP.event["stopped"]
    NO_TIME = 0xFFFFFFFFFFFFFFFF

    def bind(self, tracks: list[LogicCapture], trigger: LogicCapture | None = None) -> None:
        """Bind these (configured) tracks; [] unbinds. `trigger`: the track whose configure trigger starts them all."""
        body = struct.pack(f"<B{len(tracks)}H", len(tracks), *(t.fn for t in tracks))
        if trigger is not None:
            body += m.tlv(self.TAG_TRIGGER_TRACK, struct.pack("<H", trigger.fn), critical=True)
        self._call(self.BIND, body)

    def start(self, tracks: list[LogicCapture] = ()) -> tuple[int, int]:
        """-> (blocking_ms, the group's start_ns). `tracks`: whose armed_s and generation to set (the answer's TLV
        generations names each track's new generation; self.generations keeps them by fn)."""
        rd = m.Reader(self._call(self.START, expect_ms=max([t.config.blocking_ms for t in tracks if t.config] or [0]))
                      .payload)
        blocking, start_ns = rd.take("IQ")
        gens = rd.tail().get(GROUP_GENERATIONS) or b""
        self.generations = {fn: g for fn, g in struct.iter_unpack("<HI", gens[:len(gens) // 6 * 6])}
        now = time.monotonic()
        for t in tracks:
            t.armed_s = now
            if t.fn in self.generations:
                t.generation = self.generations[t.fn]
        return blocking, start_ns

    def stop(self) -> None:
        self._call(self.STOP)

    def force(self) -> None:
        self._call(self.FORCE)

    def status(self) -> GroupStatus:
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        state, start, trig, fn = rd.take("BQQH")
        rd.tail()
        none = self.NO_TIME
        return GroupStatus(state, None if start == none else start, None if trig == none else trig, fn or None)

    def wait(self, timeout: float = 5.0, keepalive_s: float = 1.0) -> GroupStatus:
        """Poll until every track is done (one-shot). The lock is kept alive every `keepalive_s` while it waits (a
        trigger may come later than the lease)."""
        deadline = time.monotonic() + timeout
        kept = time.monotonic()
        while time.monotonic() < deadline:
            if keepalive_s and self.host.session is not None and time.monotonic() - kept >= keepalive_s:
                self.host.keepalive()
                kept = time.monotonic()
            st = self.status()
            if st.state == STATE["done"]:
                return st
            if st.state == STATE["error"]:
                raise h.Failed(None, "the group's capture stopped with an error")
            time.sleep(0.002)
        raise TimeoutError("the group's capture did not finish")

