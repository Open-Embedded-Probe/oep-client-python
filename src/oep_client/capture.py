"""oep.fixture.logic / oep.fixture.analog / oep.fixture.capture-group revision 1 (oep-spec
interfaces/oep-if-capture.ja.md: §1 layouts, §2 segments, §3 operations, §3.7 calibration, §4 groups). Numbers from
`registry`.

Times are the probe's one clock (ns since its boot, comparable within one boot_id): estimates with an uncertainty, the
probe's known corrections applied. Analog values are always raw; the probe's 1st-order scale, its calibration data and
its reference are for the host to choose from.

Every start begins a new generation (u32: 1 at the first start after boot, 0 only before the first start; a start past
0xFFFFFFFF is refused unavailable cause 2 until a reboot; compared for equality only): segment serials and positions
count from 0 inside it, and read and release name it, so a read sent for the last capture never returns the next one's
bytes (oep-if-capture §0, §3.2). The client keeps it (`LogicCapture.generation`, from start / status / the group's
start) and passes it on; `read_segment` takes the segment's own. Every event carries the generation it was made in
(segment, stopped, triggered; the group's stopped carries the group's): an event of an earlier start may come after the
start's answer (core §10.4), so `LogicCapture.events` / `CaptureGroup.events` drop one whose generation is not the
current one (`unpack_event` reads any). Segment serials wrap (core §2.6) and segments pages by common §1.3.

configure (§3.3's contract, checked here before sending - ValueError): mode and rate always; samples in modes 1 and 2,
never in mode 3; segments in mode 2 only; pretrigger only with a trigger (type other than 0); rate, samples and
segments 1 or more; a type 0 trigger goes as role 0 value 0. The table's TLVs go without the critical bit (every probe
of the interface implements them). A value the probe cannot honour is refused unsupported with the tag as received
(core §2.3) - which one, when several apply, is the probe's choice, so this client never relies on the order. The probe
rounds samples down to its limit and the answer (Config.samples / .segments) is what holds.

Streaming (§3.4): every data frame carries its generation and its segment (segment_info: the segment's position, the
samples taken so far, start_ns and its uncertainty) and stays inside that segment; `Received.segments` keeps what the
frames said.

Errors (§2.2): a segment's bytes may arrive before the segment ends; when the track stops in error, status's write_pos
is the start of the segment not kept and the bytes received at or past it are dropped (`Received.drop_from`,
`LogicCapture.finish` does it)."""

from __future__ import annotations

import struct
import time
import zipfile
from typing import NamedTuple
from dataclasses import dataclass, field
from fractions import Fraction

from . import capture_blocks, cobs, host as h, message as m, registry as reg
from .frames import FramingLost
from .core import Interface, confirm, describe

_CAP = reg.FIXTURE_LOGIC
_ANA = reg.FIXTURE_ANALOG
_GRP = reg.FIXTURE_CAPTURE_GROUP
# configure TLVs; bit 7 of a tag = critical (the probe must reject what it cannot do)
MODE, RATE, SAMPLES, SEGMENTS, TRIGGER, PRETRIGGER, TRIGGER_OPTS, FORMAT, CARRIAGE, BLOCK_MAX = (
    _CAP.tlv["configure"][k] for k in ("mode", "rate", "samples", "segments", "trigger", "pretrigger", "trigger_opts",
                                       "format", "carriage", "block_max"))
ANSWER_FORMAT, ANSWER_BLOCK = _CAP.tlv["configure_answer"]["format"], _CAP.tlv["configure_answer"]["block"]
ANSWER_FORMAT_NAME = _CAP.tlv["configure_answer"]["format_name"]
# §3.8 / §3.9, optional (a probe declares them in its describe): configure TLVs by name, sent critical
OPTION_TLV = {**_CAP.tlv["configure"], **_ANA.tlv["configure"]}
LOGIC_USED = {k: _CAP.tlv["configure_answer"][k] for k in ("threshold_used", "glitch_used", "timebase")}
ANALOG_USED = {k: _ANA.tlv["configure_answer"][k] for k in ("coupling_used", "offset_used", "bandwidth_used",
                                                            "resolution_used", "oversample_used")}
NO_QUALIFIER = 0xFF                                        # clock: qualifier_role none (§3.8)
STREAM_BUFFER = _CAP.tlv["configure_answer"]["stream_buffer"]
JUDGE_LATENCY = _CAP.tlv["configure_answer"]["judge_latency_ns"]
DATA_FORMAT = _CAP.enum["data_format"]
LOGIC_CARRIAGE = _CAP.enum["logic_carriage"]
FRONTEND = _ANA.tlv["configure"]["frontend"]             # analog only
ACTUAL_RATE, LAYOUT, ACTUAL_SAMPLES, ACTUAL_SEGMENTS, SCALE, SKEW, FRONTEND_USED, REFERENCE = (
    _ANA.tlv["configure_answer"][k] for k in (
        "actual_rate", "layout", "actual_samples", "actual_segments", "scale", "skew", "frontend_used", "reference"))
FACTORY, VREFINT = _ANA.tlv["calibration_answer"]["factory"], _ANA.tlv["calibration_answer"]["vrefint"]
STATUS_ERROR = _CAP.tlv["status_answer"]["error"]          # status's TLV: why the state is 6
DATA_GENERATION = _CAP.tlv["data"]["generation"]           # a data frame's TLV: its generation (always in streaming)
SEGMENT_INFO = _CAP.tlv["data"]["segment_info"]            # and its segment: position samples start_ns uncertainty
REFERENCE_SOURCE = {v: k for k, v in _ANA.enum["reference_source"].items()}
REFERENCE_MEASURED = {_ANA.enum["reference_how"]["nominal"]: False, _ANA.enum["reference_how"]["measured"]: True}
CRITICAL = m.TAG_CRITICAL
ONE_SHOT, REPEAT, STREAMING = (_CAP.enum["mode"][k] for k in ("one_shot", "repeat", "streaming"))
IMMEDIATE, LEVEL, EDGE, LINKED = (_CAP.enum["trigger"][k] for k in ("immediate", "level", "edge", "linked"))
CROSS_UP, CROSS_DOWN = _ANA.enum["trigger"]["cross_up"], _ANA.enum["trigger"]["cross_down"]
STATE = _CAP.enum["state"]
SEGMENT_SLIPPED = _CAP.enum["segment_flag"]["slipped"]
STATUS_FLAGS = _CAP.enum["status_flag"]                    # dropped, slipped (reset at start)
ERRORS = {v: k for k, v in _CAP.enum["error"].items()}     # state 6's reason
# events (oep-core §10, role 0x03); the group's stopped is numbered as the tracks' (§4.2)
EVENT_SEGMENT, EVENT_STOPPED, EVENT_TRIGGERED = (_CAP.event[k] for k in ("segment", "stopped", "triggered"))
EVENT_HEADER = 6                                           # role(u8) fn(u16) seq(u16) kind(u8) (core §10.2)


def tlvs(payload: bytes) -> list[tuple[int, bytes]]:
    return m.split_tlvs(payload)


READ_GAP = 0x02                                            # read's flags bit1 (§3.2)


