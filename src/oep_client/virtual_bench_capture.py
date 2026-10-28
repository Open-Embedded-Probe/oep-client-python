# SPDX-License-Identifier: MIT
"""oep.fixture.logic / oep.fixture.analog / oep.fixture.capture-group in the virtual bench (oep-spec
interfaces/oep-if-capture.ja.md, revision 1).

What it captures is known in advance, so a receiver can check it (sample i counted from the track's start, across
segments):

- logic: sample i is the counter i, channel k bit k of it - a square wave of period 2^(k+1) samples. The layout is the
  probe's (§1.1): w is the smallest width it can make (its own list, not declared; any integer 1-128) that holds the
  channels, pos[k] = k; with a w that is not a multiple of 8 a sample may cross a byte boundary.
- analog: 12-bit values in 16-bit slots (s 16, o 0, b 12), channels in role order. Channel k's period is
  P = 64 (k // 2 + 1) samples: an even k is a square wave (4095 for the first half of the period, then 0), an odd k a sine
  round(2047.5 + 2047 sin(2 pi i / P)). zero 0, scale_nv = the frontend's range / 4095.

Time (the probe's one clock, ns): a track's first sample is at its start (the group's start, or the endpoint's clock
at start) plus the track's start offset - logic 0 (+-50 ns), analog 5 us (+-2 us), as a probe that corrected what it
knows and says how sure it is. Later segments follow at the actual rate.

Triggers observe only samples whose time has elapsed. One-shot publishes a segment only after its last sample has elapsed.

Modes: one-shot (one completed segment comes with the clock), repeat (segments come with the clock at the actual
rate, up to the ring; a full ring pauses the capture (state 5), release lets it go on) and streaming (the bytes come
with the clock and go out as data pushes while subscribed). Triggers: logic level / edge, analog cross up / down, with a
pretrigger. `slipped` sets flags bit2 on every segment (a probe whose software pace fell behind).

Every start is a new generation (u32: 1 at the first start; a start past 0xFFFFFFFF is refused unavailable cause 2
before anything changes, never re-used until a reboot): read and release must name it (another one is rejected
unavailable, cause 6), segment records, events and streaming data frames carry it. A streaming data frame carries its
segment too (segment_info, §3.4) and never crosses a segment. A capture-group starts its tracks at once (immediate
triggers only) and has a generation of its own, by the same rules (§4.1), in its start answer, status and stopped
events. Segment serials wrap (core §2.6); segments pages by common §1.3.

Data formats (§1): 0, 1 (logic blocks), 2 (analog blocks), 4 (test counter), and where declared 3 (timed states on an
external clock, ticks of TIMEBASE_HZ) and named formats (format 0xFF: its one name is format 4's bytes). The optional
features of §3.8 and §3.9 exist only where the describe declares them (virtual_bench.with_capture_options):

- threshold, bandwidth: the values in use are answered; the virtual waveforms are levels and ADC values, not voltages,
  so they do not change. The glitch filter does: a level counts once it has held the filter's samples, for the data and
  the trigger alike (one group holds every channel).
- external clock: the clock's role is no sample channel; the samples are a counter of its edges (rising at (n+1)/f,
  falling at (n+1/2)/f, both every 1/(2f) from start; f the configured rate, or the TEST HOOK `clock_hz`); a qualifier
  keeps the edges its channel's level allows.
- coupling: AC leaves the waves as they are (they have no DC); GND gives 0 V. offset moves the scale's zero (the
  waveform is defined in ADC values). resolution and oversample set b: the 12-bit waves shifted to the bits, and
  averaging 4^k to 1 adds k bits.
- external paths (TCP, UDP): the mark, then the bytes as they are taken (mux 1: in chunks of the fn; UDP: packets with
  their header, the mark a packet of its own at position 0); no data notifications, read refused; a path that goes
  (TEST HOOK Endpoint.external_disconnect) stops its tracks in error.
"""

from __future__ import annotations

import bisect
import math
import re
import struct
from dataclasses import dataclass, field
from fractions import Fraction

from . import message as m, registry as reg

CAP, ANA, GRP = reg.FIXTURE_LOGIC, reg.FIXTURE_ANALOG, reg.FIXTURE_CAPTURE_GROUP
OP = CAP.op
TLV, ANSWER = CAP.tlv["configure"], ANA.tlv["configure_answer"]
MODE, STATE = CAP.enum["mode"], CAP.enum["state"]
TRIGGER = {**CAP.enum["trigger"], **ANA.enum["trigger"]}   # logic: level / edge; analog: cross up / down (§3.3)
STOPPED, FLAG = CAP.enum["stopped_reason"], CAP.enum["segment_flag"]
EVENT = CAP.event
CALIBRATION = ANA.tlv["calibration_answer"]
NONE = 0xFFFFFFFF
NO_TIME = 0xFFFFFFFFFFFFFFFF
MAX_SAMPLES = 1 << 20                  # a virtual bench keeps its captures in memory
SEGMENT = struct.Struct("<IQIQIIBI")   # serial position samples start_ns start_uncertainty_ns trigger_index flags generation (§2)
FULL = 4095                            # the analog value's top (12 bits)
CAUSE, FN_TLV = reg.CORE.tlv["unavailable_payload"]["cause"], reg.CORE.tlv["unavailable_payload"]["fn"]
WRONG_STATE = m.tlv(CAUSE, bytes([reg.CORE.enum["unavailable_cause"]["wrong_state"]]))
LIMIT = m.tlv(CAUSE, bytes([reg.CORE.enum["unavailable_cause"]["limit"]]))
DATA_GENERATION, SEGMENT_INFO = CAP.tlv["data"]["generation"], CAP.tlv["data"]["segment_info"]
LAST_GENERATION = 0xFFFFFFFF
# the configure TLVs every track handles (§3.3); the optional ones (format_name, §3.8, §3.9) only where its describe
# declares them (VirtualCapture.option_tags) - elsewhere unknown: ignored plain, refused critical (core §2.3)
IMPLEMENTED = tuple(TLV[k] for k in ("mode", "rate", "samples", "segments", "trigger", "pretrigger", "trigger_opts",
                                     "format", "carriage", "block_max"))
FORMAT = CAP.enum["data_format"]
CARRIAGE = CAP.enum["logic_carriage"]
ACARRIAGE = ANA.enum["analog_carriage"]
BLOCK_FORMATS = (FORMAT["logic_blocks"], FORMAT["analog_blocks"])   # the data in blocks of L (§1.3 / §1.4)
PIECES = BLOCK_FORMATS + (FORMAT["state_timed"],)   # made in pieces whose bytes vary or end on a block: kept as they began
BITS = 12                              # the analog value's b (layout)
BLOCK_TOP = 4096                       # its own: the longest block L it makes (§1.3)
STREAM_BUFFER_BYTES = 1 << 20          # its own: what a stream holds unsent, said in the answer (stream_buffer, §3.3)
UNKNOWN_END = 1 << 62                  # streaming format 1: a segment dropped whole whose end is not taken yet
BLOCKS_A_TICK = 1 << 14                # streaming: the most samples one tick makes - Python makes far below a fast rate,
                                       # and catching up on all the time passed fell further behind every tick until the
                                       # bench answered nothing (wireskein: format 1 at 2 MHz x 4 lines; format 0 at 1 MHz
                                       # x 2 lines, stop answered after 12.8 s): it makes what it can and the stream runs
                                       # late instead


TIMEBASE_HZ = 100_000_000             # its own: format 3's delta tick (the answer's timebase, §3.8)
STATE_CHUNK = 256                      # its own: format 3 streams in pieces of this many samples
UDP_DATA = 1400 - 16                   # its own: a UDP packet's data after the 16-byte header (§3.9)
ESCAPE = 0xFFFFFFFF                    # format 3: a delta that only moves time on (§3.8)
NAME = re.compile(r"[a-z0-9.-]{1,64}")  # a named format's name (§1)
GND = 2                                # coupling kind GND (§3.8's table: 0 DC, 1 AC, 2 GND; no registry enum)
NO_ROLE = 0xFF                         # clock's qualifier_role: none (§3.8)
LOGIC_D, ANALOG_D = CAP.tlv["describe"], ANA.tlv["describe"]
LOGIC_T, ANALOG_T = CAP.tlv["configure"], ANA.tlv["configure"]
LOGIC_A = CAP.tlv["configure_answer"]


@dataclass
class Options:
    """The optional capture features a describe declares (§1 named formats, §3.8 front end and external clock, §3.9
    external data paths); a probe without a declaration does not have the feature, and its configure TLV is unknown
    to it (ignored plain, refused critical, core §2.3)."""
    names: tuple[str, ...] = ()
    thresholds: dict[int, tuple] = field(default_factory=dict)    # group -> (min, max, step, hyst_max, hyst_step, presets)
    glitch: dict[int, tuple[int, int, int]] = field(default_factory=dict)   # group -> (min_ns, max_ns, step_ns)
    clock: tuple[int, int, int] | None = None                    # external_clock: max_hz, edges, qualifier
    coupling: dict[int, int] = field(default_factory=dict)       # frontend -> kinds
    offset: dict[int, tuple[int, int, int]] = field(default_factory=dict)   # frontend -> (min_mv, max_mv, step_mv)
    bandwidth: dict[int, tuple[int, ...]] = field(default_factory=dict)     # frontend -> the limits (0 none)
    resolution: dict[int, int] = field(default_factory=dict)     # bits -> max_rate_hz
    oversample: tuple[int, ...] = ()
    external: dict[int, tuple[int, int, bytes]] = field(default_factory=dict)   # kind -> (muxes, capacity_kbps, value)


def options_of(decl: list[tuple[int, bytes]], analog: bool) -> Options:
    """The optional features in a capture fn's describe TLVs."""
    d, o = ANALOG_D if analog else LOGIC_D, Options()
    for tag, v in decl:
        if tag == d.get("format_names"):
            o.names += (v.decode("ascii"),)
        elif tag == d.get("external_data"):
            kind, muxes, kbps = struct.unpack_from("<BBI", v)
            o.external[kind] = (muxes, kbps, v[6:])
        elif not analog and tag == d["threshold"]:
            group, lo, hi, step, hmax, hstep = struct.unpack_from("<BiiIII", v)
            n = v[21]
            o.thresholds[group] = (lo, hi, step, hmax, hstep, struct.unpack_from(f"<{n}i", v, 22))
        elif not analog and tag == d["glitch_filter"]:
            group, lo, hi, step = struct.unpack("<BIII", v)
            o.glitch[group] = (lo, hi, step)
        elif not analog and tag == d["external_clock"]:
            o.clock = struct.unpack("<IBB", v)
        elif analog and tag == d["coupling"]:
            o.coupling[v[0]] = v[1]
        elif analog and tag == d["offset"]:
            fe, lo, hi, step = struct.unpack("<BiiI", v)
            o.offset[fe] = (lo, hi, step)
        elif analog and tag == d["bandwidth"]:
            o.bandwidth[v[0]] = struct.unpack_from(f"<{v[1]}I", v, 2)
        elif analog and tag == d["resolution"]:
            o.resolution = {v[1 + 5 * k]: struct.unpack_from("<I", v, 2 + 5 * k)[0] for k in range(v[0])}
        elif analog and tag == d["oversample"]:
            o.oversample = struct.unpack_from(f"<{v[0]}H", v, 1)
    return o


