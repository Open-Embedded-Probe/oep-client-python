# SPDX-License-Identifier: MIT
"""oep.fixture.logic / oep.fixture.analog / oep.fixture.capture-group in the virtual bench (oep-spec a09fa45
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
"""

from __future__ import annotations

import math
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
            # the value's defined range (§3.3): level 0 / 1, edge 0 / 1 / 2, a threshold an ADC value (b 12 bits); a
            # value left undefined is unsupported with the tag as received (core §2.3)
            top = {TRIGGER["level"]: 1, TRIGGER["edge"]: 2}.get(trigger[0], FULL)
            if trigger[2] > top:
                raise t.refuse(TLV["trigger"])
        if not channels:
            raise wrong_state()                                    # no plan (§3.2)
        role_ids = sorted(planned_roles) if planned_roles is not None else list(range(channels))
        if trigger is not None and trigger[1] not in role_ids:
            raise wrong_state()                                    # a role not in the fn's plan: cause 6 (§3.3)
        if trigger is not None and not self.analog:                # logic channel k: the k-th smallest role (§1.1)
            trigger = (trigger[0], role_ids.index(trigger[1]), trigger[2])
        top = Fraction(self.max_hz, channels) if self.analog else self.max_hz   # an ADC's rate is shared by its channels
        if self.exact:
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
            width = 2 * channels
        else:
            width = min((w for w in self.widths if w >= channels), default=None)
            if width is None:
                raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                                  bytes([reg.CORE.enum["unavailable_cause"]["limit"]])))   # more than a sample holds
            step = 8 // math.gcd(width, 8)                         # samples to a whole byte
            if mode == MODE["streaming"] and step > 1:             # its own split (no samples TLV in mode 3): a
                samples = max(step, samples // step * step)        # piece ends on a byte; repeat's segments take the
                # samples asked and end padded (§1.1 rule 4) - rounding down is for capacity only (§3.3)
        if pre > self.max_pretrigger or (mode != MODE["streaming"] and pre >= samples):
            raise t.refuse(TLV["pretrigger"])                      # past max_pretrigger, or not below samples (§3.3)
        return {"mode": mode, "rate": rate, "samples": samples, "segments": segs, "trigger": trigger,
                "pretrigger": pre, "width": width, "channels": channels, "roles": role_ids, "frontends": chosen}

    def apply(self, s: dict) -> None:
        self.mode, self.rate, self.samples, self.segments_max = s["mode"], s["rate"], s["samples"], s["segments"]
        self.trigger, self.pretrigger, self.width, self.channels = s["trigger"], s["pretrigger"], s["width"], s["channels"]
        self.frontend_of = s["frontends"]
        self.analog_roles = tuple(s["roles"]) if self.analog else ()
        self.state = STATE["configured"]
        self._clear()

    def answer(self, s: dict) -> bytes:
        tlv = m.tlv
        c = s["channels"]
        out = tlv(ANSWER["actual_rate"], struct.pack("<II", s["rate"].numerator, s["rate"].denominator))
        if self.analog:
            out += tlv(ANSWER["layout"], bytes([16, 0, 12, c]) + bytes(s["roles"]))
        else:
            out += tlv(ANSWER["layout"], bytes([s["width"], c]) + bytes(range(c)))
        if s["mode"] != MODE["streaming"]:                         # the rows of §3.3: actual_samples in modes 1 / 2,
            out += tlv(ANSWER["actual_samples"], struct.pack("<I", s["samples"]))
        if s["mode"] == MODE["repeat"]:                            # actual_segments in mode 2 only
            out += tlv(ANSWER["actual_segments"], struct.pack("<I", s["segments"]))
        if self.analog:                                            # scale for every role; skew 0 left out (§3.3)
            for ordinal, k in enumerate(s["roles"]):
                lo, hi = self.frontends[s["frontends"][k]]
                out += tlv(ANSWER["scale"], struct.pack("<Bii", k, 0, (hi - lo) * 1_000_000 // FULL))   # signed (§3.3)
            for ordinal, k in enumerate(s["roles"]):
                skew = self._skew(ordinal, s["rate"], c)
                if skew:
                    out += tlv(ANSWER["skew"], struct.pack("<BI", k, skew))
            if self.diagnostics:                                   # frontend_used / reference: optional diagnostics
                for k in s["roles"]:
                    out += tlv(ANSWER["frontend_used"], bytes([k, s["frontends"][k]]))
                out += tlv(ANSWER["reference"], struct.pack("<BIB", ANA.enum["reference_source"]["internal"], 1100, 0))
        return out

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
        self.trigger_scan, self.trigger_previous = 0, 0
        self.trigger_time_ns = NO_TIME

    def _pack(self, first: int, n: int) -> bytes:
        """The stream of samples first .. first + n - 1 of the track."""
        if self.analog:
            return b"".join(struct.pack(f"<{self.channels}H", *(self.value(k, first + i) for k in self.analog_roles))
                            for i in range(n))
        return pack_samples([sum(self.value(k, first + i) << k for k in range(self.width)) for i in range(n)], self.width)

    def value(self, k: int, i: int) -> int:
        return analog_value(k, i) if self.analog else (i >> k) & 1

    def poll_trigger(self, now_ms: int) -> list[bytes]:
        """Waiting input: inspect only elapsed, previously unobserved samples, retaining the edge predecessor."""
        if self.state != STATE["waiting"] or self.trigger is None:
            return []
        kind, role, value = self.trigger
        skew = self.channel_skew(role)
        last = int((Fraction(now_ms * 1_000_000) - self.t0_ns - skew) * self.rate // 1_000_000_000)
        for i in range(self.trigger_scan, last + 1):
            current, previous = self.value(role, i), self.trigger_previous
            self.trigger_scan, self.trigger_previous = i + 1, current
            matched = (kind == TRIGGER["level"] and current == (value & 1)
                       or i > 0 and kind == TRIGGER["edge"] and current != previous
                       and (value == 2 or (value == 0) == (current == 1))
                       or i > 0 and kind == TRIGGER["cross_up"] and previous < value <= current
                       or i > 0 and kind == TRIGGER["cross_down"] and previous > value >= current)
            if matched:
                when = self.time_of(i) + skew
                sample = max(0, round(Fraction(when - self.t0_ns) * self.rate / 1_000_000_000)) if self.group is not None else i
                return self._triggered(sample, when, self.pretrigger)
        return []

    def channel_skew(self, role: int) -> int:
        """The advertised channel delay follows layout.order, not a dense role number."""
        if not self.analog:
            return 0
        return self._skew(self.analog_roles.index(role), self.rate, self.channels)

    def time_of(self, i: int) -> int:
        """The probe-clock time of the track's sample i."""
        return self.t0_ns + int(i * 1_000_000_000 / self.rate)

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
        events = []
        if waiting:
            self.state = STATE["waiting"]
            return events
        if trigger_ns is not None:
            sample = max(0, round(Fraction(trigger_ns - self.t0_ns) * self.rate / 1_000_000_000))
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
        if self.mode == MODE["streaming"] and not subscribed:
            raise wrong_state(fn)
        if self.generation == LAST_GENERATION:
            raise exhausted(fn)

    def _triggered(self, sample: int, when: int, requested: int) -> list[bytes]:
        """Retain available prehistory; trigger_index is relative to this segment, not acquisition start."""
        self.trigger_time_ns = when
        index = min(requested, sample)
        self.produced = self.capture_begin = sample - index
        if self.mode == MODE["streaming"]:
            self.samples = max(self.samples, requested + 1)  # internal stream segment size, not a configure TLV
        self.first_segment_samples = self.samples - requested + index
        self.first_trigger_index = index
        if self.mode == MODE["streaming"]:
            unit = 1 if self.analog else 8 // math.gcd(self.width, 8)
            self.first_segment_samples = -(-self.first_segment_samples // unit) * unit
        events = [bytes([EVENT["triggered"]]) + struct.pack("<IIQI", 0, index, when, self.generation)]
        self.state = STATE["capturing"]
        return events

    def force(self, now_ms: int, *, trigger_ns: int | None = None,
              pretrigger: int | None = None) -> list[bytes]:
        """Resolve a waiting trigger without changing the generation; keep only available prehistory.

        The trigger opens acquisition; the one-shot segment still waits for its sample times.
        """
        if self.state != STATE["waiting"]:
            return []
        when = trigger_ns if trigger_ns is not None else now_ms * 1_000_000
        sample = max(0, round(Fraction(when - self.t0_ns) * self.rate / 1_000_000_000))
        requested = self.pretrigger if pretrigger is None else pretrigger
        return self._triggered(sample, when, requested)

    def tick(self, now_ms: int) -> list[bytes]:
        """Time passes: repeat's segments, streaming's bytes. -> events."""
        events = self.poll_trigger(now_ms) if self.group is None else []
        if self.state != STATE["capturing"]:
            return events
        if self.mode == MODE["one_shot"]:
            n = self.first_segment_samples or self.samples
            if self.one_shot_due(now_ms) - self.produced >= n:
                seg = self._segment(n, self.first_trigger_index, FLAG["short"] if n < self.samples else 0)
                self.state = STATE["done"]
                events += [bytes([EVENT["segment"]]) + seg.pack(), self.stopped_event(STOPPED["complete"])]
            return events
        due = int((now_ms - self.started_ms) * self.rate / 1000)   # samples the clock has taken since start
        if self.mode == MODE["repeat"]:
            while due - self.produced >= (self.first_segment_samples or self.samples):
                if len(self.segs) >= self.segments_max:            # no free segment: it pauses (state 5, no event; §3.2)
                    self.state = STATE["paused"]
                    break
                n = self.first_segment_samples or self.samples
                seg = self._segment(n, self.first_trigger_index, FLAG["short"] if n < self.samples else 0)
                self.first_segment_samples, self.first_trigger_index = 0, NONE
                events.append(bytes([EVENT["segment"]]) + seg.pack())
                if len(self.segs) >= self.segments_max:
                    self.state = STATE["paused"]
                    break
        elif self.mode == MODE["streaming"]:
            per_byte = 1 if self.analog else 8 // math.gcd(self.width, 8)   # whole bytes only (§2.2)
            n = min((due - self.produced) // per_byte * per_byte, MAX_SAMPLES)
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

    def one_shot_due(self, now_ms: int) -> int:
        """Count samples whose complete frame has elapsed, including sample 0 at its timestamp.

        Analog frames include the last channel's advertised skew. Work in rational time so a
        fractional realized rate cannot produce a sample before its timestamp.
        """
        skew = (self.channels - 1) * int(Fraction(1_000_000_000, 1) / (self.rate * self.channels)) if self.analog else 0
        elapsed = Fraction(now_ms * 1_000_000) - self.t0_ns - skew
        return max(0, int(elapsed * self.rate // 1_000_000_000) + 1)

    def stop(self, now_ms: int | None = None) -> list[bytes]:
        """Stop acquisition, committing the samples already taken in a short segment."""
        events = self.tick(now_ms) if now_ms is not None else []
        if self.state == STATE["error"]:
            self.state = STATE["configured"]
            return events
        if self.state not in (STATE["capturing"], STATE["waiting"], STATE["paused"]):
            return events
        if self.state == STATE["capturing"] and self.mode in (MODE["one_shot"], MODE["repeat"]) and now_ms is not None and len(self.segs) < self.segments_max:
            due = self.one_shot_due(now_ms) if self.mode == MODE["one_shot"] else int((now_ms - self.started_ms) * self.rate / 1000)
            partial = min((self.first_segment_samples or self.samples) - 1, max(0, due - self.produced))
            if partial:
                index = self.first_trigger_index if self.first_trigger_index < partial else NONE
                segment = self._segment(partial, index, flags=FLAG["short"])
                events.append(bytes([EVENT["segment"]]) + segment.pack())
        if self.state == STATE["capturing"] and self.mode == MODE["streaming"]:
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
        out = struct.pack("<BIQBI", self.state, self.serial_done, self.base + len(self.data), self.flags, self.generation)
        if self.state == STATE["error"]:
            out += m.tlv(CAP.tlv["status_answer"]["error"], bytes([self.error]))   # why (§3.2)
        return out

    def check_generation(self, generation: int) -> None:
        if generation != self.generation:
            raise wrong_state()                                    # another start's bytes (§3.2)

    def read(self, generation: int, position: int, most: int, budget: int) -> bytes:
        """position(u64) flags(u8: more, gap) len(u16) data (§3.2): from write_pos on, nothing (gap 0) at write_pos; a
        position re-used or pushed on goes to the next readable one with gap - write_pos with gap when none is left."""
        self.check_generation(generation)
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
            self.started_ms = now_ms - Fraction(self.produced * 1000) / self.rate
            self.t0_ns = now_ms * 1_000_000 + self.start_offset_ns - int(Fraction(self.produced * 1_000_000_000) / self.rate)

    def unsent(self) -> int:
        """Streaming: the bytes captured and not pushed yet (what a subscription's min_bytes counts, core §10.3)."""
        if self.mode != MODE["streaming"]:
            return 0
        return max(0, self.base + len(self.data) - self.sent)

    def stream_segment(self, position: int) -> tuple[int, int, int]:
        """Streaming: the segment the byte at `position` is in -> (its position, its first sample counted from the
        retained start, its samples in all)."""
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
        return position if position == at else at + self.bytes_of(total)

    def pushes(self, fn: int, next_seq, budget: int) -> list[bytes]:
        """Streaming: the bytes not pushed yet, as data frames (core §10.2: position(u64) len(u16) data), each within
        one segment and with the TLVs every streaming frame carries (§3.4): generation, and segment_info - the segment's
        position, its samples taken so far, start_ns and start_uncertainty_ns."""
        if self.mode != MODE["streaming"]:
            return []
        out = []
        end = self.base + len(self.data)
        tail = m.tlv(DATA_GENERATION, struct.pack("<I", self.generation))
        room = budget - 15 - len(tail) - 3 - 24
        while self.sent < end:
            at, first, total = self.stream_segment(self.sent)
            n = min(end - self.sent, at + self.bytes_of(total) - self.sent, room)
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

    def bind(self, caps: dict[int, VirtualCapture], fns: list[int], others=(), budget: int = 1 << 16) -> None:
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
        if fns == self.tracks:
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
            if c.state not in (STATE["configured"], STATE["done"], STATE["error"]) or c.trigger is not None:
                raise wrong_state(fn)
        if len({caps[fn].mode for fn in fns}) != 1:
            raise wrong_state(fns[0])
        if len(fns) > self.max_tracks:
            raise Reject(m.UNAVAILABLE, LIMIT)                     # the resources to capture together are short
        for most, shared in self.budgets:
            if sum(caps[fn].channels * caps[fn].rate for fn in fns if fn in shared) > most:
                over = next(fn for fn in fns if fn in shared)
                raise Reject(m.UNAVAILABLE, LIMIT + m.tlv(FN_TLV, struct.pack("<H", over)))
        self._unbind(caps)                                         # refused above: nothing changed (§4.1)
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
            per.append((fn, caps[fn].start(now_ms, self.start_ns, subscribed=subscribed(fn))))
        return per, self._finished(caps, False), False

    def _finished(self, caps: dict[int, VirtualCapture], was_done: bool) -> list[bytes]:
        """The group's stopped 0 when every track has completed (§4.2)."""
        if not was_done and self.state(caps) == STATE["done"]:
            return [self.stopped_event(STOPPED["complete"])]
        return []

    def tick(self, caps: dict[int, VirtualCapture], now_ms: int) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        was_done = self.state(caps) == STATE["done"]
        per = [(fn, caps[fn].tick(now_ms)) for fn in self.tracks]
        return per, self._finished(caps, was_done)

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
        if any(s in (STATE["waiting"], STATE["capturing"], STATE["paused"]) for s in states):
            return STATE["capturing"]
        return STATE["configured"]

    def status(self, caps: dict[int, VirtualCapture]) -> bytes:
        """state(u8) start_ns(u64) generation(u32) (§4.1)."""
        return struct.pack("<BQI", self.state(caps), self.start_ns, self.generation)