class ReadGap(h.ProtocolError):
    """read() asked for bytes the probe no longer has (§3.2: a position re-used or pushed on - the answer moved on to
    the next readable position, gap set): `position` is where readable data starts (write_pos when none is left)."""

    def __init__(self, asked: int, position: int):
        super().__init__(f"capture read at {asked}: those bytes are gone, readable from {position} (oep-if-capture §3.2)")
        self.asked, self.position = asked, position


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
        """flags bit2: the time base bent inside the segment - a sample taken one sample period or more late
        (oep-if-capture §2)."""
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
    named: dict[str, bytes] = field(default_factory=dict)   # named extensions (core §2.2): a probe's own diagnostics

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
    """What configure answered: the probe's actual values (oep-if-capture §3.3)."""
    rate: Fraction = Fraction(0)
    width: int = 0                               # logic: bits per sample (w)
    positions: list[int] = field(default_factory=list)   # logic: bit of channel k within a sample
    slot: int = 0                                # analog: s, o, b, order
    offset: int = 0
    bits: int = 0
    order: list[int] = field(default_factory=list)
    samples: int = 0
    segments: int = 0
    skew_ns: dict[int, int] = field(default_factory=dict)      # analog, per channel (role)
    zero: dict[int, int] = field(default_factory=dict)         # analog, per channel
    scale_nv: dict[int, int] = field(default_factory=dict)     # analog, nV per value, per channel
    frontend: dict[int, int] = field(default_factory=dict)     # analog: the frontend each channel took
    reference: tuple[str, int, bool | str] | None = None       # analog: (source, mV, measured; an unknown how as str)
    pretrigger: int = 0                                        # what this host asked
    format: int = 0                                            # the data format in use (§1; absent: 0)
    block: int = 0                                             # format 1 / 2: L
    stream_buffer: int | None = None                           # streaming: the most bytes held unsent (§3.3; None: not said)
    judge_latency_ns: int | None = None   # judging a group's trigger source condition, the longest to know it (§3.3, §4.4)
    raw_roles: list[int] = field(default_factory=list)         # format 1: the raw channels' roles (as asked)
    event_d: dict[int, int] = field(default_factory=dict)      # format 1: events role -> D (as asked)
    carriage: dict[int, tuple[int, int]] = field(default_factory=dict)   # format 2: role -> (kind, D) (as asked)
    format_name: str | None = None                             # format 0xFF: the named format in use
    timebase: Fraction | None = None                           # format 3: the delta's tick rate (Hz)
    external_clock: bool = False                               # sampled on a clock line (actual_rate 0 / 1, §3.8)
    used: dict[str, dict[int, int] | int] = field(default_factory=dict)   # §3.8's *_used: name -> group/role -> value

    def tolerable_delay_s(self, worst_bytes_per_s: float | None = None) -> float | None:
        """Streaming: how long the host may stop reading before the probe drops (§3.3: stream_buffer over the data rate;
        formats 1 / 2: pass their worst bandwidth, a lower bound). None when the probe does not say."""
        if self.stream_buffer is None:
            return None
        rate = worst_bytes_per_s if worst_bytes_per_s is not None else float(self.rate) * (
            self.width / 8 if self.width else len(self.order) * self.slot / 8)
        return self.stream_buffer / rate if rate else None

    def segment_bytes(self, samples: int) -> int:
        """Length of a segment of `samples` samples in the stream (§1.1 rule 4, §1.2 rule 3; format 2: its blocks, §1.4)."""
        if self.format == DATA_FORMAT["analog_blocks"]:
            return capture_blocks.analog_segment_bytes(samples, self.order, self.carriage, self.block, self.bits)
        if self.width:
            return (samples * self.width + 7) // 8
        return samples * len(self.order) * self.slot // 8

    @property
    def bytes(self) -> int:
        """Length of one segment in the stream (§1.1 rule 4)."""
        return self.segment_bytes(self.samples)


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
    dropped_after_error: int = 0                               # bytes at or past an error stop's write_pos, dropped
    segments: dict[int, "SegmentInfo"] = field(default_factory=dict)   # a segment's position -> what its frames said

    def drop_from(self, position: int) -> None:
        """Drop every byte at stream position `position` or later (§2.2: after an error stop, status's write_pos)."""
        if self.start is None:
            return
        keep, skipped, gaps = len(self.data), 0, sorted(self.gaps)
        for k, (index, n) in enumerate(gaps + [(len(self.data), 0)]):
            seg_from = (gaps[k - 1][0] if k else 0)              # data[seg_from:index] runs on without a gap
            if position < self.start + index + skipped:
                keep = max(seg_from, position - self.start - skipped)
                break
            skipped += n
        self.dropped_after_error += len(self.data) - keep
        del self.data[keep:]
        self.gaps = [(i, n) for i, n in self.gaps if i < keep]


@dataclass
class SegmentInfo:
    """A streaming data frame's segment_info (§3.4): the segment's position, the samples taken when the frame was made
    (1 or more; the final count once the segment ended), start_ns and its uncertainty (as §2.2's segment)."""
    position: int
    samples: int
    start_ns: int
    start_uncertainty_ns: int


def coverage(spans: list[tuple[int, int, int]], generation: int, write_pos: int) -> tuple[bool, bool]:
    """(tail reached, complete) of a generation's stream up to write_pos (§2.1 rule 2): `spans` are (generation,
    start, end) byte ranges received by notification or read. Reaching write_pos says only that the end came; the
    whole is had only when the generation's spans cover 0 .. write_pos with no hole - another generation's bytes fill
    none."""
    mine = sorted((a, b) for g, a, b in spans if g == generation)
    reached = any(b == write_pos for _, b in mine)
    at = 0
    for a, b in mine:
        if a > at:
            break
        at = max(at, b)
    return reached, at >= write_pos


def unpack_push(frame: bytes) -> tuple[int, int, int, bytes, int | None]:
    """A data frame (core §10.2: role fn seq position(u64) len(u16) data [TLV]) -> (fn, seq, position, data,
    generation): the TLV 0x01 generation, None when the frame carries none."""
    return _unpack_push(frame)[:5]


def push_segment(frame: bytes) -> SegmentInfo | None:
    """A streaming data frame's TLV 0x02 segment_info (§3.4), None when the frame carries none (or another length)."""
    return _unpack_push(frame)[5]


def _unpack_push(frame: bytes) -> tuple[int, int, int, bytes, int | None, SegmentInfo | None]:
    fn, seq, position = struct.unpack_from("<HHQ", frame, 1)
    rd = m.Reader(frame[13:])
    data = rd.counted("H")
    tail = rd.tail()
    g, info = tail.get(DATA_GENERATION), tail.get(SEGMENT_INFO)
    return (fn, seq, position, data, struct.unpack("<I", g)[0] if g is not None and len(g) == 4 else None,
            SegmentInfo(*struct.unpack("<QIQI", info)) if info is not None and len(info) == 24 else None)


def take_pushes(link, fn: int) -> list[tuple[int, int, bytes, int | None, SegmentInfo | None]]:
    """Remove this fn's data pushes (role 0x04) from the link: [(seq, position, data, generation, segment_info)],
    oldest first."""
    mine, rest = [], []
    for f in link.pushes:
        (mine if struct.unpack_from("<H", f, 1)[0] == fn else rest).append(f)
    link.pushes.clear()
    link.pushes.extend(rest)
    return [_unpack_push(f)[1:] for f in mine]


@dataclass
class Event:
    """A capture event (oep-if-capture §3.4, §4.2; core §10.2 role 0x03). `generation`: the start it was made in - a
    track's for a track's event, the group's for the group's stopped. kind "segment" has `segment`; "stopped" has
    `reason` and `error`; "triggered" has `serial`, `trigger_index`, `record`, `sample`, `trigger_ns` and `uncertainty_ns`. An event of another kind (or a
    group's other than stopped) keeps its fixed part in `raw` and generation None."""
    fn: int
    seq: int
    kind: str
    generation: int | None
    segment: Segment | None = None
    reason: int | None = None
    error: int | None = None
    serial: int | None = None
    trigger_index: int | None = None
    trigger_ns: int | None = None
    record: int | None = None                # triggered: the trigger record's serial (None: no record, §3.10)
    sample: int | None = None                # triggered: the original sample from the generation's start
    uncertainty_ns: int | None = None        # triggered: trigger_ns's +-ns
    trigger_fn: int | None = None            # a group's triggered: the track that judged it (0: force, timer)
    raw: bytes = b""

    @property
    def reason_name(self) -> str | None:
        return None if self.reason is None else STOPPED_REASONS.get(self.reason, f"reason {self.reason}")