def nearest_step(want: int, lo: int, hi: int, step: int, presets=()) -> int:
    """The value nearest `want` among lo + k step (lo .. hi; none when step is 0) and the presets (§3.8)."""
    candidates = list(presets)
    if step:
        k = max(0, min((hi - lo) // step, round((want - lo) / step)))
        candidates.append(lo + k * step)
    return min(candidates, key=lambda v: (abs(v - want), v))


class ExternalPath:
    """One external data path (§3.9) as the virtual bench gives it out: TCP's bytes as they arrive, or UDP's packets;
    `connected` False once the host's end has gone (TEST HOOK: Endpoint.external_disconnect)."""
    def __init__(self, kind: int):
        self.kind, self.stream, self.packets, self.connected = kind, bytearray(), [], True


class Reject(Exception):
    def __init__(self, reason: int, payload: bytes = b""):
        self.reason, self.payload = reason, payload


def wrong_state(fn: int | None = None) -> Reject:
    """rejected unavailable, cause 6 (the state or the generation does not fit), with the track's fn in a group."""
    return Reject(m.UNAVAILABLE, WRONG_STATE + (m.tlv(FN_TLV, struct.pack("<H", fn)) if fn is not None else b""))


def exhausted(fn: int | None = None) -> Reject:
    """rejected unavailable, cause 2: no generation left until a reboot (§0: never re-used)."""
    return Reject(m.UNAVAILABLE, LIMIT + (m.tlv(FN_TLV, struct.pack("<H", fn)) if fn is not None else b""))


@dataclass
class Segment:
    serial: int
    position: int
    samples: int
    start_ns: int
    start_uncertainty_ns: int
    trigger_index: int = NONE
    flags: int = 0
    generation: int = 0

    def pack(self) -> bytes:
        return SEGMENT.pack(self.serial, self.position, self.samples, self.start_ns, self.start_uncertainty_ns,
                            self.trigger_index, self.flags, self.generation)


def pack_samples(values: list[int], w: int) -> bytes:
    """Logic samples of w bits (any w 1-128, capture §1.1): sample i at stream bits i*w .. i*w + w - 1, bit j of the
    stream bit j mod 8 of byte j / 8 - a sample may cross a byte boundary; the last byte's bits past N*w are 0."""
    if w % 8 == 0:
        return b"".join(v.to_bytes(w // 8, "little") for v in values)
    if w in (1, 2, 4):                                           # never across a byte
        out = bytearray((len(values) * w + 7) // 8)
        for i, v in enumerate(values):
            out[(i * w) >> 3] |= v << ((i * w) & 7)
        return bytes(out)
    out = bytearray((len(values) * w + 7) // 8)
    for i, v in enumerate(values):
        bit = i * w
        v <<= bit & 7
        at = bit >> 3
        while v:
            out[at] |= v & 0xFF
            v >>= 8
            at += 1
    return bytes(out)


def logic_block(raw: list[list[int]], events: list[list[int]], ds: list[int], before: list[int], L: int) -> bytes:
    """One format 1 block (§1.3) of r = len(levels) samples: the raw channels' and events channels' levels, the events
    channels' D and the level before the block (start_level). A channel whose events would pass L / 8 bytes goes raw
    (fallback, required); the others may not."""
    r = len((raw + events)[0])
    E = len(events)
    fb, ev = [], []
    for e, lv in enumerate(events):
        d, last, mine = ds[e], before[e], []
        for t in range(-(-r // d)):
            c = 0
            for v in lv[t * d:min((t + 1) * d, r)]:
                c += v != last
                last = v
            if c:
                mine.append(struct.pack("<HBB", t, e, (last << 7) | min(c, 127)))
        if len(mine) * 4 > L // 8:
            fb.append(e)
        else:
            ev += mine
    nb = (E + 7) // 8
    fallback = sum(1 << e for e in fb).to_bytes(nb, "little")
    start = sum(before[e] << e for e in range(E)).to_bytes(nb, "little")
    cols = raw + [events[e] for e in fb]
    body = pack_samples([sum(c[i] << k for k, c in enumerate(cols)) for i in range(r)], len(cols)) if cols else b""
    return fallback + start + struct.pack("<I", len(ev)) + body + b"".join(ev)


def analog_part(values: list[int], kind: int, d: int, b: int) -> bytes:
    """One channel's part of a format 2 block (§1.4): packed the r values of b bits; else for every interval of d
    (the last one partial) min and max (b bits each) and / or the sum (b + ceil(log2 d) bits), in that order - bits by
    §1's bit order, the part ending on a byte (0s)."""
    acc = at = 0
    def put(v: int, bits: int) -> None:
        nonlocal acc, at
        acc |= v << at
        at += bits
    if kind == ACARRIAGE["packed"]:
        for v in values:
            put(v, b)
    else:
        sum_bits = b + (d - 1).bit_length()
        for lo in range(0, len(values), d):
            part = values[lo:lo + d]
            if kind & ACARRIAGE["minmax"]:
                put(min(part), b)
                put(max(part), b)
            if kind & ACARRIAGE["sum"]:
                put(sum(part), sum_bits)
    return acc.to_bytes((at + 7) // 8, "little")


def logic_wave(spec: str):
    """A logic channel's waveform instead of the counter's bit: `counter`; `quiet[:LEVEL]` (never changes, default
    high); `square:PERIOD` (samples); `uart:BAUD[:TEXT]` - 8N1 characters of TEXT (default "OEP\\n") one after another
    with a character's time of idle (high) between them, at the track's rate. -> wave(i, rate) or None (the counter)."""
    kind, *args = spec.split(":", 2)
    if kind == "counter":
        return None
    if kind == "quiet":
        level = int(args[0]) if args else 1
        return lambda i, rate: level
    if kind == "square":
        half = max(1, int(args[0]) // 2)
        return lambda i, rate: (i // half) & 1
    if kind == "uart":
        baud = int(args[0])
        text = (args[1] if len(args) > 1 else "OEP\n").encode()

        def uart(i, rate):
            bit = int(i * baud / rate)                             # the bit time sample i falls in
            char, at = divmod(bit, 20)                             # 10 bits of frame, 10 of idle
            if at == 0:
                return 0                                           # start
            if at <= 8:
                return text[char % len(text)] >> (at - 1) & 1      # LSB first
            return 1                                               # stop, idle
        return uart
    raise ValueError(f"logic wave {spec!r}: counter, quiet[:LEVEL], square:PERIOD or uart:BAUD[:TEXT]")


def analog_value(k: int, i: int) -> int:
    """Channel k's value at sample i of the virtual analog waveform."""
    period = 64 * (k // 2 + 1)
    if k % 2 == 0:
        return FULL if i % period < period // 2 else 0
    return round(2047.5 + 2047 * math.sin(2 * math.pi * i / period))


@dataclass
class VirtualCapture:
    """One capture track (logic, or analog when `frontends` is given). Times: the endpoint's clock in ms, the probe's in
    ns."""
    modes: set[int]                    # the modes the describe declares
    widths: set[int]                   # logic: the w it can lay channels out in (its own, oep-if-capture §1.1)
    min_hz: int
    max_hz: int
    ring: int = 8                      # the segment records it keeps (its own, §2)
    max_read: int = 4096               # the most one read returns (its own, §3.2)
    frontends: dict[int, tuple[int, int]] = field(default_factory=dict)   # analog: n -> (min_mv, max_mv)
    max_samples: dict[int, int] = field(default_factory=dict)   # mode -> describe's max_samples (one segment)
    max_pretrigger: int = MAX_SAMPLES - 1  # describe's trigger: max_pretrigger
    slipped: bool = False
    state: int = STATE["unconfigured"]
    mode: int = MODE["one_shot"]
    rate: Fraction = Fraction(0)
    samples: int = 0
    segments_max: int = 0
    trigger: tuple[int, int, int] | None = None
    pretrigger: int = 0
    width: int = 0                     # logic: w; analog: the bytes of one sample (C x 2)
    channels: int = 0
    analog_roles: tuple[int, ...] = ()  # layout.order preserves planned role IDs (§1.2/3.1)
    frontend_of: dict[int, int] = field(default_factory=dict)   # analog: channel -> frontend
    data: bytearray = field(default_factory=bytearray)          # the stream from `base` on
    base: int = 0                                               # the byte position data[0] is at
    segs: list[Segment] = field(default_factory=list)
    serial_done: int = 0
    started_ms: int | Fraction = 0
    t0_ns: int = 0                     # the probe-clock time of the track's first sample
    capture_begin: int = 0            # first retained source sample after initial trigger
    first_segment_samples: int = 0    # short initial segment; 0 uses configured size
    first_trigger_index: int = NONE
    trigger_time_ns: int = NO_TIME     # actual trigger instant; group must not reconstruct it from the sample grid
    stream_committed_bytes: int = 0
    trigger_scan: int = 0             # waiting: next elapsed source sample to inspect
    trigger_previous: int = 0         # previous observed trigger-role value
    produced: int = 0                  # samples produced since start
    sent: int = 0                      # streaming: the byte position pushed so far
    drop_until: int = 0                # streaming: the end of a segment begun with no room to send (dropped whole)
    gap_next: bool = False             # repeat: the capture stopped for want of a segment; the next one says so
    group: object | None = None        # the VirtualGroup that binds it
    vrefint_ns: int = NO_TIME
    generation: int = 0                # +1 at every start (§3.2); 0 before the first
    flags: int = 0                     # status flags (dropped, slipped), reset at start
    error: int = 0                     # state 6's reason (status TLV error, stopped reason 3)
    exact: bool = False                # its own: any rate in rate_range (else max_hz / a whole number)
    skews: tuple[int, ...] | None = None   # its own: analog skew_ns by the order's place (None: the channels in turn)
    diagnostics: bool = True           # analog: frontend_used and reference in the configure answer (optional, §3.3)
    opts: int = 0                      # describe trigger: opts (bit 0 arm 0, bit 1 rearm 1, bit 2 when 1, bit 3 hysteresis)
    records_max: int = 0               # describe trigger_records: the trigger records it keeps (§3.10)
    arm: int = 1                       # trigger_opts (§3.3): arm 1 looks once the pretrigger is there
    rearm: int = 0                     # 1: every segment / event its own trigger (§2.1)
    when: int = 0                      # level: 0 false -> true, 1 true at arming too
    hysteresis: int = 0                # cross: back past value -/+ hysteresis before the next crossing counts
    records: list[tuple[int, int, int, int]] = field(default_factory=list)   # (serial, sample, ns, uncertainty_ns)
    record_next: int = 0               # the serial the next trigger record gets
    trigger_floor: int = 0             # rearm: the first sample a new segment may hold (the previous one's end)
    trigger_false_seen: bool = False   # level, when 0: a false sample since arming
    trigger_ready: bool = True         # cross: back past the hysteresis since the last crossing
    rearm_waiting: bool = False        # repeat, rearm 1: between segments, looking for the next trigger (state 3)
    formats: set[int] = field(default_factory=set)   # describe formats: the data formats beyond 0 (§1, §3.5)
    carriage_kinds: int = 0            # describe carriage: kinds (bit set), min_d, max_d
    carriage_d: tuple[int, int] = (1, 1)
    format: int = 0                    # the configured data format
    carriages: dict[int, tuple[int, int]] = field(default_factory=dict)   # format 1: role -> (kind, d)
    block: int = 0                     # format 1: L
    stream_segs: list[list[int]] = field(default_factory=list)
    linked: bool = False               # describe trigger types bit 7: linked to a group's trigger source (§4.4)
    logic_roles: tuple[int, ...] = ()   # logic: the planned roles, channel k the k-th
    waves: dict = field(default_factory=dict)   # logic: role -> wave(i, rate) instead of the counter's bit (logic_wave)
    options: Options = field(default_factory=Options)   # the optional features its describe declares
    fn: int = 0                                         # its fn (the external path's mark and chunks name it)
    format_name: str = ""                               # format 0xFF: the name in use
    thresholds: dict[int, tuple[int, int]] = field(default_factory=dict)   # group -> (mv, hysteresis_mv) in use
    glitch_ns: dict[int, int] = field(default_factory=dict)                # group -> ns in use
    clock: tuple[int, int, int, int] | None = None     # external clock: (role, edge, qualifier channel or None, level)
    clock_hz: Fraction | None = None                    # TEST HOOK: the clock that comes (None: the rate configured)
    edge_rate: Fraction = Fraction(0)                   # external clock: the edges a second it samples on
    phase_ns: Fraction = Fraction(0)                    # external clock: from start to edge 0
    coupling: dict[int, int] = field(default_factory=dict)    # role -> kind in use (absent: DC)
    offset_mv: dict[int, int] = field(default_factory=dict)   # role -> mV in use
    bandwidth: dict[int, int] = field(default_factory=dict)   # role -> hz in use
    bits: int = BITS                                    # analog: the converter's bits (resolution)
    ratio: int = 1                                      # analog: hardware averaging (oversample)
    path: tuple[int, int] | None = None                 # data_path 1: (kind, mux)
    paths: dict[int, ExternalPath] = field(default_factory=dict)   # the probe's external paths, by kind (the endpoint's)
    ext_sent: int = 0                                   # data_path 1: the stream bytes given to the path

    @property
    def analog(self) -> bool:
        return bool(self.frontends)

    def option_tags(self) -> set[int]:
        """The configure TLVs of the optional features its describe declares (§3.8, §3.9, named formats)."""
        T, o = (ANALOG_T if self.analog else LOGIC_T), self.options
        have = {"format_name": o.names, "data_path": o.external, "overflow": o.external, "threshold": o.thresholds,
                "glitch_filter": o.glitch, "clock": o.clock, "coupling": o.coupling, "offset": o.offset,
                "bandwidth": o.bandwidth, "resolution": o.resolution, "oversample": o.oversample}
        return {T[k] for k, v in have.items() if v and k in T}

    def repeating(self) -> set[int]:
        T = ANALOG_T if self.analog else LOGIC_T
        return {T[k] for k in ("threshold", "glitch_filter", "coupling", "offset", "bandwidth") if k in T}

    @property
    def start_offset_ns(self) -> int:
        return 5000 if self.analog else int(self.phase_ns) if self.clock is not None else 0

    @property
    def uncertainty_ns(self) -> int:
        return 2000 if self.analog else 50

    # ---- configure ------------------------------------------------------------------------------------------------
    def settle(self, got: dict[int, bytes], t, channels: int, frontends: list[tuple[int, int, int]], *,
               query: bool = False, planned_roles: list[int] | None = None) -> dict:
        """The actual values for a configure / query request (nothing changed). `t`: the request's tail reader
        (endpoint.Take: `fixed`, `refuse`). `frontends`: (role, frontend, the tag as received) asked."""
        if self.group is not None and not query:
            raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                              bytes([reg.CORE.enum["unavailable_cause"]["bound_in_group"]])))
        if not query and self.state in (STATE["waiting"], STATE["capturing"], STATE["paused"]):
            raise wrong_state()                                    # §3.2: not while it captures
        # every TLV's form first (core §2.3: a value of another length is malformed, with or without bit 7), then the
        # contract (§3.3: mode and rate required, samples in modes 1 and 2), then what this probe does not handle:
        # unsupported with the tag as received (core §2.3, capture §3.3)
        for tag, size in ((TLV["mode"], 1), (TLV["rate"], 4), (TLV["samples"], 4), (TLV["segments"], 4),
                          (TLV["trigger"], 6), (TLV["pretrigger"], 4), (TLV["trigger_opts"], 7)):
            t.fixed(got, tag, size)
        T, o = (ANALOG_T if self.analog else LOGIC_T), self.options
        # the optional TLVs it has (§3.8, §3.9): the same form rules; a repeating one (per group / role) twice for one
        # group / role is malformed, as the frontend (§3.3)
        for which, size in (("threshold", 9), ("glitch_filter", 5), ("clock", 4), ("coupling", 2), ("offset", 5),
                           ("bandwidth", 5), ("resolution", 1), ("oversample", 2), ("data_path", 3), ("overflow", 1)):
            if which in T:
                t.fixed(got, T[which], size)
        for which in ("threshold", "glitch_filter", "coupling", "offset", "bandwidth"):
            keys = [v[0] for _, v in self._copies(t, T.get(which))]
            if len(set(keys)) != len(keys):
                raise Reject(m.MALFORMED)
        if any(not NAME.fullmatch(v.decode("latin-1")) for _, v in self._copies(t, T["format_name"])):
            raise Reject(m.MALFORMED)                              # 1-64 bytes of a-z 0-9 - . (§1)
        if TLV["rate"] not in got or TLV["mode"] not in got:
            raise Reject(m.MALFORMED)                              # mode and rate are required (§3.3)
        mode = got[TLV["mode"]][0]
        asked = struct.unpack("<I", got[TLV["rate"]])[0]
        if not asked:
            raise Reject(m.MALFORMED)                              # 1 Hz or more (§3.3)
        if mode in (MODE["one_shot"], MODE["repeat"]) and TLV["samples"] not in got:
            raise Reject(m.MALFORMED)                              # samples is required in modes 1 and 2 (§3.3)
        for tag in (TLV["samples"], TLV["segments"]):
            if tag in got and not struct.unpack("<I", got[tag])[0]:
                raise Reject(m.MALFORMED)                          # 1 or more (§3.3)
        # a later copy of a tag is checked too, the first used (core §2.3): a value the definition excludes ...
        later = {tag: [(raw, v) for raw, v in t.repeated if raw & 0x7F == tag][1:] for tag in TLV.values()}
        for tag in (TLV["rate"], TLV["samples"], TLV["segments"]):
            if any(not struct.unpack("<I", v)[0] for _, v in later[tag]):
                raise Reject(m.MALFORMED)
        trigger = None
        if TLV["trigger"] in got:
            kind, role, value = struct.unpack("<BBI", got[TLV["trigger"]])   # type role value(u32)
            trigger = (kind, role, value) if kind else None
        # the contract's "only" and "never" (§3.3): refused whatever the value, with the tag as received
        if mode == MODE["streaming"] and TLV["samples"] in got:
            raise t.refuse(TLV["samples"])
        if mode != MODE["repeat"] and TLV["segments"] in got:
            raise t.refuse(TLV["segments"])
        if trigger is None and TLV["pretrigger"] in got:
            raise t.refuse(TLV["pretrigger"])                      # no trigger, or type 0
        if mode not in self.modes:
            raise t.refuse(TLV["mode"])                            # undefined (left unused) or not declared
        clock = None
        if T.get("clock") in got:                                  # external clock (§3.8): role edge qualifier level
            role, edge, qrole, qlevel = got[T["clock"]]
            max_hz, edges, qualifier = o.clock
            if edge > 2 or not edges >> edge & 1 or qlevel > 1 or qrole != NO_ROLE and (not qualifier or qrole == role):
                raise t.refuse(T["clock"])
            clock = (role, edge, None if qrole == NO_ROLE else qrole, qlevel)
        # the rate: the fastest clock that may come, up to external_clock's max_hz (§3.8); else the declared range
        lo_hz, hi_hz = (1, o.clock[0]) if clock else (self.min_hz, self.max_hz)
        if not lo_hz <= asked <= hi_hz:
            raise t.refuse(TLV["rate"])                            # out of the declared range (§3.3)
        bits, ratio = BITS, 1                                      # analog resolution and oversample (§3.8)
        if self.analog and T["resolution"] in got:
            bits = got[T["resolution"]][0]
            if bits not in o.resolution:
                raise t.refuse(T["resolution"])
        if self.analog and T["oversample"] in got:
            ratio = struct.unpack("<H", got[T["oversample"]])[0]
            if ratio != 1 and ratio not in o.oversample:
                raise t.refuse(T["oversample"])
        b = bits + (ratio.bit_length() - 1) // 2                   # averaging 4 to 1 adds a bit (its own)
        full = (1 << b) - 1
        if trigger is not None and trigger[0] == TRIGGER["linked"] and self.linked:
            trigger = (TRIGGER["linked"], 0, 0)                    # role and value not used (§3.3)
        elif trigger is not None:
            allowed = (TRIGGER["cross_up"], TRIGGER["cross_down"]) if self.analog else (TRIGGER["level"], TRIGGER["edge"])
            if trigger[0] not in allowed:
                raise t.refuse(TLV["trigger"])                     # a type not declared (or undefined)
            # the value's defined range (§3.3): level 0 / 1, edge 0 / 1 / 2, a threshold an ADC value (b 12 bits); a
            # value left undefined is unsupported with the tag as received (core §2.3)
            top = {TRIGGER["level"]: 1, TRIGGER["edge"]: 2}.get(trigger[0], full)
            if trigger[2] > top:
                raise t.refuse(TLV["trigger"])
        # ... and one this probe does not handle: unsupported with that copy's tag as received (core §2.3)
        for raw, v in later[TLV["mode"]]:
            if v[0] not in self.modes:
                raise Reject(m.UNSUPPORTED, bytes([raw]))
        for raw, v in later[TLV["rate"]]:
            if not lo_hz <= struct.unpack("<I", v)[0] <= hi_hz:
                raise Reject(m.UNSUPPORTED, bytes([raw]))
        for raw, v in later[TLV["trigger"]]:
            kind, _, value = struct.unpack("<BBI", v)
            allowed = (TRIGGER["cross_up"], TRIGGER["cross_down"]) if self.analog else (TRIGGER["level"], TRIGGER["edge"])
            if kind and (kind not in allowed or value > {TRIGGER["level"]: 1, TRIGGER["edge"]: 2}.get(kind, full)):
                raise Reject(m.UNSUPPORTED, bytes([raw]))
        for raw, v in later[TLV["pretrigger"]]:
            if struct.unpack("<I", v)[0] > self.max_pretrigger:
                raise Reject(m.UNSUPPORTED, bytes([raw]))
        arm, rearm, when, hysteresis = struct.unpack("<BBBI", got[TLV["trigger_opts"]]) if TLV["trigger_opts"] in got else (1, 0, 0, 0)
        # trigger_opts (§3.3): arm 1, rearm 0, when 0, hysteresis 0 are every probe's; the others as its describe's opts
        # say (bits 0-3), an undefined value refused as one it does not handle (core §2.3)
        if (arm > 1 or rearm > 1 or when > 1 or (not arm and not self.opts & 1) or (rearm and not self.opts & 2)
                or (when and not self.opts & 4) or (hysteresis and (not self.opts & 8 or not self.analog))
                or hysteresis > full):
            raise t.refuse(TLV["trigger_opts"])
        for tag, size in ((TLV["format"], 1), (TLV["block_max"], 4)):
            t.fixed(got, tag, size)
        fmt = got[TLV["format"]][0] if TLV["format"] in got else FORMAT["packed"]
        carriage = []                                              # (role, kind, d, the tag as received)
        for raw_tag, v in t.repeated:
            if raw_tag & 0x7F == TLV["carriage"]:
                if len(v) != 6:
                    raise Reject(m.MALFORMED)
                role, kind, d = struct.unpack("<BBI", v)
                carriage.append((role, kind, d, raw_tag))
        # carriage (§3.3): d 0, raw with d > 1, a role twice, carriage without format 1 / 2 -> malformed; an analog
        # kind is a bit set (§1.4): none, or packed with another, is a value the definition excludes
        reduced = ACARRIAGE["minmax"] | ACARRIAGE["sum"]
        excluded = ((lambda kind: not kind or kind & ACARRIAGE["packed"] and kind & reduced)
                    if self.analog else (lambda kind: False))
        if (any(d == 0 or not self.analog and kind == CARRIAGE["raw"] and d > 1 or excluded(kind)
                for _, kind, d, _ in carriage)
                or len({c[0] for c in carriage}) != len(carriage)
                or carriage and fmt not in BLOCK_FORMATS):
            raise Reject(m.MALFORMED)
        name = got.get(T["format_name"])
        if fmt == FORMAT["named"] and name is None:
            raise Reject(m.MALFORMED)                              # a named format is its name (§1)
        if name is not None and fmt != FORMAT["named"]:
            raise t.refuse(T["format_name"])                       # format 0xFF's only (§3.3)
        if fmt != FORMAT["packed"] and (fmt not in self.formats or fmt == FORMAT["logic_blocks"] and self.analog
                                        or fmt == FORMAT["analog_blocks"] and not self.analog
                                        or fmt == FORMAT["state_timed"] and (self.analog or clock is None)):
            raise t.refuse(TLV["format"])                          # not declared, or not this track's kind (format 3:
                                                                   # the external clock's, §3.8)
        if fmt == FORMAT["named"] and name.decode("ascii") not in o.names:
            raise t.refuse(T["format_name"])
        for role, kind, d, raw_tag in carriage:
            known = (kind & ~self.carriage_kinds == 0) if self.analog else bool(self.carriage_kinds >> kind & 1)
            if not known or d > 1 and not self.carriage_d[0] <= d <= self.carriage_d[1]:
                raise Reject(m.UNSUPPORTED, bytes([raw_tag]))
        block_max = struct.unpack("<I", got[TLV["block_max"]])[0] if TLV["block_max"] in got else BLOCK_TOP
        path, overflow = None, 0                                   # §3.9: path mux kind; overflow with path 1 only
        if T["data_path"] in got:
            way, mux, kind = got[T["data_path"]]
            if way > 1 or way and (kind not in o.external or mux > 1 or not o.external[kind][0] >> mux & 1):
                raise t.refuse(T["data_path"])
            path = (kind, mux) if way else None
        if T["overflow"] in got:
            overflow = got[T["overflow"]][0]
            if path is None or overflow > 1:
                raise t.refuse(T["overflow"])
        thresholds, glitch = {}, {}
        for raw, v in self._copies(t, T.get("threshold")):        # group mv hysteresis_mv (§3.8)
            group, mv, hyst = struct.unpack("<BiI", v)
            d = o.thresholds.get(group)
            if d is None or not d[0] <= mv <= d[1] or hyst > d[3]:
                raise Reject(m.UNSUPPORTED, bytes([raw]))
            thresholds[group] = (nearest_step(mv, d[0], d[1], d[2], d[5]),
                                 nearest_step(hyst, 0, d[3], d[4], () if d[4] else (hyst,)) if hyst else 0)
        for raw, v in self._copies(t, T.get("glitch_filter")):    # group ns (0: none)
            group, ns = struct.unpack("<BI", v)
            d = o.glitch.get(group)
            if d is None or ns and not d[0] <= ns <= d[1]:
                raise Reject(m.UNSUPPORTED, bytes([raw]))
            glitch[group] = nearest_step(ns, d[0], d[1], d[2]) if ns else 0
        if not channels:
            raise wrong_state()                                    # no plan (§3.2)
        role_ids = sorted(planned_roles) if planned_roles is not None else list(range(channels))
        if clock is not None:                                      # the clock's line is no sample channel (§3.8)
            if clock[0] not in role_ids or clock[2] is not None and clock[2] not in role_ids:
                raise wrong_state()                                # a role not in the fn's plan: cause 6
            role_ids = [r for r in role_ids if r != clock[0]]
            if not role_ids:
                raise t.refuse(T["clock"])                         # nothing to sample
            channels = len(role_ids)
            clock = (clock[0], clock[1], None if clock[2] is None else role_ids.index(clock[2]), clock[3])
        if trigger is not None and trigger[0] != TRIGGER["linked"] and trigger[1] not in role_ids:
            raise wrong_state()                                    # a role not in the fn's plan: cause 6 (§3.3)
        if trigger is not None and trigger[0] != TRIGGER["linked"] and not self.analog:   # logic channel k: the k-th
            trigger = (trigger[0], role_ids.index(trigger[1]), trigger[2])
        adc = min(self.max_hz, o.resolution.get(bits, self.max_hz))   # its conversions: shared by the channels, and the
        top = Fraction(adc, channels * ratio) if self.analog else self.max_hz   # averaging takes `ratio` of them a value
        if clock is not None:
            rate = Fraction(asked)                                 # the clock decides; kept as asked (§3.8)
        elif self.exact:
            if top < self.min_hz:
                raise t.refuse(TLV["rate"])                        # no rate in the range kept for this (§3.3)
            rate = Fraction(min(max(asked, self.min_hz), top))     # any rate in the range (rate_range exact)
        else:
            # Feasible divisor clocks decrease monotonically. Only the two around
            # the requested rate can be nearest; constrain both to the declared
            # minimum and this configuration's ADC budget. Compare exact
            # fractions so a closer value above the request is not lost (§3.3).
            first = max(1, -(-self.max_hz // top))
            last = self.max_hz // self.min_hz
            if first > last:
                raise t.refuse(TLV["rate"])                        # no rate in the range kept for this (§3.3)
            near = self.max_hz // asked
            candidates = {Fraction(self.max_hz, min(last, max(first, div)))
                          for div in (near, near + 1)}
            rate = min(candidates, key=lambda value: (abs(value - asked), value))
        most = self.max_samples.get(mode, MAX_SAMPLES)
        samples = struct.unpack("<I", got[TLV["samples"]])[0] if TLV["samples"] in got else min(4096, most)
        samples = max(1, min(samples, most, MAX_SAMPLES))          # rounded down to its limit; the answer says (§3.3)
        segs = struct.unpack("<I", got[TLV["segments"]])[0] if TLV["segments"] in got else self.ring
        segs = max(1, min(segs, self.ring)) if mode == MODE["repeat"] else 1
        pre = struct.unpack("<I", got[TLV["pretrigger"]])[0] if TLV["pretrigger"] in got else 0
        chosen = {}
        if self.analog:
            chosen = {k: max(self.frontends) for k in role_ids}   # the widest range unless asked
            for role, fe, tag in frontends:
                if role not in role_ids or fe not in self.frontends:
                    raise Reject(m.UNSUPPORTED, bytes([tag]))      # the frontend's tag as received (core §2.3)
                chosen[role] = fe
            width = (2 if b <= 16 else 4) * channels
        else:
            width = min((w for w in self.widths if w >= channels), default=None)
            if width is None:
                raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                                  bytes([reg.CORE.enum["unavailable_cause"]["limit"]])))   # more than a sample holds
            step = 8 // math.gcd(width, 8)                         # samples to a whole byte
            if mode == MODE["streaming"] and step > 1:             # its own split (no samples TLV in mode 3): a
                samples = max(step, samples // step * step)        # piece ends on a byte; repeat's segments take the
                # samples asked and end padded (§1.1 rule 4) - rounding down is for capacity only (§3.3)
        carried, block = {}, 0
        if fmt == FORMAT["logic_blocks"]:
            for role, kind, d, _ in carriage:
                if role not in role_ids:
                    raise wrong_state()                            # a role not in the fn's plan: cause 6 (§3.3)
                carried[role_ids.index(role)] = (kind, d)          # logic channel k: the k-th smallest role (§1.1)
            unit = math.lcm(8, *(d for _, d in carried.values()))  # L: a multiple of 8 and of every D (§1.3)
            block = min(block_max, BLOCK_TOP) // unit * unit
            if not block:
                raise t.refuse(TLV["block_max"] if block_max < unit else carriage[0][3] & 0x7F)
            width = sum(1 for k in role_ids if carried.get(k, (CARRIAGE["raw"], 1))[0] == CARRIAGE["raw"])
        elif fmt == FORMAT["analog_blocks"]:
            for role, kind, d, _ in carriage:
                if role not in role_ids:
                    raise wrong_state()                            # a role not in the fn's plan: cause 6 (§3.3)
                if kind != ACARRIAGE["packed"]:
                    carried[role] = (kind, d)                      # packed: the r values, its d not used (§1.4)
            unit = math.lcm(8, *(d for _, d in carried.values()))  # L: a multiple of 8 and of every D (§1.4)
            block = min(block_max, BLOCK_TOP) // unit * unit
            if not block:
                raise t.refuse(TLV["block_max"] if block_max < unit else carriage[0][3] & 0x7F)
        if pre > self.max_pretrigger or (mode != MODE["streaming"] and pre >= samples):
            raise t.refuse(TLV["pretrigger"])                      # past max_pretrigger, or not below samples (§3.3)
        coupling, offset, bandwidth = {}, {}, {}
        for which, into in (("coupling", coupling), ("offset", offset), ("bandwidth", bandwidth)):
            for raw, v in self._copies(t, T.get(which)):            # role and its value, by the role's frontend (§3.8)
                role = v[0]
                if role not in role_ids:
                    raise wrong_state()                            # a role not in the fn's plan: cause 6
                fe = chosen[role]
                if which == "coupling":
                    if v[1] > GND or not o.coupling.get(fe, 0) >> v[1] & 1:
                        raise Reject(m.UNSUPPORTED, bytes([raw]))
                    into[role] = v[1]
                elif which == "offset":
                    mv, d = struct.unpack_from("<i", v, 1)[0], o.offset.get(fe)
                    if d is None or not d[0] <= mv <= d[1]:
                        raise Reject(m.UNSUPPORTED, bytes([raw]))
                    into[role] = nearest_step(mv, *d)
                else:
                    hz, limits = struct.unpack_from("<I", v, 1)[0], o.bandwidth.get(fe, ())
                    if not limits or not hz and 0 not in limits:
                        raise Reject(m.UNSUPPORTED, bytes([raw]))
                    wide = 1 << 40                                 # 0: no limit, past every other
                    into[role] = min(limits, key=lambda c: abs((c or wide) - (hz or wide)))
        if path is not None and not overflow:                      # overflow 0: the worst bandwidth fits (§3.9)
            if self._worst_bytes_s(fmt, rate, width, role_ids, carried, block, clock, b) > o.external[path[0]][1] * 1000:
                raise t.refuse(T["data_path"])
        return {"mode": mode, "rate": rate, "samples": samples, "segments": segs, "trigger": trigger,
                "pretrigger": pre, "width": width, "channels": channels, "roles": role_ids, "frontends": chosen,
                "opts": (arm, rearm, when, hysteresis), "format": fmt, "carriages": carried, "block": block,
                "name": name.decode("ascii") if name is not None else "", "thresholds": thresholds, "glitch": glitch,
                "clock": clock, "coupling": coupling, "offset": offset, "bandwidth": bandwidth, "bits": bits,
                "ratio": ratio, "b": b, "path": path,
                "asked": {k for k in ("resolution", "oversample") if T.get(k) in got}}

    @staticmethod
    def _copies(t, tag: int | None) -> list[tuple[int, bytes]]:
        """Every copy of a known TLV (the tag as received, the value)."""
        return [] if tag is None else [(raw, v) for raw, v in t.repeated if raw & 0x7F == tag]

    def _worst_bytes_s(self, fmt: int, rate: Fraction, width: int, roles: list[int], carried: dict, block: int,
                       clock, b: int) -> Fraction:
        """The most bytes a second this configuration gives out (§1.3 / §1.4 / §3.8's worst bandwidth)."""
        if fmt == FORMAT["state_timed"]:
            return rate * (2 if clock[1] == 2 else 1) * (4 + -(-width // 8))
        if fmt == FORMAT["logic_blocks"]:
            ev = [d for _, d in carried.values()]
            raw = width
            most = 2 * -(-len(ev) // 8) + 4 + block * raw // 8 + sum(min(block // 8, block // d * 4) for d in ev)
            return rate * Fraction(most, block)
        if fmt == FORMAT["analog_blocks"]:
            per = sum(len(analog_part([0] * block, *carried.get(k, (ACARRIAGE["packed"], 1)), b))
                      for k in roles)
            return rate * Fraction(per, block)
        return rate * width if self.analog else rate * Fraction(width, 8)

    def apply(self, s: dict) -> None:
        self.mode, self.rate, self.samples, self.segments_max = s["mode"], s["rate"], s["samples"], s["segments"]
        self.trigger, self.pretrigger, self.width, self.channels = s["trigger"], s["pretrigger"], s["width"], s["channels"]
        self.frontend_of = s["frontends"]
        self.arm, self.rearm, self.when, self.hysteresis = s["opts"]
        self.format, self.carriages, self.block = s["format"], s["carriages"], s["block"]
        if self.format == FORMAT["state_timed"]:
            self.block = STATE_CHUNK                               # its own pieces (no block in format 3)
        self.format_name, self.thresholds, self.glitch_ns = s["name"], s["thresholds"], s["glitch"]
        self.coupling, self.offset_mv, self.bandwidth = s["coupling"], s["offset"], s["bandwidth"]
        self.bits, self.ratio, self.path, self.clock = s["bits"], s["ratio"], s["path"], s["clock"]
        if self.clock is not None:                                 # it samples on the clock's edges (§3.8)
            f = self.clock_hz or self.rate
            self.edge_rate = f * (2 if self.clock[1] == 2 else 1)
            self.phase_ns = Fraction(1_000_000_000) / f * (1 if self.clock[1] == 0 else Fraction(1, 2))
            self.rate = self.edge_rate                             # its time: the edges
        self.analog_roles = tuple(s["roles"]) if self.analog else ()
        self.logic_roles = () if self.analog else tuple(s["roles"])
        self.state = STATE["configured"]
        self._clear()

    def answer(self, s: dict) -> bytes:
        tlv = m.tlv
        c = s["channels"]
        num, den = (0, 1) if s["clock"] is not None else (s["rate"].numerator, s["rate"].denominator)
        out = tlv(ANSWER["actual_rate"], struct.pack("<II", num, den))   # 0 / 1: the clock's, no time (§3.8)
        if self.analog:
            out += tlv(ANSWER["layout"], bytes([16 if s["b"] <= 16 else 32, 0, s["b"], c]) + bytes(s["roles"]))
        elif s["format"] == FORMAT["logic_blocks"]:                # w = C = R, the raw channels (§1.3)
            out += tlv(ANSWER["layout"], bytes([s["width"], s["width"]]) + bytes(range(s["width"])))
        else:
            out += tlv(ANSWER["layout"], bytes([s["width"], c]) + bytes(range(c)))
        if s["format"] != FORMAT["packed"]:
            out += tlv(ANSWER["format"], bytes([s["format"]]))
        if s["format"] in BLOCK_FORMATS:
            out += tlv(ANSWER["block"], struct.pack("<I", s["block"]))
        if s["format"] == FORMAT["named"]:
            out += tlv(ANSWER["format_name"], s["name"].encode("ascii"))
        if s["format"] == FORMAT["state_timed"]:
            out += tlv(LOGIC_A["timebase"], struct.pack("<II", TIMEBASE_HZ, 1))
        for group, (mv, hyst) in sorted(s["thresholds"].items()):  # the values in use (§3.8)
            out += tlv(LOGIC_A["threshold_used"], struct.pack("<BiI", group, mv, hyst))
        for group, ns in sorted(s["glitch"].items()):
            out += tlv(LOGIC_A["glitch_used"], struct.pack("<BI", group, ns))
        for role, kind in sorted(s["coupling"].items()):
            out += tlv(ANSWER["coupling_used"], bytes([role, kind]))
        for role, mv in sorted(s["offset"].items()):
            out += tlv(ANSWER["offset_used"], struct.pack("<Bi", role, mv))
        for role, hz in sorted(s["bandwidth"].items()):
            out += tlv(ANSWER["bandwidth_used"], struct.pack("<BI", role, hz))
        if "resolution" in s["asked"]:
            out += tlv(ANSWER["resolution_used"], bytes([s["bits"]]))
        if "oversample" in s["asked"]:
            out += tlv(ANSWER["oversample_used"], struct.pack("<H", s["ratio"]))
        if s["mode"] == MODE["streaming"]:                         # what it holds unsent: the bench's own (§3.3)
            out += tlv(ANSWER["stream_buffer"], struct.pack("<I", STREAM_BUFFER_BYTES))
        if s["mode"] != MODE["streaming"]:                         # the rows of §3.3: actual_samples in modes 1 / 2,
            out += tlv(ANSWER["actual_samples"], struct.pack("<I", s["samples"]))
        if s["mode"] == MODE["repeat"]:                            # actual_segments in mode 2 only
            out += tlv(ANSWER["actual_segments"], struct.pack("<I", s["segments"]))
        if self.analog:                                            # scale for every role; skew 0 left out (§3.3)
            for ordinal, k in enumerate(s["roles"]):
                scale = self._scale_nv(s["frontends"][k], s["b"])
                zero = self._zero(s["offset"].get(k, 0), scale)    # the offset is in the 1st-order formula (§3.8)
                out += tlv(ANSWER["scale"], struct.pack("<Bii", k, zero, scale))   # signed (§3.3)
            for ordinal, k in enumerate(s["roles"]):
                skew = self._skew(ordinal, s["rate"], c)
                if skew:
                    out += tlv(ANSWER["skew"], struct.pack("<BI", k, skew))
            if self.diagnostics:                                   # frontend_used / reference: optional diagnostics
                for k in s["roles"]:
                    out += tlv(ANSWER["frontend_used"], bytes([k, s["frontends"][k]]))
                out += tlv(ANSWER["reference"], struct.pack("<BIB", ANA.enum["reference_source"]["internal"], 1100, 0))
        return out

    def _scale_nv(self, frontend: int, b: int) -> int:
        lo, hi = self.frontends[frontend]
        return (hi - lo) * 1_000_000 // ((1 << b) - 1)

    @staticmethod
    def _zero(offset_mv: int, scale_nv: int) -> int:
        """The value of 0 V at the pin with `offset_mv` taken off the input: voltage = (value - zero) x scale_nv."""
        return round(Fraction(-offset_mv * 1_000_000, scale_nv))

    def _skew(self, ordinal: int, rate: Fraction, channels: int) -> int:
        """skew_ns of the order's place `ordinal`: its own list, else one ADC taking the channels in turn."""
        if self.skews is not None:
            return self.skews[ordinal] if ordinal < len(self.skews) else 0
        return ordinal * int(Fraction(1_000_000_000, 1) / (rate * channels))

    def calibration(self) -> bytes:
        """The analog's calibration (§3.7): a made-up two-point value per frontend, and the reference measured at start."""
        if not self.analog:
            raise Reject(m.UNKNOWN_OPERATION)
        scheme = b"org.example.virtual-bench.two-point"
        out = b""
        for fe in sorted(self.frontends):
            raw = struct.pack("<HH", 150, 3950)                     # raw at the range's 5 % / 95 %
            v = bytes([fe, len(scheme)]) + scheme + struct.pack("<H", len(raw)) + raw   # ... raw_len(u16) raw (§3.7)
            out += m.tlv(CALIBRATION["factory"], v)
        if self.vrefint_ns != NO_TIME:
            v = struct.pack("<IQI", 1365, self.vrefint_ns, 1100)   # raw ns nominal_mv: 1100 mV against 3300 mV full scale
            out += m.tlv(CALIBRATION["vrefint"], v)
        return out

    # ---- the data ---------------------------------------------------------------------------------------------------
    def _clear(self) -> None:
        self.data, self.base, self.segs, self.serial_done = bytearray(), 0, [], 0
        self.produced, self.sent, self.gap_next, self.drop_until = 0, 0, False, 0
        self.capture_begin, self.first_segment_samples, self.first_trigger_index = 0, 0, NONE
        self.stream_committed_bytes = 0
        self.stream_segs = []
        self.trigger_scan, self.trigger_previous = 0, 0
        self.trigger_time_ns = NO_TIME
        self.records, self.record_next, self.trigger_floor = [], 0, 0
        self.trigger_false_seen, self.trigger_ready, self.rearm_waiting = False, True, False

    def _pack(self, first: int, n: int, contiguous: bool = False) -> bytes:
        """The stream of samples first .. first + n - 1 of the track. Format 4: the byte at stream position k is
        k & 0xFF, as many as format 0's (§1); format 1: the blocks, `contiguous` with the sample before (§1.3)."""
        if self.format in (FORMAT["test_counter"], FORMAT["named"]):   # its named format: format 4's bytes
            at = self.base + len(self.data)
            return bytes((at + j) & 0xFF for j in range(self.bytes_of(n)))
        if self.format in PIECES:
            return self._blocks(first, n, contiguous)
        if self.analog:
            slot = "H" if self.width == 2 * self.channels else "I"
            return b"".join(struct.pack(f"<{self.channels}{slot}", *(self.value(k, first + i) for k in self.analog_roles))
                            for i in range(n))
        return pack_samples([sum(self.value(k, first + i) << k for k in range(self.width)) for i in range(n)], self.width)

    def _blocks(self, first: int, n: int, contiguous: bool, segment_start: bool = True) -> bytes:
        """A format 1 segment (§1.3): blocks of L from its sample 0; start_level the sample before when it is
        contiguous in the generation, else the block's first. Format 2: _analog_blocks; format 3: _timed."""
        if self.format == FORMAT["state_timed"]:
            return self._timed(first, n, segment_start)
        if self.analog:
            return self._analog_blocks(first, n)
        roles = list(range(self.channels))
        raw = [k for k in roles if self.carriages.get(k, (CARRIAGE["raw"], 1))[0] == CARRIAGE["raw"]]
        ev = [k for k in roles if k not in raw]
        ds = [self.carriages[k][1] for k in ev]
        out = bytearray()
        for b in range(0, n, self.block):
            r = min(self.block, n - b)
            at = first + b
            before = [self.value(k, at - 1) if (b or contiguous) else self.value(k, at) for k in ev]
            out += logic_block([[self.value(k, at + i) for i in range(r)] for k in raw],
                               [[self.value(k, at + i) for i in range(r)] for k in ev], ds, before, self.block)
        return bytes(out)

    def _ticks(self, i: int) -> int:
        """Format 3: sample i's time in timebase ticks from start (§3.8)."""
        return int((self.phase_ns + Fraction(self.edge_of(i) * 1_000_000_000) / self.rate) * TIMEBASE_HZ
                   // 1_000_000_000)

    def _timed(self, first: int, n: int, segment_start: bool) -> bytes:
        """Format 3 (§3.8): per sample delta(u32) - ticks from the sample before, a segment's first from start - and
        the state (w bits to a byte); a delta of 0xFFFFFFFF or more first goes as escapes (time only, no sample)."""
        out, size = bytearray(), -(-self.width // 8)
        before = 0 if segment_start else self._ticks(first - 1)
        for i in range(first, first + n):
            now = self._ticks(i)
            delta, before = now - before, now
            while delta >= ESCAPE:
                out += struct.pack("<I", ESCAPE) + bytes(size)
                delta -= ESCAPE
            out += struct.pack("<I", delta) + pack_samples([sum(self.value(k, i) << k for k in range(self.width))],
                                                           self.width)
        return bytes(out)

    def _analog_blocks(self, first: int, n: int) -> bytes:
        """A format 2 segment (§1.4): blocks of L from its sample 0, each the channels' parts in the order's order, a
        part ending on a byte."""
        out = bytearray()
        for b in range(0, n, self.block):
            r = min(self.block, n - b)
            for k in self.analog_roles:
                kind, d = self.carriages.get(k, (ACARRIAGE["packed"], 1))
                out += analog_part([self.value(k, first + b + i) for i in range(r)], kind, d,
                                   self.bits + (self.ratio.bit_length() - 1) // 2)
        return bytes(out)

    def value(self, k: int, i: int) -> int:
        """Channel k's value at sample i as it is given out: analog in the configured b bits (resolution, oversample,
        GND coupling); logic after the glitch filter, on the external clock's edges counted."""
        if self.analog:
            b = self.bits + (self.ratio.bit_length() - 1) // 2
            if self.coupling.get(k) == GND:                        # the input grounded: 0 V at the pin
                zero = self._zero(self.offset_mv.get(k, 0), self._scale_nv(self.frontend_of[k], b))
                return max(0, min((1 << b) - 1, zero))
            v = analog_value(k, i)
            v = v << (self.bits - BITS) if self.bits >= BITS else v >> (BITS - self.bits)
            return v << (b - self.bits)                            # averaging the same value: the bits it adds
        ns = max(self.glitch_ns.values(), default=0)               # the bench's one group holds every channel
        n = -(-ns * self.rate // 1_000_000_000) if ns else 0
        return self._filtered(k, i, int(n)) if n > 1 else self._level(k, i)

    def _level(self, k: int, i: int) -> int:
        wave = self.waves.get(self.logic_roles[k]) if k < len(self.logic_roles) else None
        if self.clock is not None:
            i = self.edge_of(i)                                    # a counter clocked by the external clock
        return (i >> k) & 1 if wave is None else wave(i, self.rate)

    def _filtered(self, k: int, i: int, n: int) -> int:
        """The glitch filter (§3.8): a level counts once it has held n samples; a shorter pulse is not seen."""
        if k not in self.waves and self.clock is None:             # the counter: bit k holds 2^k samples
            return self._level(k, max(0, i - (n - 1))) if 1 << k >= n else self._level(k, 0)
        j = i
        while j >= 0:                                              # the last sample its level had held n samples at
            first = self._level(k, j)
            if all(self._level(k, max(0, x)) == first for x in range(j - n + 1, j)):
                return first
            j -= 1
        return self._level(k, 0)

    def edge_of(self, i: int) -> int:
        """External clock: the edge sample i was taken on (with a qualifier, its level's edges only)."""
        if self.clock is None or self.clock[2] is None:
            return i
        q, level = self.clock[2], self.clock[3]
        return ((i >> q) << (q + 1)) | (level << q) | (i & ((1 << q) - 1))

    def kept(self, edges: int) -> int:
        """External clock: the samples taken on edges 0 .. edges - 1 (the qualifier's, §3.8); else `edges`."""
        if self.clock is None or self.clock[2] is None or edges <= 0:
            return max(0, edges) if self.clock is not None else edges
        q, level = self.clock[2], self.clock[3]
        whole, part = divmod(edges, 1 << (q + 1))
        return whole * (1 << q) + max(0, min(1 << q, part - (level << q)))

    def poll_trigger(self, now_ms: int) -> list[bytes]:
        """Waiting input: inspect only elapsed, previously unobserved samples, retaining the edge predecessor. Looks
        in state 2, and with rearm 1 between repeat's segments and all through streaming (§2.1). One trigger at a time,
        but streaming's (no segment to wait for)."""
        if self.trigger is None or self.group is not None and self.state != STATE["waiting"]:
            return []
        looking = self.state == STATE["waiting"] or (
            self.state == STATE["capturing"] and self.rearm and (self.rearm_waiting or self.mode == MODE["streaming"]))
        if not looking:
            return []
        kind, role, value = self.trigger
        skew = self.channel_skew(role)
        last = self.kept(int((Fraction(now_ms * 1_000_000) - self.t0_ns - skew) * self.rate // 1_000_000_000) + 1) - 1
        events = []
        while (i := self._scan(last)) is not None:
            when = self.time_of(i) + skew
            sample = self.kept(max(0, round(Fraction(when - self.t0_ns) * self.rate / 1_000_000_000))) if self.group is not None else i
            events += self._triggered(sample, when, self.pretrigger)
            if self.mode != MODE["streaming"] or not self.rearm:
                break
        return events

    def _scan(self, last: int) -> int | None:
        """The next sample up to `last` the trigger holds at (§3.3: arm, when, hysteresis), or None; what it saw is
        kept for the next call."""
        kind, role, value = self.trigger
        if kind == TRIGGER["linked"]:
            return None                                            # the group's trigger source decides (§4.4)
        begin = self.trigger_floor + (self.pretrigger if self.arm else 0)   # arm 1: once the pretrigger is there
        if self.trigger_scan < begin:
            if last < begin:
                return None                                        # not there yet: nothing read before its time
            self.trigger_scan = begin
            self.trigger_previous = self.value(role, begin - 1) if begin > 0 else 0
        for i in range(self.trigger_scan, last + 1):
            current, previous = self.value(role, i), self.trigger_previous
            self.trigger_scan, self.trigger_previous = i + 1, current
            if kind == TRIGGER["level"]:
                if current != (value & 1):
                    self.trigger_false_seen = True
                    continue
                if self.trigger_false_seen or self.when and self.trigger_ready:   # when 1: at arming only
                    self.trigger_false_seen = self.trigger_ready = False
                    return i
            elif kind == TRIGGER["edge"]:
                if i > 0 and current != previous and (value == 2 or (value == 0) == (current == 1)):
                    return i
            elif kind in (TRIGGER["cross_up"], TRIGGER["cross_down"]):
                up = kind == TRIGGER["cross_up"]
                if not self.trigger_ready and (current < value - self.hysteresis if up else current > value + self.hysteresis):
                    self.trigger_ready = True
                if i > 0 and self.trigger_ready and (previous < value <= current if up else previous > value >= current):
                    self.trigger_ready = False
                    return i
        return None

    def channel_skew(self, role: int) -> int:
        """The advertised channel delay follows layout.order, not a dense role number."""
        if not self.analog:
            return 0
        return self._skew(self.analog_roles.index(role), self.rate, self.channels)

    def time_of(self, i: int) -> int:
        """The probe-clock time of the track's sample i."""
        return self.t0_ns + int(self.edge_of(i) * 1_000_000_000 / self.rate)

    def _segment(self, n: int, trigger_index: int = NONE, flags: int = 0) -> Segment:
        if self.gap_next:
            flags, self.gap_next = flags | FLAG["gap"], False
        seg = Segment(self.serial_done, self.base + len(self.data), n, self.time_of(self.produced), self.uncertainty_ns,
                      trigger_index, flags | (FLAG["slipped"] if self.slipped else 0), self.generation)
        if self.slipped:
            self.flags |= CAP.enum["status_flag"]["slipped"]
        self.data += self._pack(self.produced, n, contiguous=self.serial_done != 0 and not flags & FLAG["gap"])
        self.produced += n
        self.segs.append(seg)
        self.serial_done = (self.serial_done + 1) & 0xFFFFFFFF    # serials wrap (core §2.6, capture §3.2)
        return seg

    # ---- the operations ---------------------------------------------------------------------------------------------
    def start(self, now_ms: int, group_ns: int | None = None, trigger_ns: int | None = None,
              subscribed: bool = True, waiting: bool = False, pretrigger_count: int | None = None) -> list[bytes]:
        """-> the events it raises (kind(u8) fixed part). In a group: the group's start time. A new generation;
        streaming needs a subscription (§3.2); none left: cause 2 with the data kept (§0)."""
        self.startable(subscribed)
        self._clear()
        self.generation += 1
        self.flags, self.error = 0, 0
        self.started_ms = now_ms
        self.t0_ns = (group_ns if group_ns is not None else now_ms * 1_000_000) + self.start_offset_ns
        # The fixture measures the reference synchronously at start, before the
        # first channel sample's start offset. A measured value must not carry
        # that future channel timestamp (capture §3.8).
        self.vrefint_ns = now_ms * 1_000_000 if self.analog else NO_TIME
        self.ext_sent = 0
        if self.path is not None:                                  # the generation's mark first (§3.9)
            self._emit(b"OEPX" + struct.pack("<IHB5x", self.generation, self.fn, self.format), None)
        events = []
        if waiting:
            self.state = STATE["waiting"]
            return events
        if trigger_ns is not None:
            sample = self.kept(max(0, round(Fraction(trigger_ns - self.t0_ns) * self.rate / 1_000_000_000)))
            return self._triggered(sample, trigger_ns, self.pretrigger if pretrigger_count is None else pretrigger_count)
        if self.trigger:
            self.state = STATE["waiting"]
            return self.poll_trigger(now_ms)
        self.state = STATE["capturing"]
        return self.tick(now_ms)

    def startable(self, subscribed: bool = True, fn: int | None = None) -> None:
        """start's refusals, before anything changes (§3.2, §0); `fn`: the track's, named in a group's refusal."""
        if self.state in (STATE["unconfigured"], STATE["waiting"], STATE["capturing"], STATE["paused"]):
            raise wrong_state(fn)
        if self.mode == MODE["streaming"] and not subscribed and self.path is None:
            raise wrong_state(fn)                                  # the data goes as notifications (§3.4)
        if self.trigger is not None and self.trigger[0] == TRIGGER["linked"] and self.group is None:
            raise wrong_state(fn)                                  # linked, not bound to a trigger source (§3.3)
        if self.generation == LAST_GENERATION:
            raise exhausted(fn)

    def _triggered(self, sample: int, when: int, requested: int) -> list[bytes]:
        """Retain available prehistory; trigger_index is relative to this segment, not acquisition start. With rearm 1
        a record too (§3.10); streaming's later triggers cut nothing (§2.1)."""
        period = -(-1_000_000_000 // self.rate)
        uncertainty = max(self.uncertainty_ns, int(period))
        record = NONE
        if self.rearm:
            record = self.record_next
            self.records.append((record, sample, when, uncertainty))
            del self.records[:max(0, len(self.records) - self.records_max)]
            self.record_next = (record + 1) & 0xFFFFFFFF
        if self.mode == MODE["streaming"] and self.state == STATE["capturing"]:
            rel, first = sample - self.capture_begin, self.first_segment_samples or self.samples
            serial, index = (0, rel) if rel < first else (1 + (rel - first) // self.samples, (rel - first) % self.samples)
            return [self._triggered_event(serial & 0xFFFFFFFF, index, record, sample, when, uncertainty)]
        self.trigger_time_ns = when
        index = min(requested, sample - self.trigger_floor)
        begin = sample - index
        if self.state == STATE["waiting"]:
            self.capture_begin = begin
        elif begin != self.produced:
            self.gap_next = True                                   # rearm: not straight after the previous segment
        self.produced = begin
        if self.mode == MODE["streaming"]:
            self.samples = max(self.samples, requested + 1)  # internal stream segment size, not a configure TLV
        self.first_segment_samples = self.samples - requested + index
        self.first_trigger_index = index
        if self.mode == MODE["streaming"]:
            unit = 1 if self.analog else 8 // math.gcd(self.width, 8)
            self.first_segment_samples = -(-self.first_segment_samples // unit) * unit
        self.rearm_waiting = False
        self.state = STATE["capturing"]
        return [self._triggered_event(self.serial_done, index, record, sample, when, uncertainty)]

    def _triggered_event(self, serial: int, index: int, record: int, sample: int, when: int, uncertainty: int) -> bytes:
        """kind triggered (§3.4): serial trigger_index record sample(u64) trigger_ns(u64) uncertainty_ns generation."""
        return bytes([EVENT["triggered"]]) + struct.pack("<IIIQQII", serial, index, record, sample, when, uncertainty,
                                                         self.generation)

    def rearm_after(self) -> None:
        """rearm 1 in repeat: a segment ended; the next waits for its own trigger from here (§2.1)."""
        self.rearm_waiting = True
        self.trigger_floor = self.trigger_scan = self.produced
        self.trigger_false_seen, self.trigger_ready = False, True

    def trigger_list(self, generation: int, first: int, budget: int) -> bytes:
        """triggers (§3.10): more(u8) count(u8) count x record(serial sample(u64) trigger_ns(u64) uncertainty_ns),
        paged by serial as segments are (common §1.3)."""
        self.check_generation(generation)
        at = m.serial_page_start([r[0] for r in self.records], self.record_next, first)
        rows = [struct.pack("<IQQI", *r) for r in self.records[at:]]
        out = rows[:max(0, (budget - 2) // 24)][:255]
        return bytes([int(len(out) < len(rows)), len(out)]) + b"".join(out)

    def force(self, now_ms: int, *, trigger_ns: int | None = None,
              pretrigger: int | None = None) -> list[bytes]:
        """Resolve a waiting trigger without changing the generation; keep only available prehistory.

        The trigger opens acquisition; the one-shot segment still waits for its sample times.
        """
        if self.state != STATE["waiting"] and not self.rearm_waiting:
            return []
        when = trigger_ns if trigger_ns is not None else now_ms * 1_000_000
        sample = self.kept(max(0, round(Fraction(when - self.t0_ns) * self.rate / 1_000_000_000)))
        requested = self.pretrigger if pretrigger is None else pretrigger
        return self._triggered(sample, when, requested)

    def tick(self, now_ms: int) -> list[bytes]:
        events = self._tick(now_ms)
        self._forward()
        return events

    def _tick(self, now_ms: int) -> list[bytes]:
        """Time passes: repeat's segments, streaming's bytes. -> events."""
        events = self.poll_trigger(now_ms) if self.group is None else []
        if self.state != STATE["capturing"]:
            return events
        if self.mode == MODE["one_shot"]:
            n = self.first_segment_samples or self.samples
            if self.one_shot_due(now_ms) - self.produced >= n:
                # an early trigger's segment is short of pretrigger, not ended by stop: no flags bit1 (§2.2, §3.3)
                seg = self._segment(n, self.first_trigger_index)
                self.state = STATE["done"]
                events += [bytes([EVENT["segment"]]) + seg.pack(), self.stopped_event(STOPPED["complete"])]
            return events
        due = self.kept(int((now_ms - self.started_ms) * self.rate / 1000))   # samples the clock has taken since start
        if self.mode == MODE["repeat"]:
            while True:
                if self.rearm_waiting:                             # rearm 1: the next segment's own trigger first
                    got = self.poll_trigger(now_ms)
                    if not got:
                        break
                    events += got
                if due - self.produced < (self.first_segment_samples or self.samples):
                    break
                if len(self.segs) >= self.segments_max:            # no free segment: it pauses (state 5, no event; §3.2)
                    self.state = STATE["paused"]
                    break
                n = self.first_segment_samples or self.samples
                own = FLAG["rearm"] if self.rearm and self.first_trigger_index != NONE else 0   # its own trigger (§2.1)
                seg = self._segment(n, self.first_trigger_index, own)  # bit1 only when stop ends it (§2.2)
                self.first_segment_samples, self.first_trigger_index = 0, NONE
                events.append(bytes([EVENT["segment"]]) + seg.pack())
                if self.rearm and self.trigger is not None:
                    self.rearm_after()
                if len(self.segs) >= self.segments_max:
                    self.state = STATE["paused"]
                    break
        elif self.mode == MODE["streaming"] and self.format in PIECES:
            self._stream_blocks(due, whole=True)
        elif self.mode == MODE["streaming"]:
            per_byte = 1 if self.analog else 8 // math.gcd(self.width, 8)   # whole bytes only (§2.2)
            n = min((due - self.produced) // per_byte * per_byte, BLOCKS_A_TICK)
            if n > 0:
                self.data += self._pack(self.produced, n)
                self.produced += n
                taken = self.produced - self.capture_begin
                first = self.first_segment_samples or self.samples
                complete = 0 if taken < first else 1 + (taken - first) // self.samples
                self.serial_done = complete & 0xFFFFFFFF
                done_samples = 0 if not complete else first + (complete - 1) * self.samples
                self.stream_committed_bytes = self.bytes_of(done_samples)
        return events

    def _stream_geometry(self, rel: int) -> tuple[int, int, int]:
        """Streaming: the segment holding sample `rel` (from the retained start) -> (its number, first sample, size)."""
        first = self.first_segment_samples or self.samples
        if rel < first:
            return 0, 0, first
        k = (rel - first) // self.samples
        return 1 + k, first + k * self.samples, self.samples

    def _stream_blocks(self, due: int, whole: bool) -> None:
        """Format 1 streaming: the blocks the clock has taken - each whole (a block counts its events first, §1.3), at
        most BLOCKS_A_TICK samples a call; `whole` False at stop: the block being taken ends there, short."""
        if whole:
            due = min(due, self.produced + BLOCKS_A_TICK)
        else:                                                      # stop: only the block begun, not all the time behind
            rel = self.produced - self.capture_begin
            _, at, size = self._stream_geometry(rel)
            due = min(due, self.capture_begin + min(at + ((rel - at) // self.block + 1) * self.block, at + size))
        while True:
            rel = self.produced - self.capture_begin
            k, at, size = self._stream_geometry(rel)
            end = min(at + ((rel - at) // self.block + 1) * self.block, at + size)
            if due - self.capture_begin < end:
                if whole or due - self.capture_begin <= rel:
                    break
                end = due - self.capture_begin
            if rel == at:                                          # a segment begins: where its bytes start
                position = self.base + len(self.data)
                self.stream_segs.append([position, at, size])
                if self.drop_until >= UNKNOWN_END:
                    self.drop_until = position                     # the segment dropped whole ended here
            # start_level: the sample before, but for the generation's first segment (§1.3)
            self.data += self._blocks(self.produced, end - rel, contiguous=rel != 0, segment_start=rel == at)
            self.produced = self.capture_begin + end
            if end == at + size or not whole:
                self.stream_segs[-1][2] = end - at                 # its final size (short at stop)
                self.serial_done = (k + 1) & 0xFFFFFFFF
                self.stream_committed_bytes = self.base + len(self.data)
            if not whole:
                break

    def one_shot_due(self, now_ms: int) -> int:
        """Count samples whose complete frame has elapsed, including sample 0 at its timestamp.

        Analog frames include the last channel's advertised skew. Work in rational time so a
        fractional realized rate cannot produce a sample before its timestamp.
        """
        skew = (self.channels - 1) * int(Fraction(1_000_000_000, 1) / (self.rate * self.channels)) if self.analog else 0
        elapsed = Fraction(now_ms * 1_000_000) - self.t0_ns - skew
        return self.kept(max(0, int(elapsed * self.rate // 1_000_000_000) + 1))

    def stop(self, now_ms: int | None = None) -> list[bytes]:
        """Stop acquisition, committing the samples already taken in a short segment."""
        events = self._stop(now_ms)
        self._forward()                                            # an external path gets the rest (§3.9)
        return events

    def _stop(self, now_ms: int | None = None) -> list[bytes]:
        events = self.tick(now_ms) if now_ms is not None else []
        if self.state == STATE["error"]:
            self.state = STATE["configured"]
            return events
        if self.state not in (STATE["capturing"], STATE["waiting"], STATE["paused"]):
            return events
        if (self.state == STATE["capturing"] and self.mode in (MODE["one_shot"], MODE["repeat"]) and now_ms is not None
                and len(self.segs) < self.segments_max and not self.rearm_waiting):
            due = (self.one_shot_due(now_ms) if self.mode == MODE["one_shot"]
                   else self.kept(int((now_ms - self.started_ms) * self.rate / 1000)))
            partial = min((self.first_segment_samples or self.samples) - 1, max(0, due - self.produced))
            if partial:
                index = self.first_trigger_index if self.first_trigger_index < partial else NONE
                segment = self._segment(partial, index, flags=FLAG["short"])
                events.append(bytes([EVENT["segment"]]) + segment.pack())
        if self.state == STATE["capturing"] and self.mode == MODE["streaming"] and self.format in PIECES:
            if now_ms is not None:                                 # the taken samples, a short last block
                self._stream_blocks(self.kept(int((now_ms - self.started_ms) * self.rate / 1000)), whole=False)
            rel = self.produced - self.capture_begin
            if self.stream_segs and self.stream_segs[-1][1] + self.stream_segs[-1][2] > rel:
                self.stream_segs[-1][2] = rel - self.stream_segs[-1][1]
            if self.stream_segs and self.serial_done < len(self.stream_segs):
                self.serial_done = len(self.stream_segs) & 0xFFFFFFFF
                self.stream_committed_bytes = self.base + len(self.data)
        elif self.state == STATE["capturing"] and self.mode == MODE["streaming"]:
            # the segment being taken ends here, short: one more finished (serial_done, §3.2), its bytes all kept
            if self.produced - self.capture_begin > self._stream_done_samples():
                self.serial_done = (self.serial_done + 1) & 0xFFFFFFFF
                self.stream_committed_bytes = self.base + len(self.data)
        was = self.state
        self.state = STATE["configured"]
        if was == STATE["paused"]:
            return events                                          # 5 -> 1: no stopped (§3.2's state table)
        return events + [self.stopped_event(STOPPED["host"])]

    def _stream_done_samples(self) -> int:
        """Streaming: the samples (from the retained start) of the segments finished so far."""
        taken, first = self.produced - self.capture_begin, self.first_segment_samples or self.samples
        complete = 0 if taken < first else 1 + (taken - first) // self.samples
        return 0 if not complete else first + (complete - 1) * self.samples

    def overflow(self, error: int = CAP.enum["error"]["storage"]) -> list[bytes]:
        """TEST HOOK: the probe dropped data inside the segment it is taking (§2.2: a capture queue or ring overflow,
        error 2; a DMA / peripheral failure, error 1). That segment is never handed out - no record, no segment event,
        its bytes neither read nor streamed (this bench adds a segment whole, so its bytes were never kept; write_pos
        stays at its start) - and the track stops in error: state 6, status flags bit0, stopped reason 3. -> events."""
        if self.state not in (STATE["waiting"], STATE["capturing"], STATE["paused"]):
            return []
        if self.mode == MODE["streaming"]:                         # its bytes ran on: write_pos back to the start of
            keep = self.stream_committed_bytes         # the segment not kept (§2.2; serials from start)
            if keep < self.base + len(self.data):
                del self.data[max(0, keep - self.base):]
                self.base = min(self.base, keep)                   # bytes already sent past it stay sent
        self.state, self.error = STATE["error"], error
        self.flags |= CAP.enum["status_flag"]["dropped"]
        return [self.stopped_event(STOPPED["error"], error)]

    def segment_bytes(self) -> int:
        """The bytes of one whole segment of this configuration."""
        return self.bytes_of(self.samples)

    def bytes_of(self, samples: int) -> int:
        """The stream bytes of `samples` samples (§1.1 rule 4, §1.2 rule 3)."""
        return samples * self.width if self.analog else -(-samples * self.width // 8)

    def stopped_event(self, reason: int, error: int = 0) -> bytes:
        """kind stopped: reason(u8) error(u8) generation(u32) (§3.4: every event carries the generation it was made
        in)."""
        return bytes([EVENT["stopped"], reason, error]) + struct.pack("<I", self.generation)

    def status(self) -> bytes:
        waiting = CAP.enum["status_flag"]["rearm_waiting"] if self.rearm_waiting and self.state == STATE["capturing"] else 0
        out = struct.pack("<BIQBI", self.state, self.serial_done, self.base + len(self.data), self.flags | waiting,
                          self.generation)
        if self.state == STATE["error"]:
            out += m.tlv(CAP.tlv["status_answer"]["error"], bytes([self.error]))   # why (§3.2)
        return out

    def _forward(self) -> None:
        """data_path 1 (§3.9): the bytes taken so far go out on the external path as they come; write_pos stays the
        bytes given to it (the mark not counted)."""
        if self.path is None:
            return
        end = self.base + len(self.data)
        at = max(self.ext_sent, self.base)
        if at < end:
            self._emit(bytes(self.data[at - self.base:]), at)
        self.ext_sent = end

    def _emit(self, data: bytes, position: int | None) -> None:
        """Bytes onto the external path: TCP as they are (mux 1: in chunks fn(u16) len(u16) data); UDP in packets of
        position(u64) generation(u32) fn(u16) reserved(u16) and up to 1384 bytes. `position` None: the mark (UDP: a
        packet of its own at position 0)."""
        kind, mux = self.path
        sink = self.paths.setdefault(kind, ExternalPath(kind))
        if not sink.connected:
            return
        if kind == CAP.enum["external_kind"]["udp"]:
            at = position or 0
            for k in range(0, max(len(data), 1), UDP_DATA):
                piece = data[k:k + UDP_DATA]
                sink.packets.append(struct.pack("<QIHH", at + (k if position is not None else 0), self.generation,
                                                self.fn, 0) + piece)
            return
        if mux == CAP.enum["external_mux"]["chunks"]:
            for k in range(0, len(data), 0xFFFF):
                piece = data[k:k + 0xFFFF]
                sink.stream += struct.pack("<HH", self.fn, len(piece)) + piece
            return
        sink.stream += data

    def disconnected(self) -> list[bytes]:
        """Its external path has gone (§3.9): a capture running stops in error (state 6, stopped reason 2)."""
        if self.state not in (STATE["waiting"], STATE["capturing"], STATE["paused"]):
            return []
        self.state, self.error = STATE["error"], CAP.enum["error"]["peripheral"]
        return [self.stopped_event(STOPPED["error"], self.error)]

    def check_generation(self, generation: int) -> None:
        if generation != self.generation:
            raise wrong_state()                                    # another start's bytes (§3.2)

    def read(self, generation: int, position: int, most: int, budget: int) -> bytes:
        """position(u64) flags(u8: more, gap) len(u16) data (§3.2): from write_pos on, nothing (gap 0) at write_pos; a
        position re-used or pushed on goes to the next readable one with gap - write_pos with gap when none is left."""
        self.check_generation(generation)
        if self.path is not None:
            raise wrong_state()                                    # its data goes the external path (§3.9)
        flags, end = 0, self.base + len(self.data)
        if position >= end:
            return struct.pack("<QBH", end, 0, 0)
        if position < self.base:
            position, flags = self.base, flags | 0x02              # re-used: from what is kept (gap)
        take = max(0, min(most, self.max_read, budget - 11, end - position))
        data = bytes(self.data[position - self.base:position - self.base + take])
        if position + take < end:
            flags |= 0x01                                          # more
        return struct.pack("<QBH", position, flags, len(data)) + data

    def segment_list(self, first: int, budget: int) -> bytes:
        """more(u8) count(u8) count x record (§3.2; no element length, core §2.3), paged by common §1.3: from `first`
        inclusive, nothing (more 0) from serial_done (the next to finish), the oldest kept for a serial not kept."""
        at = m.serial_page_start([s.serial for s in self.segs], self.serial_done, first)
        rows = [s.pack() for s in self.segs[at:]]
        out = rows[:max(0, (budget - 2) // SEGMENT.size)][:255]
        return bytes([int(len(out) < len(rows)), len(out)]) + b"".join(out)

    def release(self, generation: int, serial: int, now_ms: int) -> None:
        self.check_generation(generation)
        if self.mode != MODE["repeat"]:
            return                                                 # one-shot: the next start clears; streaming: pushed
        if self.state not in (STATE["capturing"], STATE["paused"]):
            return                                                 # the state table's "—" (§3.2): only 3 and 5 free
        keep = [s for s in self.segs if m.serial_diff(s.serial, serial) > 0]   # finished ones at or before serial
        # (core §2.6); self.segs holds finished segments only, so one not finished yet is never freed (§3.2)
        drop_to = keep[0].position if keep else self.base + len(self.data)
        del self.data[:drop_to - self.base]
        self.base = drop_to
        self.segs = keep
        if self.state == STATE["paused"] and len(self.segs) < self.segments_max: # resume only when a slot was freed
            self.state, self.gap_next = STATE["capturing"], True
            self.started_ms = now_ms - Fraction(self.edge_of(self.produced) * 1000) / self.rate
            self.t0_ns = (now_ms * 1_000_000 + self.start_offset_ns
                          - int(Fraction(self.edge_of(self.produced) * 1_000_000_000) / self.rate))

    def unsent(self) -> int:
        """Streaming: the bytes captured and not pushed yet (what a subscription's min_bytes counts, core §10.3)."""
        if self.mode != MODE["streaming"] or self.path is not None:
            return 0
        return max(0, self.base + len(self.data) - self.sent)

    def stream_segment(self, position: int) -> tuple[int, int, int]:
        """Streaming: the segment the byte at `position` is in -> (its position, its first sample counted from the
        retained start, its samples in all)."""
        if self.format in PIECES:                           # as recorded when each began
            at = bisect.bisect_right([r[0] for r in self.stream_segs], position) - 1
            return tuple(self.stream_segs[at]) if at >= 0 else (0, 0, self.first_segment_samples or self.samples)
        first = self.first_segment_samples or self.samples
        first_bytes = self.bytes_of(first)
        if position < first_bytes:
            return 0, 0, first
        k = (position - first_bytes) // self.bytes_of(self.samples)
        return first_bytes + k * self.bytes_of(self.samples), first + k * self.samples, self.samples

    def drop_to(self, position: int) -> None:
        """Streaming: the bytes before `position` not sent are dropped - the next frame's position jumps, status flags
        bit0 (capture §2.1 rule 1)."""
        if position <= self.sent:
            return
        del self.data[:position - self.base]
        self.base = self.sent = position
        self.flags |= CAP.enum["status_flag"]["dropped"]

    def segment_end_after(self, position: int) -> int:
        """Streaming: where the segment holding stream byte `position` ends (a segment boundary at `position`: that
        position itself, the segment beginning there not begun yet)."""
        at, _, total = self.stream_segment(position)
        if self.format in PIECES:
            later = [r[0] for r in self.stream_segs if r[0] > at]
            return position if position == at else later[0] if later else UNKNOWN_END   # its end: when the next begins
        return position if position == at else at + self.bytes_of(total)

    def pushes(self, fn: int, next_seq, budget: int) -> list[bytes]:
        """Streaming: the bytes not pushed yet, as data frames (core §10.2: position(u64) len(u16) data), each within
        one segment and with the TLVs every streaming frame carries (§3.4): generation, and segment_info - the segment's
        position, its samples taken so far, start_ns and start_uncertainty_ns."""
        if self.mode != MODE["streaming"] or self.path is not None:
            return []                                              # data_path 1: no data notifications (§3.9)
        out = []
        end = self.base + len(self.data)
        tail = m.tlv(DATA_GENERATION, struct.pack("<I", self.generation))
        room = budget - 15 - len(tail) - 3 - 24
        while self.sent < end:
            at, first, total = self.stream_segment(self.sent)
            seg_end = at + self.bytes_of(total)
            if self.format in PIECES:                       # up to the next segment's bytes
                seg_end = next((r[0] for r in self.stream_segs if r[0] > at), end)
            n = min(end - self.sent, seg_end - self.sent, room)
            taken = min(total, self.produced - self.capture_begin - first)
            info = struct.pack("<QIQI", at, taken, self.time_of(self.capture_begin + first), self.uncertainty_ns)
            chunk = bytes(self.data[self.sent - self.base:self.sent - self.base + n])
            out.append(bytes([m.ROLE_DATA]) + struct.pack("<HHQH", fn, next_seq(), self.sent, n) + chunk + tail
                       + m.tlv(SEGMENT_INFO, info))
            self.sent += n
        del self.data[:self.sent - self.base]                      # pushed: the probe re-uses it
        self.base = self.sent
        return out


@dataclass
class VirtualGroup:
    """oep.fixture.capture-group (§4): tracks started at once by one request (immediate triggers only)."""
    tracks_allowed: list[int]          # the fns it may bind (describe tracks)
    max_tracks: int                    # its own: the most tracks one bind holds (not declared)
    budgets: list[tuple[int, list[int]]]   # its own: (max channel-samples / s, the fns sharing it) (not declared)
    tracks: list[int] = field(default_factory=list)
    start_ns: int = NO_TIME            # all ones before a start (§4.1)
    generation: int = 0                # the group's: +1 at every group start, as a track's (§4.1); kept across binds
    sourced: bool = False              # describe features bit 0: it has a trigger source (§4.4)
    expr: object | None = None         # the bind's trigger source (virtual_bench_trigger.Expression)
    judge: object | None = None        # while waiting: its Evaluator
    event: tuple[int, int, int] | None = None   # the event: (fn, ns, uncertainty_ns)
    declared: tuple[int, int, bool] | None = None   # its trigger_caps: (types, protocols, has trigger_out)
    inputs: dict = field(default_factory=dict)  # TEST HOOK: trig_in role -> level(ns), the external input (idle high)
    pulses: list = field(default_factory=list)  # trigger_out's pulses: (role, start_ns, width_ns, polarity)
    record_next: int = 0                        # rearm 1: the next event's serial (§4.4)
    again_ns: int | None = None                 # rearm 1: an event happened; the next one is looked for from here

    def bind(self, caps: dict[int, VirtualCapture], fns: list[int], others=(), budget: int = 1 << 16,
             repeated: list[tuple[int, bytes]] = (), roles: dict[int, list[int]] | None = None,
             group_roles: list[int] = ()) -> None:
        """§4.1's refusals, every one checked before anything changes: the same fn twice -> malformed; an fn not in
        tracks -> unsupported (0x00, fn); the start answer past the route's frame -> unsupported (0x00); the same list
        -> nothing changes; a change while capturing -> cause 6; a track in another group (`others`) -> cause 4; a
        track not in state 1/4/6, modes apart or a non-immediate trigger -> cause 6; more than it takes together ->
        cause 2."""
        if len(set(fns)) != len(fns):
            raise Reject(m.MALFORMED)
        if any(fn not in self.tracks_allowed for fn in fns):
            raise Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]) + m.tlv(          # 0x00 + TLV fn (core §4.3)
                reg.CORE.tlv["unsupported_payload"]["fn"], struct.pack("<H", next(fn for fn in fns if fn not in self.tracks_allowed))))
        if 18 + 6 * len(fns) > budget:
            raise Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]))      # its start answer would not fit (§4.1)
        from . import virtual_bench_trigger as vbt
        if repeated and not self.sourced:
            raise Reject(m.UNSUPPORTED, bytes([repeated[0][0]]))   # no trigger source here (§4.4)
        expr = vbt.parse(list(repeated), fns, roles or {fn: [] for fn in fns},
                         {fn: caps[fn].analog for fn in fns if fn in caps}, self.declared, group_roles) if repeated else None
        if fns == self.tracks and expr is None and self.expr is None:
            return                                                 # the same list: the state kept
        if self.state(caps) == STATE["capturing"]:
            raise wrong_state()                                    # not while the group captures
        if not fns:
            self._unbind(caps)
            return
        for fn in fns:
            c = caps[fn]
            if fn not in self.tracks and any(fn in g.tracks for g in others):
                raise Reject(m.UNAVAILABLE, m.tlv(CAUSE, bytes([reg.CORE.enum["unavailable_cause"]["bound_in_group"]]))
                             + m.tlv(FN_TLV, struct.pack("<H", fn)))
            linked = c.trigger is not None and c.trigger[0] == TRIGGER["linked"]
            if c.state not in (STATE["configured"], STATE["done"], STATE["error"]) or (
                    not linked if expr is not None else c.trigger is not None):
                raise wrong_state(fn)                              # with a trigger source every track linked (§4.4)
        if len({caps[fn].mode for fn in fns}) != 1:
            raise wrong_state(fns[0])
        if len(fns) > self.max_tracks:
            raise Reject(m.UNAVAILABLE, LIMIT)                     # the resources to capture together are short
        for most, shared in self.budgets:
            if sum(caps[fn].channels * caps[fn].rate for fn in fns if fn in shared) > most:
                over = next(fn for fn in fns if fn in shared)
                raise Reject(m.UNAVAILABLE, LIMIT + m.tlv(FN_TLV, struct.pack("<H", over)))
        self._unbind(caps)                                         # refused above: nothing changed (§4.1)
        self.expr, self.judge, self.event = expr, None, None
        self.tracks = list(fns)
        for fn in fns:
            caps[fn].group = self

    def _unbind(self, caps: dict[int, VirtualCapture]) -> None:
        for fn in self.tracks:
            if fn in caps:
                caps[fn].group = None
        self.tracks = []

    def release_session(self, caps: dict[int, VirtualCapture]) -> None:
        """The session's lock ended (end, lease expiry, force): its bind goes (capture §4.1: a session's resource,
        core §9); the tracks stay as they are, each on its own."""
        self._unbind(caps)

    def start(self, caps: dict[int, VirtualCapture], now_ms: int, subscribed=lambda fn: True,
              fail: set[int] = frozenset()) -> tuple[list[tuple[int, list[bytes]]], list[bytes], bool]:
        """-> (each track's events, the group's events, whether a track's start failed). Every track checked first
        (state 1/4/6, its subscription for streaming, a generation left), then the group and every track get a new
        generation (the answer names them, §4.1) and start at once, in bind order. A track in `fail` (TEST HOOK) does
        not start: it is state 6, the tracks not started yet stay state 1, the ones started are stopped as by stop, and
        no generation goes back (§4.1)."""
        if not self.tracks:
            raise wrong_state()                                    # nothing bound (§4.1)
        for fn in self.tracks:
            caps[fn].startable(subscribed(fn))                 # cause 6 / 2 alone, as interface_boundaries.json
        if self.generation == LAST_GENERATION:
            raise exhausted()
        self.start_ns = now_ms * 1_000_000
        self.generation += 1
        per = []
        for k, fn in enumerate(self.tracks):
            if fn in fail:
                for later in self.tracks[k:]:                      # their generations are allotted all the same
                    c = caps[later]
                    c._clear()
                    c.generation += 1
                    c.state = STATE["configured"]
                failed = caps[fn]
                failed.state, failed.error = STATE["error"], CAP.enum["error"]["peripheral"]
                per += [(started, caps[started].stop(now_ms)) for started in self.tracks[:k]]
                # state 6 always says stopped reason 3 (§3.2 エラー), and the group in 6 too (§4.1, §4.2)
                per.append((fn, [failed.stopped_event(STOPPED["error"], failed.error)]))
                return per, [self.stopped_event(STOPPED["error"], failed.error)], True
            per.append((fn, caps[fn].start(now_ms, self.start_ns, subscribed=subscribed(fn),
                                           waiting=self.expr is not None)))
        self.event, self.record_next, self.again_ns = None, 0, None
        if self.expr is not None:
            from . import virtual_bench_trigger as vbt
            begin = self.start_ns                                  # arm 1: once every track's pretrigger is there
            if self.expr.arm:
                begin = max([begin] + [caps[fn].time_of(caps[fn].pretrigger) for fn in self.tracks])
            for fn in self.tracks:
                caps[fn].rearm = self.expr.rearm                   # each event its tracks' own segment (§4.4)
            self.judge = vbt.Evaluator(self.expr, caps, begin, self.inputs)
            per += self._judge(caps, now_ms)[0]
        return per, self._finished(caps, False), False

    def _judge(self, caps: dict[int, VirtualCapture], now_ms) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        """While waiting: the trigger source judged up to now; at its event every track cuts its segment there. rearm 1:
        the next event looked for after holdoff_ns, in repeat once every track waits for its next segment (§4.4)."""
        per, own = [], []
        while True:
            if self.judge is None and self.again_ns is not None and self._ready(caps):
                from . import virtual_bench_trigger as vbt
                begin = max([self.again_ns] + [caps[fn].time_of(caps[fn].produced) for fn in self.tracks
                                               if caps[fn].mode == MODE["repeat"]])
                self.judge, self.again_ns = vbt.Evaluator(self.expr, caps, begin, self.inputs), None
            if self.judge is None:
                return per, own
            event = self.judge.run(int(now_ms * 1_000_000))
            if event is None:
                return per, own
            more, mine = self._happened(caps, now_ms, event)
            per, own = per + more, own + mine

    def _ready(self, caps: dict[int, VirtualCapture]) -> bool:
        states = [caps[fn] for fn in self.tracks]
        if any(c.state != STATE["capturing"] for c in states):
            return False                                           # done, paused, stopped: no next event
        return all(c.mode == MODE["streaming"] or c.rearm_waiting for c in states)

    def _happened(self, caps, now_ms, event: tuple[int, int, int]):
        self.event, self.judge = event, None
        fn, ns, unc = event
        per = []
        for t in self.tracks:
            c = caps[t]
            if c.mode == MODE["streaming"] and c.state == STATE["capturing"]:   # a record, the stream not cut (§2.1)
                sample = c.kept(max(0, round(Fraction(ns - c.t0_ns) * c.rate / 1_000_000_000)))
                per.append((t, c._triggered(sample, ns, c.pretrigger)))
            else:
                per.append((t, c.force(now_ms, trigger_ns=ns, pretrigger=c.pretrigger)))
        record = NONE
        if self.expr is not None and self.expr.rearm:
            record, self.record_next = self.record_next, (self.record_next + 1) & 0xFFFFFFFF
            self.again_ns = ns + max(1, self.expr.holdoff_ns)
        if self.expr is not None and self.expr.out is not None:
            from . import virtual_bench_trigger as vbt
            role, width, polarity = self.expr.out
            self.pulses.append((role, ns + vbt.TRIGGER_OUT_LATENCY_NS, width, polarity))
        own = [bytes([GRP.event["triggered"]]) + struct.pack("<HIQII", fn, record, ns, unc, self.generation)]
        return per, own

    def force(self, caps: dict[int, VirtualCapture], now_ms) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        """§4.4 force: waiting, the event happens now (fn 0); otherwise nothing."""
        per, own = self._judge(caps, now_ms)
        if self.judge is None:
            return per, own
        more, mine = self._happened(caps, now_ms, (0, int(now_ms * 1_000_000), 0))
        return per + more, own + mine

    def _finished(self, caps: dict[int, VirtualCapture], was_done: bool) -> list[bytes]:
        """The group's stopped 0 when every track has completed (§4.2)."""
        if not was_done and self.state(caps) == STATE["done"]:
            return [self.stopped_event(STOPPED["complete"])]
        return []

    def tick(self, caps: dict[int, VirtualCapture], now_ms: int) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        was_done = self.state(caps) == STATE["done"]
        per, own = [], []
        while True:                                                # rearm 1: an event, its segments, the next event...
            judged, mine = self._judge(caps, now_ms)
            per += judged + [(fn, caps[fn].tick(now_ms)) for fn in self.tracks]
            own += mine
            if not (self.judge is None and self.again_ns is not None and self._ready(caps)):
                return per, own + self._finished(caps, was_done)

    def stopped_event(self, reason: int, error: int = 0) -> bytes:
        """The group's stopped: reason(u8) error(u8) generation(u32: the group's, §4.2)."""
        return bytes([GRP.event["stopped"], reason, error]) + struct.pack("<I", self.generation)

    def start_answer(self, caps: dict[int, VirtualCapture]) -> bytes:
        """start's answer (§4.1): start_ns(u64) generation(u32: the group's) n(u8), then n x (fn(u16) generation(u32))
        - each bound track once, in bind order."""
        return (struct.pack("<QIB", self.start_ns, self.generation, len(self.tracks))
                + b"".join(struct.pack("<HI", fn, caps[fn].generation) for fn in self.tracks))

    def stop(self, caps: dict[int, VirtualCapture], now_ms: int | None = None) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        ticked = {fn: caps[fn].tick(now_ms) if now_ms is not None else [] for fn in self.tracks}   # the clock first
        was = self.state(caps)
        per = [(fn, ticked[fn] + caps[fn].stop(now_ms)) for fn in self.tracks]
        # the group's stopped when its stop ended an acquisition (state 3: a track in 2, 3 or 5; §4.1, §4.2)
        return per, [self.stopped_event(STOPPED["host"])] if was == STATE["capturing"] else []

    def state(self, caps: dict[int, VirtualCapture]) -> int:
        """The group's state from its tracks' (§4.1): 0 with none bound; 6 if any is 6; else 4 if started and every
        one is 4; else 3 if one is 3 or 5; else 1."""
        states = [caps[fn].state for fn in self.tracks]
        if not states:
            return STATE["unconfigured"]
        if STATE["error"] in states:
            return STATE["error"]
        if self.start_ns != NO_TIME and all(s == STATE["done"] for s in states):
            return STATE["done"]
        if self.judge is not None and any(s == STATE["waiting"] for s in states):
            return STATE["waiting"]                                # the trigger source has not happened (§4.4)
        if any(s in (STATE["waiting"], STATE["capturing"], STATE["paused"]) for s in states):
            return STATE["capturing"]
        return STATE["configured"]

    def status(self, caps: dict[int, VirtualCapture]) -> bytes:
        """state(u8) start_ns(u64) generation(u32) (§4.1), and once the event happened TLV trigger (§4.4)."""
        out = struct.pack("<BQI", self.state(caps), self.start_ns, self.generation)
        if self.event is not None:
            out += m.tlv(GRP.tlv["status_answer"]["trigger"], struct.pack("<HQI", *self.event))
        return out
