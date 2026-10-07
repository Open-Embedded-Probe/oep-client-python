# SPDX-License-Identifier: MIT
"""oep.fixture.logic / oep.fixture.analog / oep.fixture.capture-group in the virtual bench (oep-spec
docs/oep-if-capture.ja.md, revision 1).

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

Modes: one-shot (the segment is there as soon as start answers), repeat (segments come with the clock at the actual
rate, up to the ring; a full ring pauses the capture (state 5), release lets it go on) and streaming (the bytes come
with the clock and go out as data pushes while subscribed). Triggers: logic level / edge, analog cross up / down, with a
pretrigger. `slipped` sets flags bit2 on every segment (a probe whose software pace fell behind).

Every start is a new generation (u32: 1 at the first start, 0xFFFFFFFF followed by 1; §3.4): read and release must
name it (another one is rejected unavailable, cause 6), segment records, events and streaming data frames carry it. A
capture-group has a generation of its own, by the same rules (§4.1), in its start answer, status and events. Segment
serials wrap (core §2.6); segments pages by common §1.3.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from fractions import Fraction

from . import message as m, multirate as mr, registry as reg

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
WRONG_STATE = m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"], bytes([reg.CORE.enum["unavailable_cause"]["wrong_state"]]))
DATA_GENERATION = CAP.tlv["data"]["generation"]


class Reject(Exception):
    def __init__(self, reason: int, payload: bytes = b""):
        self.reason, self.payload = reason, payload


def wrong_state() -> Reject:
    """rejected unavailable, cause 6 (the state or the generation does not fit)."""
    return Reject(m.UNAVAILABLE, WRONG_STATE)


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
    frontends: dict[int, tuple[int, int, int]] = field(default_factory=dict)   # analog: n -> (min_mv, max_mv, mdb)
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
    frontend_of: dict[int, int] = field(default_factory=dict)   # analog: channel -> frontend
    data: bytearray = field(default_factory=bytearray)          # the stream from `base` on
    base: int = 0                                               # the byte position data[0] is at
    segs: list[Segment] = field(default_factory=list)
    serial_done: int = 0
    started_ms: int = 0
    t0_ns: int = 0                     # the probe-clock time of the track's first sample
    produced: int = 0                  # samples produced since start
    sent: int = 0                      # streaming: the byte position pushed so far
    gap_next: bool = False             # repeat: the capture stopped for want of a segment; the next one says so
    group: object | None = None        # the VirtualGroup that binds it
    vrefint_ns: int = NO_TIME
    generation: int = 0                # +1 at every start (§3.2); 0 before the first
    flags: int = 0                     # status flags (dropped, slipped), reset at start
    error: int = 0                     # state 6's reason (status TLV error, stopped reason 3)
    exact: bool = False                # rate_range's exact: any rate in the range (else max_hz / a whole number)
    multirate_decl: mr.Declared | None = None   # describe's multirate (§5.1), None: not offered
    mr_layout: mr.Layout | None = None # the multirate configuration's block layout (§5.5)
    mr_d1_roles: list[int] = field(default_factory=list)   # the layout's D = 1 channels' roles

    @property
    def analog(self) -> bool:
        return bool(self.frontends)

    @property
    def start_offset_ns(self) -> int:
        return 5000 if self.analog else 0

    @property
    def uncertainty_ns(self) -> int:
        return 2000 if self.analog else 50

    # ---- configure ------------------------------------------------------------------------------------------------
    # the virtual bench's multirate budget (its own, §5.2): the reduced channels' work per base sample against 8 x max_hz
    MR_WEIGHT = {mr.SAMPLE: 1, mr.ANY_ACTIVE: 2, mr.EDGE_LATCH: 5}

    def settle(self, got: dict[int, bytes], t, channels: int, frontends: list[tuple[int, int, int]],
               multirate: list[tuple[int, bytes]] = ()) -> dict:
        """The actual values for a configure / query request (nothing changed). `t`: the request's tail reader
        (endpoint.Take: `fixed`, `refuse`). `frontends`: (role, frontend, the tag as received) asked. `multirate`:
        the multirate TLVs as received (tag, value), §5.2."""
        if self.group is not None:
            raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                              bytes([reg.CORE.enum["unavailable_cause"]["bound_in_group"]])))
        if self.state in (STATE["waiting"], STATE["capturing"]):
            raise wrong_state()                                    # §3.2: not while it captures
        # every TLV's form first (core §2.3: a value of another length is malformed, with or without bit 7), then the
        # contract (§3.3: mode and rate required, samples in modes 1 and 2), then what this probe does not handle:
        # unsupported with the tag as received (core §2.3, capture §3.3)
        for tag, size in ((TLV["mode"], 1), (TLV["rate"], 4), (TLV["samples"], 4), (TLV["segments"], 4),
                          (TLV["trigger"], 6), (TLV["pretrigger"], 4)):
            t.fixed(got, tag, size)
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
        specs = []                                                 # multirate (§5.2): the malformed cases first
        for tag, v in multirate:
            if len(v) != mr.TLV.size:
                raise Reject(m.MALFORMED)
            spec = mr.Multirate.unpack(v)
            if spec.malformed() or any(x.role == spec.role for x, _ in specs):
                raise Reject(m.MALFORMED)                          # d 0, phase >= d, d 1 / param > 1, a role twice
            specs.append((spec, tag))
        for spec, tag in specs:
            if self.multirate_decl.refuses(spec):
                raise Reject(m.UNSUPPORTED, bytes([tag]))          # reserved / undeclared policy, undeclared d
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
        if not self.min_hz <= asked <= self.max_hz:
            raise t.refuse(TLV["rate"])                            # out of the declared range (§3.3)
        if trigger is not None:
            allowed = (TRIGGER["cross_up"], TRIGGER["cross_down"]) if self.analog else (TRIGGER["level"], TRIGGER["edge"])
            if trigger[0] not in allowed:
                raise t.refuse(TLV["trigger"])                     # a type not declared (or undefined)
        if not channels:
            raise wrong_state()                                    # no plan (§3.2)
        if trigger is not None and trigger[1] >= channels:
            raise wrong_state()                                    # a role not in the fn's plan: cause 6 (§3.3)
        if any(spec.role >= channels for spec, _ in specs):
            raise wrong_state()                                    # a multirate role not in the plan: cause 6 (§5.2)
        reduced = sorted((spec for spec, _ in specs if spec.reduced), key=lambda x: x.role)
        if specs:
            weight = sum(self.MR_WEIGHT[x.policy] for x in reduced)
            kept = self.max_hz * 8 // max(8, weight)               # the highest base rate it keeps for this (§5.2)
            if kept < self.min_hz:
                raise Reject(m.UNSUPPORTED, bytes([specs[0][1]]))  # not even at min_hz: the multirate tag (§5.2)
        top = self.max_hz // channels if self.analog else self.max_hz   # an ADC's rate is shared by its channels
        if specs:
            top = min(top, kept)
        if self.exact:
            rate = Fraction(min(max(asked, self.min_hz), top))     # any rate in the range (rate_range exact)
        else:
            div = max(1, -(-self.max_hz // min(max(asked, self.min_hz), top)))
            rate = Fraction(self.max_hz, div)                      # the nearest it can make within the range
        most = self.max_samples.get(mode, MAX_SAMPLES)
        samples = struct.unpack("<I", got[TLV["samples"]])[0] if TLV["samples"] in got else min(4096, most)
        samples = max(1, min(samples, most, MAX_SAMPLES))          # rounded down to its limit; the answer says (§3.3)
        segs = struct.unpack("<I", got[TLV["segments"]])[0] if TLV["segments"] in got else self.ring
        segs = max(1, min(segs, self.ring)) if mode == MODE["repeat"] else 1
        pre = struct.unpack("<I", got[TLV["pretrigger"]])[0] if TLV["pretrigger"] in got else 0
        chosen = {}
        if self.analog:
            chosen = {k: max(self.frontends) for k in range(channels)}   # the widest range unless asked
            for role, fe, tag in frontends:
                if role >= channels or fe not in self.frontends:
                    raise Reject(m.UNSUPPORTED, bytes([tag]))      # the frontend's tag as received (core §2.3)
                chosen[role] = fe
            width = 2 * channels
        else:
            d1 = [r for r in range(channels) if r not in {x.role for x in reduced}]   # D = 1 channels (§5.2)
            width = min((w for w in self.widths if w >= max(1, len(d1))), default=None)
            if width is None:
                raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                                  bytes([reg.CORE.enum["unavailable_cause"]["limit"]])))   # more than a sample holds
            step = 8 // math.gcd(width, 8)                         # samples to a whole byte
            if specs:
                block = mr.choose_block(reduced)
                rounded = -(-samples // block) * block             # up to a multiple of L, down if past the most (§5.2)
                samples = rounded if rounded <= min(most, MAX_SAMPLES) else min(most, MAX_SAMPLES) // block * block
                if not samples:
                    raise t.refuse(TLV["samples"])
            elif mode != MODE["one_shot"] and step > 1:
                samples = -(-samples // step) * step               # segments end on a byte
        if pre > self.max_pretrigger or (mode != MODE["streaming"] and pre >= samples):
            raise t.refuse(TLV["pretrigger"])                      # past max_pretrigger, or not below samples (§3.3)
        layout = None
        if specs:
            layout = mr.Layout(width, list(range(len(d1))), mr.choose_block(reduced), reduced)
        return {"mode": mode, "rate": rate, "samples": samples, "segments": segs, "trigger": trigger,
                "pretrigger": pre, "width": width, "channels": channels, "frontends": chosen,
                "multirate": layout, "d1": d1 if specs else []}

    def apply(self, s: dict) -> None:
        self.mode, self.rate, self.samples, self.segments_max = s["mode"], s["rate"], s["samples"], s["segments"]
        self.trigger, self.pretrigger, self.width, self.channels = s["trigger"], s["pretrigger"], s["width"], s["channels"]
        self.frontend_of = s["frontends"]
        self.mr_layout, self.mr_d1_roles = s["multirate"], s["d1"]
        self.state = STATE["configured"]
        self._clear()

    def answer(self, s: dict) -> bytes:
        tlv = m.tlv
        c = s["channels"]
        out = tlv(ANSWER["actual_rate"], struct.pack("<II", s["rate"].numerator, s["rate"].denominator))
        if self.analog:
            out += tlv(ANSWER["layout"], bytes([16, 0, 12, c]) + bytes(range(c)))
        elif s["multirate"] is not None:                          # the D = 1 channels' layout, then block L (§5.3)
            lay = s["multirate"]
            out += tlv(ANSWER["layout"], bytes([lay.w, len(lay.pos)]) + bytes(lay.pos))
            out += tlv(CAP.tlv["configure_answer"]["block"], struct.pack("<I", lay.L))
        else:
            out += tlv(ANSWER["layout"], bytes([s["width"], c]) + bytes(range(c)))
        if s["mode"] != MODE["streaming"]:                         # the rows of §3.3: actual_samples in modes 1 / 2,
            out += tlv(ANSWER["actual_samples"], struct.pack("<I", s["samples"]))
        if s["mode"] == MODE["repeat"]:                            # actual_segments in mode 2 only
            out += tlv(ANSWER["actual_segments"], struct.pack("<I", s["segments"]))
        out += tlv(ANSWER["blocking_ms"], struct.pack("<I", 0))
        if self.analog:
            step_ns = int(1e9 / (s["rate"] * c))                   # one ADC, the channels in turn
            for k in range(c):
                lo, hi, _ = self.frontends[s["frontends"][k]]
                out += tlv(ANSWER["scale"], struct.pack("<Bii", k, 0, (hi - lo) * 1_000_000 // FULL))   # signed (§3.3)
                out += tlv(ANSWER["skew"], struct.pack("<BI", k, k * step_ns))
                out += tlv(ANSWER["frontend_used"], bytes([k, s["frontends"][k]]))
            out += tlv(ANSWER["reference"], struct.pack("<BIB", ANA.enum["reference_source"]["internal"], 1100, 0))
        return out

    def calibration(self) -> bytes:
        """The analog's calibration (§3.8): a made-up two-point value per frontend, and the reference measured at start."""
        if not self.analog:
            raise Reject(m.UNKNOWN_OPERATION)
        scheme = b"org.example.virtual_bench.two-point"
        out = b""
        for fe in sorted(self.frontends):
            raw = struct.pack("<HH", 150, 3950)                     # raw at the range's 5 % / 95 %
            v = bytes([fe, len(scheme)]) + scheme + struct.pack("<H", len(raw)) + raw   # ... raw_len(u16) raw (§3.8)
            out += m.tlv(CALIBRATION["factory"], v)
        if self.vrefint_ns != NO_TIME:
            v = struct.pack("<IQI", 1365, self.vrefint_ns, 1100)   # raw ns nominal_mv: 1100 mV against 3300 mV full scale
            out += m.tlv(CALIBRATION["vrefint"], v)
        return out

    # ---- the data ---------------------------------------------------------------------------------------------------
    def _clear(self) -> None:
        self.data, self.base, self.segs, self.serial_done = bytearray(), 0, [], 0
        self.produced, self.sent, self.gap_next = 0, 0, False

    def _pack(self, first: int, n: int, segment: bool = True) -> bytes:
        """The stream of samples first .. first + n - 1 of the track. Multirate: a segment's blocks (its grid from its
        base sample 0), or - `segment` False, streaming - the blocks of one segment from the track's start."""
        if self.mr_layout is not None:
            lay, roles = self.mr_layout, self.mr_d1_roles
            if segment:
                return lay.encode(lambda role, i: self.value(role, first + i), n, roles)
            return lay.encode(self.value, first + n, roles, range(first // lay.L, (first + n) // lay.L))
        if self.analog:
            return b"".join(struct.pack(f"<{self.channels}H", *(analog_value(k, first + i) for k in range(self.channels)))
                            for i in range(n))
        return pack_samples([(first + i) & ((1 << self.width) - 1) for i in range(n)], self.width)   # the counter

    def value(self, k: int, i: int) -> int:
        return analog_value(k, i) if self.analog else (i >> k) & 1

    def trigger_at(self) -> int:
        """The first sample, at or after the pretrigger, where the trigger holds (within a few periods)."""
        kind, role, value = self.trigger
        span = (1 << (role + 1)) if not self.analog else 64 * (role // 2 + 1)
        for i in range(max(self.pretrigger, 1), self.pretrigger + 3 * span + 2):
            now, before = self.value(role, i), self.value(role, i - 1)
            if kind == TRIGGER["level"] and now == (value & 1):
                return i
            if kind == TRIGGER["edge"] and now != before and (value == 2 or (value == 0) == (now == 1)):
                return i
            if kind == TRIGGER["cross_up"] and before < value <= now:
                return i
            if kind == TRIGGER["cross_down"] and before > value >= now:
                return i
        return self.pretrigger

    def time_of(self, i: int) -> int:
        """The probe-clock time of the track's sample i."""
        return self.t0_ns + int(i * 1_000_000_000 / self.rate)

    def index_at(self, t_ns: int) -> int:
        """The track's sample nearest the probe-clock time t (NONE when outside the one-shot segment)."""
        i = round((t_ns - self.t0_ns) * self.rate / 1_000_000_000)
        return i if 0 <= i < self.samples else NONE

    def _segment(self, n: int, trigger_index: int = NONE, flags: int = 0) -> Segment:
        if self.gap_next:
            flags, self.gap_next = flags | FLAG["gap"], False
        seg = Segment(self.serial_done, self.base + len(self.data), n, self.time_of(self.produced), self.uncertainty_ns,
                      trigger_index, flags | (FLAG["slipped"] if self.slipped else 0), self.generation)
        if self.slipped:
            self.flags |= CAP.enum["status_flag"]["slipped"]
        self.data += self._pack(self.produced, n)
        self.produced += n
        self.segs.append(seg)
        self.serial_done = (self.serial_done + 1) & 0xFFFFFFFF    # serials wrap (core §2.6, capture §3.2)
        return seg

    # ---- the operations ---------------------------------------------------------------------------------------------
    def start(self, now_ms: int, group_ns: int | None = None, trigger_ns: int | None = None,
              subscribed: bool = True) -> list[bytes]:
        """-> the events it raises (kind(u8) fixed part). In a group: its start and, from the trigger track, the
        trigger's time to mark on this track. A new generation; streaming needs a subscription (§3.2)."""
        if self.state in (STATE["unconfigured"], STATE["waiting"], STATE["capturing"], STATE["paused"]):
            raise wrong_state()
        if self.mode == MODE["streaming"] and not subscribed:
            raise wrong_state()
        self._clear()
        self.generation = m.next_generation(self.generation)     # 0xFFFFFFFF is followed by 1 (§3.4)
        self.flags, self.error = 0, 0
        self.started_ms = now_ms
        self.t0_ns = (group_ns if group_ns is not None else now_ms * 1_000_000) + self.start_offset_ns
        self.vrefint_ns = self.t0_ns if self.analog else NO_TIME
        events = []
        if self.mode == MODE["one_shot"]:
            if trigger_ns is not None:
                index = self.index_at(trigger_ns)
            elif self.trigger:
                index = self.trigger_at()
                events.append(bytes([EVENT["triggered"]]) + struct.pack("<IIQI", 0, index, self.time_of(index),
                                                                         self.generation))
            else:
                index = NONE
            seg = self._segment(self.samples, index)
            self.state = STATE["done"]
            events += [bytes([EVENT["segment"]]) + seg.pack(), self.stopped_event(STOPPED["complete"])]
        else:
            self.state = STATE["capturing"]
        return events

    def tick(self, now_ms: int) -> list[bytes]:
        """Time passes: repeat's segments, streaming's bytes. -> events."""
        if self.state != STATE["capturing"]:
            return []
        due = int((now_ms - self.started_ms) * self.rate / 1000)   # samples the clock has taken since start
        events = []
        if self.mode == MODE["repeat"]:
            while due - self.produced >= self.samples:
                if len(self.segs) >= self.segments_max:            # no free segment: it pauses (state 5, no event; §3.2)
                    self.state = STATE["paused"]
                    break
                seg = self._segment(self.samples)
                events.append(bytes([EVENT["segment"]]) + seg.pack())
        elif self.mode == MODE["streaming"]:
            per_byte = 1 if self.analog else 8 // math.gcd(self.width, 8)   # whole bytes only
            if self.mr_layout is not None:
                per_byte = self.mr_layout.L                        # whole blocks (§5.5)
            n = min((due - self.produced) // per_byte * per_byte, MAX_SAMPLES)
            if n > 0:
                self.data += self._pack(self.produced, n, segment=False)
                self.produced += n
        return events

    def stop(self) -> list[bytes]:
        """-> events. Stopping what is not running is nothing (§3.2)."""
        if self.state not in (STATE["capturing"], STATE["waiting"], STATE["paused"]):
            return []
        self.state = STATE["configured"]
        return [self.stopped_event(STOPPED["host"])]

    def overflow(self, error: int = CAP.enum["error"]["storage"]) -> list[bytes]:
        """TEST HOOK: the probe dropped data inside the segment it is taking (§2.2: a capture queue or ring overflow,
        error 2; a DMA / peripheral failure, error 1). That segment is never handed out - no record, no segment event,
        its bytes neither read nor streamed (this bench adds a segment whole, so its bytes were never kept; write_pos
        stays at its start) - and the track stops in error: state 6, status flags bit0, stopped reason 3. -> events."""
        if self.state not in (STATE["waiting"], STATE["capturing"], STATE["paused"]):
            return []
        self.state, self.error = STATE["error"], error
        self.flags |= CAP.enum["status_flag"]["dropped"]
        return [self.stopped_event(STOPPED["error"], error)]

    def stopped_event(self, reason: int, error: int = 0) -> bytes:
        """kind stopped: reason(u8) error(u8) generation(u32) (§3.4: every event carries the generation it was made
        in)."""
        return bytes([EVENT["stopped"], reason, error]) + struct.pack("<I", self.generation)

    def status(self) -> bytes:
        out = struct.pack("<BIQBI", self.state, self.serial_done, self.base + len(self.data), self.flags, self.generation)
        if self.state == STATE["error"]:
            out += m.tlv(CAP.tlv["status_answer"]["error"], bytes([self.error]))   # why (§3.2)
        return out

    def check_generation(self, generation: int) -> None:
        if generation != self.generation:
            raise wrong_state()                                    # another start's bytes (§3.2)

    def read(self, generation: int, position: int, most: int, budget: int) -> bytes:
        self.check_generation(generation)
        flags = 0
        if position < self.base:
            position, flags = self.base, flags | 0x02              # re-used: from what is kept (gap)
        end = self.base + len(self.data)
        take = max(0, min(most, self.max_read, budget - 13, end - position))
        data = bytes(self.data[position - self.base:position - self.base + take])
        if position + take < end:
            flags |= 0x01                                          # more
        return struct.pack("<QBI", position, flags, len(data)) + data   # position flags len(u32) data (§3.2)

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
        keep = [s for s in self.segs if m.serial_diff(s.serial, serial) > 0]   # finished ones at or before serial
        # (core §2.6); self.segs holds finished segments only, so one not finished yet is never freed (§3.2)
        drop_to = keep[0].position if keep else self.base + len(self.data)
        del self.data[:drop_to - self.base]
        self.base = drop_to
        self.segs = keep
        if self.state == STATE["paused"]:                          # it goes on from now; the gap shows (flags bit0)
            self.state, self.gap_next = STATE["capturing"], True
            self.started_ms = now_ms - int(self.produced * 1000 / self.rate)

    def unsent(self) -> int:
        """Streaming: the bytes captured and not pushed yet (what a subscription's min_bytes counts, core §11.3)."""
        if self.mode != MODE["streaming"]:
            return 0
        return max(0, self.base + len(self.data) - self.sent)

    def pushes(self, fn: int, next_seq, budget: int) -> list[bytes]:
        """Streaming: the bytes not pushed yet, as data frames (core §11.2: position(u64) len(u16) data, then the TLV
        generation every streaming frame carries, §3.4)."""
        if self.mode != MODE["streaming"]:
            return []
        out = []
        end = self.base + len(self.data)
        tail = m.tlv(DATA_GENERATION, struct.pack("<I", self.generation))
        while self.sent < end:
            n = min(end - self.sent, budget - 15 - len(tail))
            chunk = bytes(self.data[self.sent - self.base:self.sent - self.base + n])
            out.append(bytes([m.ROLE_DATA]) + struct.pack("<HHQH", fn, next_seq(), self.sent, n) + chunk + tail)
            self.sent += n
        del self.data[:self.sent - self.base]                      # pushed: the probe re-uses it
        self.base = self.sent
        return out


@dataclass
class VirtualGroup:
    """oep.fixture.capture-group (§4): tracks started together, one of them the trigger."""
    tracks_allowed: list[int]          # the fns it may bind (describe tracks)
    max_tracks: int                    # its own: the most tracks one bind holds (not declared)
    budgets: list[tuple[int, list[int]]]   # its own: (max channel-samples / s, the fns sharing it) (not declared)
    tracks: list[int] = field(default_factory=list)
    trigger_fn: int = 0
    start_ns: int = NO_TIME
    trigger_ns: int = NO_TIME
    generation: int = 0                # the group's: +1 at every group start, as a track's (§4.1); kept across binds

    def bind(self, caps: dict[int, VirtualCapture], fns: list[int], trigger_fn: int) -> None:
        """§4.1's refusals, every one checked before anything changes: the same fn twice -> malformed; an fn not in
        tracks -> unsupported; not configured, modes apart, a trigger off the trigger track -> unavailable cause 6;
        more tracks or more samples than it can take together -> unavailable cause 2."""
        if len(set(fns)) != len(fns):
            raise Reject(m.MALFORMED)
        if any(fn not in self.tracks_allowed for fn in fns):
            raise Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]) + m.tlv(          # 0x00 + TLV fn (core §4.3)
                reg.CORE.tlv["unsupported_payload"]["fn"], struct.pack("<H", next(fn for fn in fns if fn not in self.tracks_allowed))))
        if any(caps[fn].state == STATE["capturing"] for fn in self.tracks):
            raise wrong_state()                                    # the state after the form and the values (core §4.3)
        if not fns:
            self._unbind(caps)
            return
        chosen = [caps.get(fn) for fn in fns]
        if (any(c is None or c.state == STATE["unconfigured"] for c in chosen)
                or len({c.mode for c in chosen}) != 1 or (trigger_fn and trigger_fn not in fns)
                or any(c.trigger for fn, c in zip(fns, chosen) if fn != trigger_fn)):
            raise wrong_state()
        limit = m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"], bytes([reg.CORE.enum["unavailable_cause"]["limit"]]))
        if len(fns) > self.max_tracks:
            raise Reject(m.UNAVAILABLE, limit)                     # the resources to capture together are short
        for most, shared in self.budgets:
            if sum(caps[fn].channels * caps[fn].rate for fn in fns if fn in shared) > most:
                over = next(fn for fn in fns if fn in shared)
                raise Reject(m.UNAVAILABLE, limit + m.tlv(reg.CORE.tlv["unavailable_payload"]["fn"],
                                                          struct.pack("<H", over)))
        self._unbind(caps)                                         # refused above: nothing changed (§4.1)
        self.tracks, self.trigger_fn = list(fns), trigger_fn
        for c in chosen:
            c.group = self

    def _unbind(self, caps: dict[int, VirtualCapture]) -> None:
        for fn in self.tracks:
            if fn in caps:
                caps[fn].group = None
        self.tracks, self.trigger_fn, self.start_ns, self.trigger_ns = [], 0, NO_TIME, NO_TIME

    def release_session(self, caps: dict[int, VirtualCapture]) -> None:
        """The session's lock ended (end, lease expiry, force): its bind goes (capture §4.1: a session's resource,
        core §9); the tracks stay as they are, each on its own."""
        for fn in self.tracks:
            if fn in caps:
                caps[fn].group = None
        self.tracks, self.trigger_fn, self.start_ns, self.trigger_ns = [], 0, NO_TIME, NO_TIME

    def start(self, caps: dict[int, VirtualCapture], now_ms: int,
              subscribed=lambda fn: True) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        """-> (each track's events, the group's events). The group and every track get a new generation (the answer
        names them, §4.1)."""
        if not self.tracks:
            raise wrong_state()                                    # nothing bound (§4.1)
        for fn in self.tracks:                                     # every track checked before any starts (§4.1)
            c = caps[fn]
            if c.state in (STATE["unconfigured"], STATE["waiting"], STATE["capturing"], STATE["paused"]):
                raise Reject(m.UNAVAILABLE, WRONG_STATE + m.tlv(reg.CORE.tlv["unavailable_payload"]["fn"],
                                                                struct.pack("<H", fn)))
            if c.mode == MODE["streaming"] and not subscribed(fn):
                raise Reject(m.UNAVAILABLE, WRONG_STATE + m.tlv(reg.CORE.tlv["unavailable_payload"]["fn"],
                                                                struct.pack("<H", fn)))   # no subscription for it
        self.start_ns, self.trigger_ns = now_ms * 1_000_000, NO_TIME
        self.generation = m.next_generation(self.generation)
        per, own = [], []
        trigger_ns = None
        src = caps.get(self.trigger_fn)
        if src is not None and src.trigger and src.mode == MODE["one_shot"]:
            per.append((self.trigger_fn, src.start(now_ms, self.start_ns, subscribed=subscribed(self.trigger_fn))))
            trigger_ns = self.trigger_ns = src.time_of(src.segs[0].trigger_index)
            own.append(bytes([GRP.event["triggered"]]) + struct.pack("<HQI", self.trigger_fn, trigger_ns, self.generation))
        for fn in self.tracks:
            if fn != self.trigger_fn or trigger_ns is None:
                per.append((fn, caps[fn].start(now_ms, self.start_ns, trigger_ns, subscribed=subscribed(fn))))
        if all(caps[fn].state == STATE["done"] for fn in self.tracks):
            own.append(self.stopped_event(STOPPED["complete"]))
        return per, own

    def stopped_event(self, reason: int, error: int = 0) -> bytes:
        """The group's stopped: reason(u8) error(u8) generation(u32: the group's, §4.2)."""
        return bytes([GRP.event["stopped"], reason, error]) + struct.pack("<I", self.generation)

    def start_answer(self, caps: dict[int, VirtualCapture], blocking_ms: int = 0) -> bytes:
        """start's answer (§4.1): blocking_ms(u32) start_ns(u64) generation(u32: the group's) n(u8), then n x (fn(u16)
        generation(u32)) - each bound track once, in bind order."""
        return (struct.pack("<IQIB", blocking_ms, self.start_ns, self.generation, len(self.tracks))
                + b"".join(struct.pack("<HI", fn, caps[fn].generation) for fn in self.tracks))

    def stop(self, caps: dict[int, VirtualCapture]) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        per = [(fn, caps[fn].stop()) for fn in self.tracks]
        return per, [self.stopped_event(STOPPED["host"])] if any(e for _, e in per) else []

    def state(self, caps: dict[int, VirtualCapture]) -> int:
        """The group's state from its tracks' (§4.1): 0 with none bound; 6 if any is 6; else 4 if started and every
        one is 4; else 2 if trigger_track is set, has not fired, and one is 2 or 3; else 3 if one is 2, 3 or 5; else 1."""
        states = [caps[fn].state for fn in self.tracks]
        if not states:
            return STATE["unconfigured"]
        if STATE["error"] in states:
            return STATE["error"]
        if self.start_ns != NO_TIME and all(s == STATE["done"] for s in states):
            return STATE["done"]
        busy = (STATE["waiting"], STATE["capturing"])
        if self.trigger_fn and self.trigger_ns == NO_TIME and any(s in busy for s in states):
            return STATE["waiting"]
        if any(s in busy + (STATE["paused"],) for s in states):
            return STATE["capturing"]
        return STATE["configured"]

    def status(self, caps: dict[int, VirtualCapture]) -> bytes:
        state = self.state(caps)
        return struct.pack("<BQQHI", state, self.start_ns, self.trigger_ns,
                           self.trigger_fn if self.trigger_ns != NO_TIME else 0, self.generation)   # ... generation (§4.1)