STOPPED_REASONS = {v: k for k, v in _CAP.enum["stopped_reason"].items()}


def unpack_event(frame: bytes, group: bool = False) -> Event:
    """An event frame (role fn seq kind, then the kind's fixed part and TLVs) -> Event. `group`: the frame is a
    capture-group's (stopped only, §4.2). A fixed part shorter than its kind's raises ProtocolError."""
    if len(frame) < EVENT_HEADER or frame[0] != m.ROLE_EVENT:
        raise h.ProtocolError(f"not an event frame: {frame.hex()}")
    fn, seq = struct.unpack_from("<HH", frame, 1)
    kind, body = frame[5], frame[EVENT_HEADER:]
    rd = m.Reader(body)
    try:
        if kind == EVENT_SEGMENT and not group:
            seg = Segment.unpack(rd.bytes(SEGMENT_BYTES))
            e = Event(fn, seq, "segment", seg.generation, segment=seg)
        elif kind == EVENT_STOPPED:
            reason, error, generation = rd.take("BBI")
            e = Event(fn, seq, "stopped", generation, reason=reason, error=error)
        elif kind == EVENT_TRIGGERED and group:                # §4.4: trigger_fn record trigger_ns uncertainty generation
            trigger_fn, record, trigger_ns, uncertainty, generation = rd.take("HIQII")
            e = Event(fn, seq, "triggered", generation, trigger_ns=trigger_ns, uncertainty_ns=uncertainty,
                      record=None if record == NO_RECORD else record, trigger_fn=trigger_fn)
        elif kind == EVENT_TRIGGERED and not group:
            serial, index, record, sample, trigger_ns, uncertainty, generation = rd.take("IIIQQII")
            e = Event(fn, seq, "triggered", generation, serial=serial, trigger_index=index, trigger_ns=trigger_ns,
                      record=None if record == NO_RECORD else record, sample=sample, uncertainty_ns=uncertainty)
        else:
            return Event(fn, seq, f"kind 0x{kind:02x}", None, raw=bytes(body))
    except m.ShortPayload as err:
        raise h.ProtocolError(f"capture event kind {kind} too short: {frame.hex()}") from err
    rd.tail()
    return e


NO_RECORD = 0xFFFFFFFF
TRIGGER_RECORD_BYTES = 24                  # serial u32, sample u64, trigger_ns u64, uncertainty_ns u32 (§3.10)


CAPTURE_DROPS = "io.github.open-embedded-probe.capture-drops"


@dataclass(frozen=True)
class CaptureDrops:
    """The Arduino reference firmware's own diagnostics in a capture's status (named extension CAPTURE_DROPS, not an OEP
    definition): where data a stream dropped went, since start - bytes no free stage took, bytes the transport would not
    queue, DMA chunks rewritten before they were copied (a hole: the track stops in error), chunks the harvest queue had
    no room for - then the way the data goes (1 the zero-copy stages, 0 the store), the stages held, and the memory a
    capture draws on: internal RAM's and DMA-capable internal RAM's largest free blocks, PSRAM's total (0: none); then
    the most the stream's buffer (the DMA ring, in PSRAM on a P4) held not yet sent since start (bytes; None from a
    firmware before it); then the spill behind the ring: the most it held since start and its size (bytes, 0 0: none;
    None from a firmware before it); then every byte the stream dropped since start, whatever the way (the copied way's
    segments with no room to send too; None from a firmware before it)."""
    no_stage_bytes: int
    unqueued_bytes: int
    dma_overruns: int
    queue_overflows: int
    zero_copy: bool
    stages: int
    internal_largest: int
    dma_largest: int
    psram_total: int
    buffer_peak: int | None = None
    spill_peak: int | None = None
    spill_bytes: int | None = None
    dropped_bytes: int | None = None

    FORMAT = "<IIIIBBIIIIIIQ"
    FORMAT_BEFORE_DROPPED = "<IIIIBBIIIIII"   # a firmware before dropped_bytes (until 2026-10-21)
    FORMAT_BEFORE_SPILL = "<IIIIBBIIII"   # a firmware before the spill (until 2026-10-20)
    FORMAT_BEFORE_PEAK = "<IIIIBBIII"   # a firmware before buffer_peak (until 2026-10-20)

    @classmethod
    def unpack(cls, data: bytes) -> "CaptureDrops | None":
        """-> its fields, or None for data of another length (a form this host does not know)."""
        for form in (cls.FORMAT, cls.FORMAT_BEFORE_DROPPED, cls.FORMAT_BEFORE_SPILL, cls.FORMAT_BEFORE_PEAK):
            if len(data) == struct.calcsize(form):
                f = struct.unpack(form, data)
                return cls(*f[:4], bool(f[4]), *f[5:])
        return None

    @classmethod
    def of(cls, status: "Status") -> "CaptureDrops | None":
        data = status.named.get(CAPTURE_DROPS)
        return None if data is None else cls.unpack(data)


@dataclass(frozen=True)
class TriggerOpts:
    """configure's trigger_opts (§3.3): `arm` 1 looks once the pretrigger is there (0: at once, a short segment);
    `rearm` 1 a trigger for every segment (streaming: a record for every event, §2.1, §3.10); `when` 1 a level
    already true at arming counts; `hysteresis` (analog cross, in ADC values) back that far before the next crossing.
    The defaults are every probe's; the others as describe's trigger opts says."""
    arm: int = 1
    rearm: int = 0
    when: int = 0
    hysteresis: int = 0


@dataclass(frozen=True)
class TriggerRecord:
    """A trigger record (§3.10): serial (from 0 in the generation), the original sample from the generation's start,
    trigger_ns and its uncertainty."""
    serial: int
    sample: int
    trigger_ns: int
    uncertainty_ns: int


def take_events(link, fn: int) -> list[bytes]:
    """Remove this fn's event frames (role 0x03) from the link, oldest first."""
    mine, rest = [], []
    for f in link.events:
        (mine if struct.unpack_from("<H", f, 1)[0] == fn else rest).append(f)
    link.events.clear()
    link.events.extend(rest)
    return mine


def _options(options: dict) -> bytes:
    """§3.8 / §3.9's configure TLVs, critical but overflow (configure's `options`)."""
    forms = {"threshold": "<iI", "glitch_filter": "<I", "coupling": "<B", "offset": "<i", "bandwidth": "<I"}
    out = b""
    for name, value in options.items():
        tag = OPTION_TLV[name] | (0 if name == "overflow" else CRITICAL)   # overflow: §3.3's table, as block_max
        if name in forms:                                  # per group / role
            for key, v in sorted(value.items()):
                out += m.tlv(tag, bytes([key]) + struct.pack(forms[name], *(v if isinstance(v, tuple) else (v,))))
        elif name == "clock":
            role, edge, qualifier, level = value
            out += m.tlv(tag, bytes([role, edge, NO_QUALIFIER if qualifier is None else qualifier, level]))
        elif name == "oversample":
            out += m.tlv(tag, struct.pack("<H", value))
        elif name == "data_path":
            out += m.tlv(tag, bytes(value))
        else:                                              # resolution, overflow: u8
            out += m.tlv(tag, bytes([value]))
    return out


def _config(payload: bytes, analog: bool) -> Config:
    """configure's answer TLVs (§3.3). A tag that does not repeat is taken from its first copy only, and a value not of
    its defined length (layout: its count and length) is broken and not used - nor replaced by a later copy (core
    §2.3). scale, skew and frontend_used repeat, one per role."""
    c = Config()
    seen: set[int] = set()
    for tag, v in tlvs(payload):
        if tag in (ACTUAL_RATE, LAYOUT, ACTUAL_SAMPLES, ACTUAL_SEGMENTS, REFERENCE, ANSWER_FORMAT, ANSWER_BLOCK, STREAM_BUFFER,
                   JUDGE_LATENCY):
            if tag in seen:
                continue
            seen.add(tag)
        if tag == ACTUAL_RATE and len(v) == 8:
            num, den = struct.unpack("<II", v)
            c.rate = Fraction(num, den) if den else c.rate
            c.external_clock = num == 0                        # 0 / 1: sampled on a clock, no time (§3.8)
        elif tag == LAYOUT and not analog and len(v) >= 2 and len(v) == 2 + v[1]:
            c.width, n = v[0], v[1]
            c.positions = list(v[2:2 + n])
        elif tag == LAYOUT and analog and len(v) >= 4 and len(v) == 4 + v[3]:
            c.slot, c.offset, c.bits, n = v[0], v[1], v[2], v[3]
            c.order = list(v[4:4 + n])
        elif tag == ANSWER_FORMAT and len(v) == 1:
            c.format = v[0]
        elif tag == ANSWER_BLOCK and len(v) == 4:
            c.block = struct.unpack("<I", v)[0]
        elif tag == STREAM_BUFFER and len(v) == 4:
            c.stream_buffer = struct.unpack("<I", v)[0]
        elif tag == JUDGE_LATENCY and len(v) == 4:
            c.judge_latency_ns = struct.unpack("<I", v)[0]
        elif tag == ACTUAL_SAMPLES and len(v) == 4:
            c.samples = struct.unpack("<I", v)[0]
        elif tag == ACTUAL_SEGMENTS and len(v) == 4:
            c.segments = struct.unpack("<I", v)[0]
        elif tag == SCALE and analog and len(v) == 9:
            role, zero, scale = struct.unpack("<Bii", v)            # signed: an inverting frontend (§3.3)
            c.zero[role], c.scale_nv[role] = zero, scale
        elif tag == SKEW and analog and len(v) == 5:
            role, ns = struct.unpack("<BI", v)
            c.skew_ns[role] = ns
        elif tag == FRONTEND_USED and analog and len(v) == 2:
            c.frontend[v[0]] = v[1]
        elif tag == ANSWER_FORMAT_NAME:
            c.format_name = c.format_name or v.decode("ascii", "replace")
        elif not analog and tag == LOGIC_USED["timebase"] and len(v) == 8:
            num, den = struct.unpack("<II", v)
            c.timebase = Fraction(num, den) if den else None
        elif not analog and tag == LOGIC_USED["threshold_used"] and len(v) == 9:
            group, mv, hyst = struct.unpack("<BiI", v)
            c.used.setdefault("threshold_used", {})[group] = (mv, hyst)
        elif not analog and tag == LOGIC_USED["glitch_used"] and len(v) == 5:
            group, ns = struct.unpack("<BI", v)
            c.used.setdefault("glitch_used", {})[group] = ns
        elif analog and tag in (ANALOG_USED["coupling_used"], ANALOG_USED["offset_used"], ANALOG_USED["bandwidth_used"]):
            name = next(k for k, t in ANALOG_USED.items() if t == tag)
            form = {"coupling_used": "<BB", "offset_used": "<Bi", "bandwidth_used": "<BI"}[name]
            if len(v) == struct.calcsize(form):
                role, value = struct.unpack(form, v)
                c.used.setdefault(name, {})[role] = value
        elif analog and tag == ANALOG_USED["resolution_used"] and len(v) == 1:
            c.used["resolution_used"] = v[0]
        elif analog and tag == ANALOG_USED["oversample_used"] and len(v) == 2:
            c.used["oversample_used"] = struct.unpack("<H", v)[0]
        elif tag == REFERENCE and analog and len(v) == 6:
            source, mv, how = struct.unpack("<BIB", v)
            # how: nominal False, measured True; an unknown value is shown as it is, not as nominal (core §2.4)
            c.reference = (REFERENCE_SOURCE.get(source, str(source)), mv, REFERENCE_MEASURED.get(how, str(how)))
    for k in c.order:
        c.skew_ns.setdefault(k, 0)                            # a role with no delay may be left out (§3.3)
    return c


class LogicCapture(Interface):
    """Basic logic capture. Channels are the plan's roles 0..C-1."""
    NAME = "oep.fixture.logic"
    REVISION = 1
    ANALOG = False
    CONFIGURE, START, STOP, FORCE, STATUS, READ, SEGMENTS, RELEASE, QUERY_OP, TRIGGERS = (
        _CAP.op[k] for k in ("configure", "start", "stop", "force", "status", "read", "segments", "release", "query",
                             "triggers"))

    generation: int | None = None         # the current capture's generation (start / status / the group's start)

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)
        self.config: Config | None = None
        self.armed_s: float | None = None     # time.monotonic() at the last start()
        self.generation = None

    def configure(self, *, rate: int, mode: int = ONE_SHOT, samples: int | None = None, segments: int | None = None,
                  trigger: tuple[int, int, int] | None = None, pretrigger: int | None = None, query: bool = False,
                  frontends: dict[int, int] | None = None, trigger_opts: TriggerOpts | None = None,
                  data_format: int = 0, events: dict[int, int] | None = None, roles: list[int] | None = None,
                  block_max: int | None = None, carriage: dict[int, tuple[int, int]] | None = None,
                  format_name: str | None = None, options: dict | None = None) -> Config:
        """-> the probe's actual values. A value the probe cannot honour is refused: host.Unsupported, .tag = the TLV as
        sent (§3.3). The TLVs go without the critical bit (§3.3). Read Config.samples / .segments: the probe rounds
        samples down.

        Format 1 (logic blocks, §1.3): `data_format` DATA_FORMAT["logic_blocks"], `events` role -> D for the channels
        carried as events (the others raw), `roles` the plan's roles (to name the raw channels), `block_max` the
        longest block (§3.3's table: without the critical bit). Format 2 (analog blocks, §1.4): `carriage` role ->
        (kind, D), kind a bit set of capture_blocks.PACKED / MINMAX / SUM (the roles not named packed). Format 4 (test
        counter) needs nothing else. Format 0xFF: `format_name`. The others go critical.

        `options` (§3.8 / §3.9, critical; a probe without one refuses it): "threshold" group -> (mv, hysteresis_mv),
        "glitch_filter" group -> ns, "clock" (role, edge, qualifier role or None, qualifier level), "coupling" role ->
        kind, "offset" role -> mV, "bandwidth" role -> Hz, "resolution" bits, "oversample" ratio, "data_path" (path,
        mux, kind), "overflow" 0 / 1 (plain: §3.3's table). The answer's values in use: Config.used, by the answer TLV's
        name.

        §3.3's contract is checked before anything is sent (ValueError): `samples` is needed in one-shot and repeat
        and not taken in streaming, `segments` is repeat's only, `pretrigger` needs a trigger (type other than 0) - a
        pretrigger of 0 without one is simply not sent."""
        for name, v in (("rate", rate), ("samples", samples), ("segments", segments)):
            if v is not None and v < 1:
                raise ValueError(f"capture configure: {name} {v} - 1 or more (oep-if-capture §3.3)")
        if trigger is not None and trigger[0] == IMMEDIATE:
            trigger = (IMMEDIATE, 0, 0)                        # a type 0 trigger's role and value are not used: 0 (§3.3)
        if mode in (ONE_SHOT, REPEAT) and samples is None:
            raise ValueError("capture configure: samples is required in one-shot and repeat (oep-if-capture §3.3)")
        if mode == STREAMING and samples is not None:
            raise ValueError("capture configure: streaming takes no samples (oep-if-capture §3.3)")
        if segments is not None and mode != REPEAT:
            raise ValueError("capture configure: segments is for repeat only (oep-if-capture §3.3)")
        if pretrigger is not None and (trigger is None or trigger[0] == IMMEDIATE):
            if pretrigger:
                raise ValueError("capture configure: a pretrigger needs a trigger (oep-if-capture §3.3)")
            pretrigger = None

        def tlv(tag: int, value: bytes) -> bytes:
            return m.tlv(tag, value)
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
        if data_format:                                      # critical: a probe without it refuses (§3.3)
            body += m.tlv(FORMAT | CRITICAL, bytes([data_format]))
        if (data_format == DATA_FORMAT["named"]) != (format_name is not None):
            raise ValueError("capture configure: format 0xFF goes with its format_name, and only it (oep-if-capture §3.3)")
        if format_name is not None:
            body += m.tlv(OPTION_TLV["format_name"] | CRITICAL, format_name.encode("ascii"))
        body += _options(options or {})
        if data_format == DATA_FORMAT["logic_blocks"] and roles is None:
            raise ValueError("capture configure: format 1 needs the plan's roles (to name the raw channels)")
        for role, d in sorted((events or {}).items()):
            body += m.tlv(CARRIAGE | CRITICAL, struct.pack("<BBI", role, LOGIC_CARRIAGE["events"], d))
        for role, (kind, d) in sorted((carriage or {}).items()):
            body += m.tlv(CARRIAGE | CRITICAL, struct.pack("<BBI", role, kind, d))
        if block_max is not None:
            body += m.tlv(BLOCK_MAX, struct.pack("<I", block_max))   # §3.3's table: every probe takes it
        if trigger_opts is not None:                         # critical: a probe without it refuses (§3.3)
            o = trigger_opts
            body += m.tlv(TRIGGER_OPTS | CRITICAL, struct.pack("<BBBI", o.arm, o.rearm, o.when, o.hysteresis))
        # query is its own operation: the lock is decided per operation, before the payload is looked at
        op = self.QUERY_OP if query else self.CONFIGURE
        c = _config(self._call(op, body, locked=not query).payload, self.ANALOG)
        c.pretrigger = pretrigger or 0
        if data_format == DATA_FORMAT["logic_blocks"]:
            c.event_d = dict(events or {})
            c.raw_roles = [r for r in sorted(roles) if r not in c.event_d]
        if c.format == DATA_FORMAT["analog_blocks"]:
            c.carriage = {r: kd for r, kd in (carriage or {}).items() if kd[0] != capture_blocks.PACKED}
        if mode == ONE_SHOT and not c.segments:
            c.segments = 1                                     # one-shot's answer has no actual_segments (§3.3)
        if not query:
            self.config = c
        return c

    def subscribe(self, min_bytes: int = 0, max_delay_ms: int = 0) -> None:
        """Events, and in streaming the data pushes (oep-core §10): send when min_bytes are ready or max_delay_ms after the
        first byte (0, 0: as soon as there is anything)."""
        self.host.subscribe(self.fn, min_bytes, max_delay_ms)

    def unsubscribe(self) -> None:
        self.host.unsubscribe(self.fn)

    stale_events: int = 0                 # events of an earlier generation that events() dropped

    def events(self, link) -> list[Event]:
        """This track's events waiting on the link (taken off it), the current generation's only: one of another
        generation belongs to an earlier start (it may come after the start's answer, core §10.4) and is dropped,
        counted in `stale_events` (oep-if-capture §3.4). A host that did not start the capture learns the generation
        from status first."""
        frames = take_events(link, self.fn)
        if frames and self.generation is None:
            self.status()
        return self._current([e for e in map(unpack_event, frames) if e.generation is not None])   # an unknown kind:
                                                                   # dropped, its seq counted (core §2.4)

    def _current(self, events: list[Event]) -> list[Event]:
        out = [e for e in events if e.generation is None or e.generation == self.generation]
        self.stale_events += len(events) - len(out)
        return out

    def stream(self, link, *, seconds: float | None = None, nbytes: int | None = None,
               into: Received | None = None, keepalive_s: float = 1.0) -> Received:
        """Streaming: collect data pushes until `nbytes` have arrived or `seconds` have passed (at least one is needed).
        A position that does not follow the previous push is a probe-side drop (a gap); a seq that skips is a lost frame.
        A push of another generation than this capture's (a leftover of the start before, oep-if-capture §3.4) is
        dropped; each push's segment_info goes to `segments`. The subscription ends with the lock, so the lock is kept alive every `keepalive_s` while collecting (a
        stream longer than the lease otherwise stopped: 35 MB missing at the end of 10 s at 150 MHz with a 10 s lease) -
        counted across calls with the same `into` (a host calling every 0.2 s lost its 30 s lease: each call began the
        count again)."""
        if seconds is None and nbytes is None:
            raise ValueError("stream() needs seconds or nbytes")
        got = into or Received()
        deadline = time.monotonic() + seconds if seconds is not None else None
        kept = getattr(got, "_kept", None) or time.monotonic()   # on from the last call with the same `into`: a host
        while True:                                               # calling in short slices still keeps the lease
            self._collect(link, got)
            if keepalive_s and time.monotonic() - kept >= keepalive_s:
                self.host.keepalive()
                kept = time.monotonic()
            got._kept = kept
            if nbytes is not None and len(got.data) >= nbytes:
                return got
            if deadline is not None and time.monotonic() >= deadline:
                return got
            link.pump(0.02, until_one=True)

    def _collect(self, link, got: Received) -> None:
        """The pushes of this fn waiting on the link into `got` (stream()'s rules: seq, generation, gaps, segments)."""
        expect_seq = getattr(got, "_seq", None)
        event_seqs = getattr(got, "_events", set())   # events share the fn's seq (they stay on the link for the caller)
        for e in link.events:
            if struct.unpack_from("<H", e, 1)[0] == self.fn:
                event_seqs.add(struct.unpack_from("<H", e, 3)[0])
        for seq, position, data, generation, info in take_pushes(link, self.fn):
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
            if info is not None:
                got.segments[info.position] = info              # the latest: samples grow while it is taken
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

    def finish(self, link, got: Received, timeout: float = 5.0) -> Received:
        """Streaming, after stop() or an error stop: collect the pushes still to come, up to the last byte captured
        (status's write position), or until `timeout`. In state 6 the bytes at or past write_pos are dropped (§2.2)."""
        st = self.status()
        end = st.write_pos
        if st.state == STATE["error"]:
            self.stream(link, seconds=min(0.1, timeout), into=got)
            got.drop_from(end)                                 # §2.2: nothing at or past write_pos is data
            return got
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if got.start is not None:
                reached = got.start + len(got.data) + sum(n for _, n in got.gaps)
                if reached == end:
                    return got
            self.stream(link, seconds=min(0.1, max(0.0, deadline - time.monotonic())), into=got)
        return got

    def start(self) -> int:
        """-> the new capture's generation (also self.generation). A start whose initialisation failed is completed
        failed with the same fixed part (§3.2): host.Failed, the state 6 and error read by status() - the generation
        that fixed part names is taken all the same (read / release name it, §0)."""
        try:
            payload = self._call(self.START).payload
        except h.Failed as e:
            if e.result is not None and len(e.result.payload) >= 4:
                self.generation = struct.unpack_from("<I", e.result.payload)[0]
                self.armed_s = time.monotonic()
            raise
        rd = m.Reader(payload)
        self.generation = rd.take("I")
        rd.tail()
        self.armed_s = time.monotonic()
        return self.generation

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
        tail = rd.tail()
        error = tail.get(STATUS_ERROR)                       # u8: another length is broken, not used (core §2.3)
        self.generation = generation
        return Status(state, done, pos, flags, generation, error[0] if error is not None and len(error) == 1 else None,
                      tail.named())

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
        """One answer's segment records from `from_serial` on (common §1.3: from_serial included; from serial_done,
        nothing and more 0; a serial no longer kept starts at the oldest kept - the serials jump). -> (segments, more)."""
        rd = m.Reader(self._call(self.SEGMENTS, struct.pack("<I", from_serial), locked=False).payload)
        more, count = rd.take("BB")
        out = [Segment.unpack(rd.bytes(SEGMENT_BYTES)) for _ in range(count)]
        rd.tail()
        return out, bool(more)

    def triggers_page(self, from_serial: int = 0, generation: int | None = None) -> tuple[list[TriggerRecord], bool]:
        """One answer's trigger records (§3.10) from `from_serial` on, paged as segments are. -> (records, more)."""
        payload = struct.pack("<II", self._generation(generation), from_serial)
        rd = m.Reader(self._call(self.TRIGGERS, payload, locked=False).payload)
        more, count = rd.take("BB")
        out = [TriggerRecord(*struct.unpack("<IQQI", rd.bytes(TRIGGER_RECORD_BYTES))) for _ in range(count)]
        rd.tail()
        return out, bool(more)

    def triggers(self, from_serial: int = 0, generation: int | None = None) -> list[TriggerRecord]:
        """Every trigger record kept from `from_serial` on; a jump in the serials is what was overwritten."""
        out: list[TriggerRecord] = []
        while True:
            page, more = self.triggers_page(from_serial, generation)
            out += page
            if not more or not page:
                return out
            from_serial = (page[-1].serial + 1) & 0xFFFFFFFF

    def segments(self, from_serial: int = 0) -> list[Segment]:
        """Every segment record from `from_serial` on, following `more` (each next page from the last serial + 1,
        until more 0)."""
        out: list[Segment] = []
        while True:
            page, more = self.segments_page(from_serial)
            out += page
            if not more or not page:
                return out
            from_serial = (page[-1].serial + 1) & 0xFFFFFFFF   # the last serial + 1 (common §1.3, core §2.6)

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

    READ_HEAD = 11   # the answer's position(u64) flags(u8) len(u16) in front of the data

    @staticmethod
    def _read_data(payload: bytes, asked: int) -> bytes:
        """read's answer (position flags len(u16) data [TLV], §3.2) -> the data, which must start at `asked`: an
        answer moved on from there (gap: the bytes were re-used or pushed on) holds other bytes - ReadGap."""
        rd = m.Reader(payload)
        position, flags = rd.take("QB")
        data = rd.counted("H")
        if flags & READ_GAP:
            raise ReadGap(asked, position)
        if position != asked and data:                     # at or past write_pos: write_pos, len 0 (no data yet)
            raise h.ProtocolError(f"capture read at {asked} answered position {position} without gap")
        return data

    def read(self, position: int, length: int, generation: int | None = None) -> bytes:
        """Bytes [position, position+length) of the stream, pipelined in frame-sized reads. `generation`: the capture
        they belong to (default: the one this client saw at start / status); the probe refuses another one as
        unavailable (cause 6), so an old read never gets the next capture's bytes.

        The reads need no lock and go without the session id, so a repeat after a broken reply is simply run again
        (reads are not deduplicated, oep-core §5.2). They do not extend the lease, though: a long read (64 KB over a
        115200 bps UART probe takes seconds) sends a keepalive between batches, or the lease lapsed mid-read, the plan
        went with it (core §9) and the capture read back nothing (2026-09-26, V003 jig)."""
        g = self._generation(generation)
        chunk = min(0xFFFF, max(1, confirm(self.host)["max_frame"] - m.RESULT_HEADER - self.READ_HEAD))
        offsets = list(range(0, length, chunk))
        out = bytearray()
        for at in range(0, len(offsets), self.BATCH):
            if at and self.host.session is not None:
                self.host.keepalive()
            batch = offsets[at:at + self.BATCH]
            for attempt in range(self.READ_TRIES):
                reqs = [self.request(self.READ, struct.pack("<IQH", g, position + off, min(chunk, length - off)))
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
                data = self._read_data(r.payload, position + off)
                want = min(chunk, length - off)
                while len(data) < want:                   # a short answer: read on from where it stopped
                    at = position + off + len(data)
                    more = self._read_data(self._call(self.READ, struct.pack(
                        "<IQH", g, at, want - len(data)), locked=False).payload, at)
                    if not more:
                        raise h.ProtocolError(f"read at {position + off + len(data)} returned nothing")
                    data += more
                out += data
        return bytes(out)

    def read_segment(self, segment: Segment) -> bytes:
        """The segment's bytes (of its generation); every Host.on_capture callback gets them as a CaptureRecord (a run
        recorder, e.g. pytest-embedded-wireskein, without this package knowing it)."""
        c = self.config
        if c.format in (DATA_FORMAT["logic_blocks"], DATA_FORMAT["state_timed"]):
            data = self._read_blocks(segment)
        else:
            data = self.read(segment.position, c.segment_bytes(segment.samples), segment.generation or None)
        if self.host.on_capture:
            record = CaptureRecord(self.fn, self.name, c, segment, data, self.armed_s, time.monotonic())
            for callback in list(self.host.on_capture):
                callback(record)
        return data

    def _read_blocks(self, segment: Segment) -> bytes:
        """A format 1 segment's bytes: its length is not in its record (§1.3), so it ends where the next segment
        begins, or at write_pos for the last; read at once, then cut by its blocks' headers."""
        after = [s.position for s in self.segments((segment.serial + 1) & 0xFFFFFFFF)
                 if s.generation == segment.generation and s.position > segment.position]
        end = after[0] if after else self.status().write_pos
        data = self.read(segment.position, end - segment.position, segment.generation or None)
        return data[:self._blocks_length(data, segment.samples)]

    def _blocks_length(self, data: bytes, samples: int) -> int:
        """The bytes the blocks of `samples` samples take at the start of `data` (from their headers, §1.3; format 3:
        its entries, §3.8)."""
        c = self.config
        if c.format == DATA_FORMAT["state_timed"]:
            return capture_blocks.state_timed_length(data, samples, c.width)
        nb = (len(c.event_d) + 7) // 8
        at = 0
        for first in range(0, samples, c.block):
            r = min(c.block, samples - first)
            head = data[at:at + 2 * nb + 4]
            if len(head) < 2 * nb + 4:
                raise h.ProtocolError(f"format 1: the segment's bytes end inside block {first // c.block}")
            fallbacks = bin(int.from_bytes(head[:nb], "little")).count("1")
            n = int.from_bytes(head[2 * nb:], "little")
            at += len(head) + (r * (len(c.raw_roles) + fallbacks) + 7) // 8 + capture_blocks.EVENT_BYTES * n
        if at > len(data):
            raise h.ProtocolError("format 1: the segment's bytes end inside its last block")
        return at

    def blocks(self, data: bytes, samples: int) -> list[capture_blocks.LogicBlock]:
        """A format 1 segment's bytes -> its blocks (raw channels as bits, events channels as ticks, §1.3)."""
        c = self.config
        return capture_blocks.read_logic_blocks(data, samples, c.raw_roles, c.event_d, c.block)

    def state_timed(self, data: bytes, samples: int) -> list[tuple[int, int]]:
        """A format 3 segment's bytes -> (ticks from start, state) per sample (§3.8; Config.timebase the tick rate)."""
        return capture_blocks.read_state_timed(data, samples, self.config.width)

    def analog_blocks(self, data: bytes, samples: int) -> list[capture_blocks.AnalogBlock]:
        """A format 2 segment's bytes -> its blocks (each channel packed values or its intervals' summaries, §1.4)."""
        c = self.config
        return capture_blocks.read_analog_blocks(data, samples, c.order, c.carriage, c.block, c.bits)

    def columns(self, data: bytes, samples: int) -> dict:
        """A format 1 segment's bytes -> role -> its raw bits or its EventColumn over the whole segment, without blocks
        (capture_blocks.segment_columns; needs numpy)."""
        c = self.config
        return capture_blocks.segment_columns(data, samples, c.raw_roles, c.event_d, c.block)

    # ---- the §3.0 layout ---------------------------------------------------------------------------------
    def channel(self, data: bytes, k: int, samples: int | None = None) -> list[int]:
        """Channel k's values, one per sample (§1.1 rules 1-3: bit i*w + pos[k] of the stream, bit j being bit j mod 8
        of byte j / 8 - any w 1-128, a sample may cross a byte boundary)."""
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
    """What the probe knows for turning an analog value into a voltage (oep-if-capture §3.7), raw: the probe applies
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

    CLIP_LOW, CLIP_HIGH = -1, 1

    def millivolts(self, k: int, value: float) -> float | None:
        """The probe's own 1st-order reading of a raw value of channel k, (value - zero) x scale_nv (a nominal
        reference: see reference and calibration() for others). None for a clipped value (§1.2 rule 6: 0 or 2^b - 1,
        the input's voltage is not known): see clipped() and ends_millivolts()."""
        if self.clipped(k, value):
            return None
        return self._linear_mv(k, value)

    def _linear_mv(self, k: int, value: float) -> float:
        c = self.config
        return (value - c.zero.get(k, 0)) * c.scale_nv.get(k, 0) / 1_000_000

    def clipped(self, k: int, value: float) -> int:
        """§1.2 rule 6: 0 when the value is a voltage; CLIP_LOW (-1) when it is the converter's code that means the input
        was at or below the low end of the frontend's range, CLIP_HIGH (+1) at or above the high end. Codes 0 and
        2^b - 1 are the ends; with a negative scale_nv (an inverting frontend) code 0 is the high end."""
        if value == 0:
            end = self.CLIP_LOW
        elif value == (1 << self.config.bits) - 1:
            end = self.CLIP_HIGH
        else:
            return 0
        return -end if self.config.scale_nv.get(k, 0) < 0 else end

    def ends_millivolts(self, k: int) -> tuple[float, float]:
        """(low end, high end) of channel k in mV: rule 4 applied to codes 0 and 2^b - 1, what a clipped value is shown
        against ("<= low", ">= high")."""
        a, b = self._linear_mv(k, 0), self._linear_mv(k, (1 << self.config.bits) - 1)
        return (min(a, b), max(a, b))

    def clip_mask(self, k: int, values: list[int]) -> list[int]:
        """clipped() of each value: 0, CLIP_LOW or CLIP_HIGH. The values themselves stay raw."""
        return [self.clipped(k, v) for v in values]

    def clip_counts(self, k: int, values: list[int]) -> tuple[int, int]:
        """(how many values are clipped low, how many high)."""
        mask = self.clip_mask(k, values)
        return mask.count(self.CLIP_LOW), mask.count(self.CLIP_HIGH)

    def calibration(self) -> Calibration:
        out = Calibration()
        seen_vrefint = False
        # a value not of its defined length (factory: its counts and lengths) is broken and not used; vrefint does not
        # repeat: its first copy only, a broken first not replaced by a later one (core §2.3)
        for tag, v in tlvs(self._call(self.CALIBRATION, locked=False).payload):
            if tag == FACTORY:                               # frontend scheme_len scheme raw_len(u16) raw
                if len(v) < 2 or len(v) < 4 + v[1] or len(v) != 4 + v[1] + struct.unpack_from("<H", v, 2 + v[1])[0]:
                    continue
                rd = m.Reader(v)
                fe = rd.u8()
                scheme = rd.counted("B").decode("utf-8", "replace")
                out.factory.append((None if fe == 0xFF else fe, scheme, bytes(rd.counted("H"))))
            elif tag == VREFINT and not seen_vrefint:        # raw(u32) ns(u64) nominal_mv(u32)
                seen_vrefint = True
                if len(v) == 16:
                    raw, ns, nominal = struct.unpack("<IQI", v)
                    out.vrefint, out.vrefint_nominal_mv = (raw, ns), nominal
        return out


@dataclass
class GroupStatus:
    state: int
    start_ns: int | None             # None before the group's first start
    generation: int = 0              # the group's (§4.1; 0 before its first start)
    trigger: tuple[int, int, int] | None = None   # §4.4, once the event happened: (fn judging, 0 force / timer; ns; +-ns)


GROUP_BIND = _GRP.tlv["bind"]
COND = _GRP.enum["trigger_cond"]
COND_SRC = _GRP.enum["cond_src"]


@dataclass(frozen=True)
class Cond:
    """A condition of the trigger source (§4.4): built with level() / edge() / cross_up() / cross_down() / above() /
    below() / timer(). `fn` the bound track (0 for timer), `role` its plan role, `kind` enum trigger_cond, `args` the
    type's arguments packed."""
    fn: int
    role: int
    kind: int
    args: bytes
    src: int = COND_SRC["track"]

    @staticmethod
    def level(track: "LogicCapture", role: int, level: int, when: int = 0) -> "Cond":
        return Cond(track.fn, role, COND["level"], struct.pack("<BB", level, when))

    @staticmethod
    def edge(track: "LogicCapture", role: int, edge: int = 0) -> "Cond":
        """edge 0 rising, 1 falling, 2 both."""
        return Cond(track.fn, role, COND["edge"], struct.pack("<B", edge))

    @staticmethod
    def cross_up(track: "LogicCapture", role: int, value: int, hysteresis: int = 0) -> "Cond":
        return Cond(track.fn, role, COND["cross_up"], struct.pack("<II", value, hysteresis))

    @staticmethod
    def cross_down(track: "LogicCapture", role: int, value: int, hysteresis: int = 0) -> "Cond":
        return Cond(track.fn, role, COND["cross_down"], struct.pack("<II", value, hysteresis))

    @staticmethod
    def above(track: "LogicCapture", role: int, value: int, hysteresis: int = 0, when: int = 0) -> "Cond":
        return Cond(track.fn, role, COND["above"], struct.pack("<IIB", value, hysteresis, when))

    @staticmethod
    def below(track: "LogicCapture", role: int, value: int, hysteresis: int = 0, when: int = 0) -> "Cond":
        return Cond(track.fn, role, COND["below"], struct.pack("<IIB", value, hysteresis, when))

    @staticmethod
    def timer(ns: int) -> "Cond":
        """ns after the stage was entered."""
        return Cond(0, 0, COND["timer"], struct.pack("<Q", ns), COND_SRC["track"])


@dataclass(frozen=True)
class Stage:
    """A stage (§4.4): its terms are OR'd, a term's conditions AND'd; `count` times the OR becomes true; `within_ns`
    from entering it (0 none); `on_timeout` 0 back to stage 0 when it passes, 1 the "should come" stage (its OR in
    time goes back to stage 0, the time passing makes it hold). `aborts`: terms that send it back to stage 0."""
    terms: list[list[Cond]]
    count: int = 1
    within_ns: int = 0
    on_timeout: int = 0
    aborts: list[list[Cond]] = field(default_factory=list)


@dataclass(frozen=True)
class TriggerSource:
    """The group's trigger source (§4.4): stages in order, the last one holding is the event."""
    stages: list[Stage]
    holdoff_ns: int = 0
    arm: int = 1
    rearm: int = 0

    def tlvs(self) -> bytes:
        """The bind TLVs, every one critical (§4.4)."""
        out = m.tlv(GROUP_BIND["trigger_source"] | CRITICAL,
                    struct.pack("<BQBB", len(self.stages), self.holdoff_ns, self.arm, self.rearm))
        for k, st in enumerate(self.stages):
            out += m.tlv(GROUP_BIND["stage"] | CRITICAL, struct.pack("<BIQB", k, st.count, st.within_ns, st.on_timeout))
            for part, terms in ((0, st.terms), (1, st.aborts)):
                for term, conds in enumerate(terms):
                    for c in conds:
                        out += m.tlv(GROUP_BIND["cond"] | CRITICAL,
                                     struct.pack("<BBBBHBB", k, part, term, c.src, c.fn, c.role, c.kind) + c.args)
        return out


class TriggerCaps(NamedTuple):
    """capture-group describe's trigger_caps (§4.4, tag 0x41): what its trigger source takes - a declaration (core §7.3),
    bind's answer is the probe's word. `types`: the condition types (enum trigger_cond, bit k = type k); `protocols`:
    serial's (bit k = protocol k); trigger_out_latency_ns None: no trigger_out."""
    max_stages: int
    max_terms: int
    max_conds: int
    cross_track: bool
    types: int
    protocols: int
    trigger_out_latency_ns: int | None

    FORMAT = "<BBBBIBI"

    @classmethod
    def unpack(cls, v: bytes) -> "TriggerCaps | None":
        """-> its fields, or None for a value of another length (a form this host does not know)."""
        if len(v) != struct.calcsize(cls.FORMAT):
            return None
        st, te, co, ct, types, prot, lat = struct.unpack(cls.FORMAT, v)
        return cls(st, te, co, bool(ct), types, prot, None if lat == 0xFFFFFFFF else lat)

    def takes(self, kind: int) -> bool:
        """Whether the condition type `kind` (COND[...]) is declared."""
        return 0 <= kind < 32 and bool(self.types >> kind & 1)

    def kinds(self) -> list[str]:
        """The declared condition types by name (COND), in number order."""
        return [name for name, k in sorted(COND.items(), key=lambda kv: kv[1]) if self.takes(k)]


class CaptureGroup(Interface):
    """oep.fixture.capture-group (§4): tracks (LogicCapture / AnalogCapture, each configured as usual with an immediate
    trigger, all in one mode) started at once by one request. Each track is read as usual; a track's offset is its
    first segment's start_ns minus the group's start_ns (no exact match is promised: each segment's start_ns and
    uncertainty place it)."""
    NAME = "oep.fixture.capture-group"
    REVISION = 1
    BIND, START, STOP, STATUS, FORCE = (_GRP.op[k] for k in ("bind", "start", "stop", "status", "force"))
    EVENT_STOPPED = _GRP.event["stopped"]
    NO_TIME = 0xFFFFFFFFFFFFFFFF

    def bind(self, tracks: list[LogicCapture], source: TriggerSource | None = None) -> None:
        """Bind these configured tracks; [] unbinds. Without `source` they start at once (immediate triggers): a track
        with a trigger of its own is refused here before sending (ValueError, §4.1). With `source` (§4.4, describe
        features bit 0) every track has the linked trigger (with its own pretrigger) and the group's event starts them.
        The probe refuses a track not in state 1/4/6 or modes apart (unavailable cause 6, `.fn`), one in another group
        (cause 4) and more than it takes together (cause 2)."""
        for t in tracks:
            if source is None and t.config is not None and t.config.pretrigger:
                raise ValueError(f"capture-group bind: fn {t.fn} has a trigger of its own - a group starts its tracks "
                                 "at once (oep-if-capture §4.1)")
        self._call(self.BIND, struct.pack(f"<B{len(tracks)}H", len(tracks), *(t.fn for t in tracks))
                   + (source.tlvs() if source is not None else b""))

    def force(self) -> None:
        """§4.4: waiting for the event, it happens now (nothing otherwise)."""
        self._call(self.FORCE)

    TRIGGER_CAPS = _GRP.tlv["describe"]["trigger_caps"]

    def trigger_caps(self) -> TriggerCaps | None:
        """describe's trigger_caps (§4.4): what the group's trigger source takes; None when the probe declares none (no
        trigger source, or a form this host does not know). A declaration: bind still has the last word (§4.3)."""
        for tag, v in describe(self.host, self.fn):
            if tag == self.TRIGGER_CAPS:
                return TriggerCaps.unpack(v)
        return None

    generation: int | None = None     # the group's current generation (start / status, §4.1)
    stale_events: int = 0             # the group's events of an earlier generation that events() dropped

    def start(self, tracks: list[LogicCapture] = ()) -> int:
        """-> the group's start_ns. The answer's fixed part: start_ns generation(the group's) n, then n x (fn,
        generation) - each bound track's new generation, in bind order (§4.1). self.generation: the group's;
        self.generations: the tracks' by fn. `tracks`: whose armed_s and generation to set. A start that failed to
        begin is completed failed with the same fixed form (§4.1): host.Failed, the generations it names taken first
        (a stopped track's short segments are read with its new one)."""
        try:
            payload = self._call(self.START).payload
        except h.Failed as e:
            if e.result is not None and len(e.result.payload) >= 13:
                self._started(e.result.payload, tracks)
            raise
        return self._started(payload, tracks)

    def _started(self, payload: bytes, tracks) -> int:
        rd = m.Reader(payload)
        start_ns, self.generation, n = rd.take("QIB")
        self.generations = dict(rd.take("HI") for _ in range(n))
        rd.tail()
        now = time.monotonic()
        for t in tracks:
            t.armed_s = now
            if t.fn in self.generations:
                t.generation = self.generations[t.fn]
        return start_ns

    def stop(self) -> None:
        self._call(self.STOP)

    def status(self) -> GroupStatus:
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        state, start, generation = rd.take("BQI")
        tail = rd.tail()
        self.generation = generation
        trig = tail.get(_GRP.tlv["status_answer"]["trigger"])
        trigger = struct.unpack("<HQI", trig) if trig is not None and len(trig) == 14 else None
        return GroupStatus(state, None if start == self.NO_TIME else start, generation, trigger)

    def events(self, link) -> list[Event]:
        """The group's own events waiting on the link (taken off it; stopped, §4.2), the current group generation's
        only - one of another generation is an earlier start's, dropped and counted in `stale_events`. The tracks'
        events stay for each track's events()."""
        frames = take_events(link, self.fn)
        if frames and self.generation is None:
            self.status()
        got = [e for e in (unpack_event(f, group=True) for f in frames) if e.generation is not None]   # core §2.4
        out = [e for e in got if e.generation is None or e.generation == self.generation]
        self.stale_events += len(got) - len(out)
        return out

    def stream(self, link, tracks: list[LogicCapture], *, seconds: float | None = None, nbytes: int | None = None,
               into: dict[int, Received] | None = None, keepalive_s: float = 1.0) -> dict[int, Received]:
        """Streaming tracks of this group (§4.1: all in mode 3, each subscribed): collect every track's pushes together,
        each into its own Received by fn (LogicCapture.stream()'s rules per track), until each has `nbytes` or
        `seconds` have passed (at least one is needed). The lock is kept alive every `keepalive_s`."""
        if seconds is None and nbytes is None:
            raise ValueError("stream() needs seconds or nbytes")
        got = into if into is not None else {}
        for t in tracks:
            got.setdefault(t.fn, Received())
        deadline = time.monotonic() + seconds if seconds is not None else None
        kept = getattr(self, "_kept", None) or time.monotonic()
        while True:
            for t in tracks:
                t._collect(link, got[t.fn])
            if keepalive_s and time.monotonic() - kept >= keepalive_s:
                self.host.keepalive()
                kept = time.monotonic()
            self._kept = kept
            if nbytes is not None and all(len(got[t.fn].data) >= nbytes for t in tracks):
                return got
            if deadline is not None and time.monotonic() >= deadline:
                return got
            link.pump(0.02, until_one=True)

    def finish(self, link, tracks: list[LogicCapture], got: dict[int, Received], timeout: float = 5.0) -> dict[int, Received]:
        """Streaming, after stop(): each track's pushes still to come (LogicCapture.finish() per track)."""
        for t in tracks:
            t.finish(link, got.setdefault(t.fn, Received()), timeout)
        return got

    def wait(self, timeout: float = 5.0, keepalive_s: float = 1.0) -> GroupStatus:
        """Poll until every track is done (one-shot). The lock is kept alive every `keepalive_s` while it waits."""
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
