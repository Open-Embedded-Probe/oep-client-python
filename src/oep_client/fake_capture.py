# SPDX-License-Identifier: MIT
"""oep.fixture.logic / oep.fixture.analog / oep.fixture.capture-group in the fake probe (oep-spec
docs/oep-if-capture.ja.md, revision 1).

What it captures is known in advance, so a receiver can check it (sample i counted from the track's start, across
segments):

- logic: sample i is the counter i, channel k bit k of it - a square wave of period 2^(k+1) samples. The layout is the
  probe's (§1.1): w is the smallest width the describe allows (channels tag 0x44) that holds the channels, pos[k] = k.
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

Every start is a new generation (u32, from 1; §3.2): read and release must name it (another one is rejected
unavailable, cause 6), segment records and streaming data frames carry it.
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
MAX_SAMPLES = 1 << 20                  # a fake keeps its captures in memory
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


def analog_value(k: int, i: int) -> int:
    """Channel k's value at sample i of the fake analog waveform."""
    period = 64 * (k // 2 + 1)
    if k % 2 == 0:
        return FULL if i % period < period // 2 else 0
    return round(2047.5 + 2047 * math.sin(2 * math.pi * i / period))


@dataclass
class FakeCapture:
    """One capture track (logic, or analog when `frontends` is given). Times: the endpoint's clock in ms, the probe's in
    ns."""
    modes: set[int]                    # the modes the describe declares
    widths: set[int]                   # logic: the w the describe allows
    min_hz: int
    max_hz: int
    ring: int = 8                      # segment_ring
    max_read: int = 4096
    frontends: dict[int, tuple[int, int, int]] = field(default_factory=dict)   # analog: n -> (min_mv, max_mv, mdb)
    max_samples: dict[int, int] = field(default_factory=dict)   # mode -> describe's max_samples (one segment)
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
    group: object | None = None        # the FakeGroup that binds it
    vrefint_ns: int = NO_TIME
    generation: int = 0                # +1 at every start (§3.2); 0 before the first
    flags: int = 0                     # status flags (dropped, slipped), reset at start

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
    def settle(self, got: dict[int, bytes], t, channels: int, frontends: list[tuple[int, int]]) -> dict:
        """The actual values for a configure / query request (nothing changed). `t`: the request's tail reader
        (endpoint.Take: `fixed`, `refuse`). `frontends`: (role, frontend) asked."""
        if self.group is not None:
            raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                              bytes([reg.CORE.enum["unavailable_cause"]["bound_in_group"]])))
        if self.state in (STATE["waiting"], STATE["capturing"]):
            raise wrong_state()                                    # §3.2: not while it captures
        # every TLV's form first (core §2.3: shorter -> malformed, longer -> unsupported / ignored), then what this
        # probe cannot handle: sent critical (mode, rate, trigger, pretrigger, frontend always are, §3.3) unsupported
        # with the tag as received, else ignored (t.refuse)
        for tag, size in ((TLV["mode"], 1), (TLV["rate"], 4), (TLV["samples"], 4), (TLV["segments"], 4),
                          (TLV["trigger"], 6), (TLV["pretrigger"], 4)):
            t.fixed(got, tag, size)
        if TLV["rate"] not in got:
            raise Reject(m.MALFORMED)                              # rate is required
        asked = struct.unpack("<I", got[TLV["rate"]])[0]
        if not asked:
            raise Reject(m.MALFORMED)
        if TLV["mode"] in got and got[TLV["mode"]][0] not in self.modes:
            t.refuse(TLV["mode"], got)                             # undefined (a later revision's) or not declared
        mode = got[TLV["mode"]][0] if TLV["mode"] in got else MODE["one_shot"]
        if not self.min_hz <= asked <= self.max_hz:
            t.refuse(TLV["rate"], got)                             # out of the declared range (§3.3)
            raise Reject(m.MALFORMED)                              # ... and without it nothing says the rate
        if TLV["trigger"] in got:
            kind, role, _ = struct.unpack("<BBI", got[TLV["trigger"]])   # type role value(u32)
            allowed = (TRIGGER["cross_up"], TRIGGER["cross_down"]) if self.analog else (TRIGGER["level"], TRIGGER["edge"])
            if (kind and kind not in allowed) or (kind and channels and role >= channels):
                t.refuse(TLV["trigger"], got)                      # a type not declared (or undefined), a role it lacks
        if not channels:
            raise wrong_state()                                    # no plan (§3.2)
        top = self.max_hz // channels if self.analog else self.max_hz   # an ADC's rate is shared by its channels
        div = max(1, -(-self.max_hz // min(max(asked, self.min_hz), top)))
        rate = Fraction(self.max_hz, div)                          # the nearest it can make within the range
        most = self.max_samples.get(mode, MAX_SAMPLES)
        samples = struct.unpack("<I", got[TLV["samples"]])[0] if TLV["samples"] in got else min(4096, most)
        samples = max(1, min(samples, most, MAX_SAMPLES))          # rounded down to its limit; the answer says (§3.3)
        segs = struct.unpack("<I", got[TLV["segments"]])[0] if TLV["segments"] in got else self.ring
        segs = max(1, min(segs, self.ring)) if mode == MODE["repeat"] else 1
        trigger = None
        if TLV["trigger"] in got:
            kind, role, value = struct.unpack("<BBI", got[TLV["trigger"]])
            trigger = (kind, role, value) if kind else None
        pre = struct.unpack("<I", got[TLV["pretrigger"]])[0] if TLV["pretrigger"] in got else 0
        chosen = {}
        if self.analog:
            chosen = {k: max(self.frontends) for k in range(channels)}   # the widest range unless asked
            for role, fe in frontends:
                if role >= channels or fe not in self.frontends:
                    raise Reject(m.UNSUPPORTED, bytes([ANA.tlv["configure"]["frontend"] | m.TAG_CRITICAL]))
                chosen[role] = fe
            width = 2 * channels
        else:
            width = min((w for w in self.widths if w >= channels), default=None)
            if width is None:
                raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                                  bytes([reg.CORE.enum["unavailable_cause"]["limit"]])))   # more than a sample holds
            if mode != MODE["one_shot"] and width < 8:
                samples = -(-samples // (8 // width)) * (8 // width)   # segments end on a byte
        return {"mode": mode, "rate": rate, "samples": samples, "segments": segs, "trigger": trigger,
                "pretrigger": min(pre, samples - 1), "width": width, "channels": channels, "frontends": chosen}

    def apply(self, s: dict) -> None:
        self.mode, self.rate, self.samples, self.segments_max = s["mode"], s["rate"], s["samples"], s["segments"]
        self.trigger, self.pretrigger, self.width, self.channels = s["trigger"], s["pretrigger"], s["width"], s["channels"]
        self.frontend_of = s["frontends"]
        self.state = STATE["configured"]
        self._clear()

    def answer(self, s: dict) -> bytes:
        tlv = m.tlv
        c = s["channels"]
        out = tlv(ANSWER["actual_rate"], struct.pack("<II", s["rate"].numerator, s["rate"].denominator))
        if self.analog:
            out += tlv(ANSWER["layout"], bytes([16, 0, 12, c]) + bytes(range(c)))
        else:
            out += tlv(ANSWER["layout"], bytes([s["width"], c]) + bytes(range(c)))
        out += (tlv(ANSWER["actual_samples"], struct.pack("<I", s["samples"]))
                + tlv(ANSWER["actual_segments"], struct.pack("<I", s["segments"]))
                + tlv(ANSWER["timing"], struct.pack("<BI", 1 if s["rate"].denominator != 1 else 0, 0))
                + tlv(ANSWER["blocking_ms"], struct.pack("<I", 0))
                # the logic's rate is the divider's; the ADC's is off by some per mille, as the P4's (+0.15 %)
                + tlv(ANSWER["rate_accuracy"], struct.pack("<BI", 1, 1500) if self.analog else struct.pack("<BI", 0, 0)))
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
        scheme = b"org.example.fake.two-point"
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

    def _pack(self, first: int, n: int) -> bytes:
        if self.analog:
            return b"".join(struct.pack(f"<{self.channels}H", *(analog_value(k, first + i) for k in range(self.channels)))
                            for i in range(n))
        w = self.width                                             # logic: the counter, w bits a sample
        mask = (1 << w) - 1
        if w >= 8:
            return b"".join(((first + i) & mask).to_bytes(w // 8, "little") for i in range(n))
        out = bytearray((n * w + 7) // 8)
        for i in range(n):
            bit = i * w
            out[bit >> 3] |= ((first + i) & mask) << (bit & 7)
        return bytes(out)

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
        self.serial_done += 1
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
        self.generation = (self.generation + 1) & 0xFFFFFFFF or 1
        self.flags = 0
        self.started_ms = now_ms
        self.t0_ns = (group_ns if group_ns is not None else now_ms * 1_000_000) + self.start_offset_ns
        self.vrefint_ns = self.t0_ns if self.analog else NO_TIME
        events = []
        if self.mode == MODE["one_shot"]:
            if trigger_ns is not None:
                index = self.index_at(trigger_ns)
            elif self.trigger:
                index = self.trigger_at()
                events.append(bytes([EVENT["triggered"]]) + struct.pack("<IIQ", 0, index, self.time_of(index)))
            else:
                index = NONE
            seg = self._segment(self.samples, index)
            self.state = STATE["done"]
            events += [bytes([EVENT["segment"]]) + seg.pack(), bytes([EVENT["stopped"], STOPPED["complete"], 0])]
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
            per_byte = 8 // self.width if not self.analog and self.width < 8 else 1   # whole bytes only
            n = min((due - self.produced) // per_byte * per_byte, MAX_SAMPLES)
            if n > 0:
                self.data += self._pack(self.produced, n)
                self.produced += n
        return events

    def stop(self) -> list[bytes]:
        """-> events. Stopping what is not running is nothing (§3.2)."""
        if self.state not in (STATE["capturing"], STATE["waiting"], STATE["paused"]):
            return []
        self.state = STATE["configured"]
        return [bytes([EVENT["stopped"], STOPPED["host"], 0])]

    def status(self) -> bytes:
        return struct.pack("<BIQBI", self.state, self.serial_done, self.base + len(self.data), self.flags, self.generation)

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
        """more(u8) count(u8) count x record (§3.2; no element length, core §2.3)."""
        rows = [s.pack() for s in self.segs if s.serial >= first]
        out = rows[:max(0, (budget - 2) // SEGMENT.size)][:255]
        return bytes([int(len(out) < len(rows)), len(out)]) + b"".join(out)

    def release(self, generation: int, serial: int, now_ms: int) -> None:
        self.check_generation(generation)
        if self.mode != MODE["repeat"]:
            return                                                 # one-shot: the next start clears; streaming: pushed
        keep = [s for s in self.segs if s.serial > serial]
        drop_to = keep[0].position if keep else self.base + len(self.data)
        del self.data[:drop_to - self.base]
        self.base = drop_to
        self.segs = keep
        if self.state == STATE["paused"]:                          # it goes on from now; the gap shows (flags bit0)
            self.state, self.gap_next = STATE["capturing"], True
            self.started_ms = now_ms - int(self.produced * 1000 / self.rate)

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
class FakeGroup:
    """oep.fixture.capture-group (§4): tracks started together, one of them the trigger."""
    tracks_allowed: list[int]          # the fns it may bind (describe tracks)
    max_tracks: int
    budgets: list[tuple[int, list[int]]]   # (max channel-samples / s, the fns sharing it)
    tracks: list[int] = field(default_factory=list)
    trigger_fn: int = 0
    start_ns: int = NO_TIME
    trigger_ns: int = NO_TIME

    def bind(self, caps: dict[int, FakeCapture], fns: list[int], trigger_fn: int) -> None:
        """§4.1's refusals in core §4.3's order: the same fn twice -> malformed; an fn not in tracks -> unsupported;
        not configured, modes apart, a trigger off the trigger track, over the budget -> unavailable."""
        if len(set(fns)) != len(fns):
            raise Reject(m.MALFORMED)
        if any(fn not in self.tracks_allowed for fn in fns):
            raise Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]) + m.tlv(          # 0x00 + TLV fn (core §4.3)
                reg.CORE.tlv["unsupported_payload"]["fn"], struct.pack("<H", next(fn for fn in fns if fn not in self.tracks_allowed))))
        if any(caps[fn].state == STATE["capturing"] for fn in self.tracks):
            raise wrong_state()                                    # the state after the form and the values (order 7)
        for fn in self.tracks:
            caps[fn].group = None
        self.tracks, self.trigger_fn, self.start_ns, self.trigger_ns = [], 0, NO_TIME, NO_TIME
        if not fns:
            return
        chosen = [caps.get(fn) for fn in fns]
        if (len(fns) > self.max_tracks or any(c is None or c.state == STATE["unconfigured"] for c in chosen)
                or len({c.mode for c in chosen}) != 1 or (trigger_fn and trigger_fn not in fns)
                or any(c.trigger for fn, c in zip(fns, chosen) if fn != trigger_fn)):
            raise wrong_state()
        for most, shared in self.budgets:
            if sum(caps[fn].channels * caps[fn].rate for fn in fns if fn in shared) > most:
                raise Reject(m.UNAVAILABLE, m.tlv(reg.CORE.tlv["unavailable_payload"]["cause"],
                                                  bytes([reg.CORE.enum["unavailable_cause"]["limit"]])))
        self.tracks, self.trigger_fn = list(fns), trigger_fn
        for c in chosen:
            c.group = self

    def release_session(self, caps: dict[int, FakeCapture]) -> None:
        """The session's lock ended (end, lease expiry, force): its bind goes (capture §4.1: a session's resource,
        core §9); the tracks stay as they are, each on its own."""
        for fn in self.tracks:
            if fn in caps:
                caps[fn].group = None
        self.tracks, self.trigger_fn, self.start_ns, self.trigger_ns = [], 0, NO_TIME, NO_TIME

    def start(self, caps: dict[int, FakeCapture], now_ms: int,
              subscribed=lambda fn: True) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        """-> (each track's events, the group's events). Every track gets a new generation (the answer's TLV lists
        them, §4.1)."""
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
        per, own = [], []
        trigger_ns = None
        src = caps.get(self.trigger_fn)
        if src is not None and src.trigger and src.mode == MODE["one_shot"]:
            per.append((self.trigger_fn, src.start(now_ms, self.start_ns, subscribed=subscribed(self.trigger_fn))))
            trigger_ns = self.trigger_ns = src.time_of(src.segs[0].trigger_index)
            own.append(bytes([GRP.event["triggered"]]) + struct.pack("<HQ", self.trigger_fn, trigger_ns))
        for fn in self.tracks:
            if fn != self.trigger_fn or trigger_ns is None:
                per.append((fn, caps[fn].start(now_ms, self.start_ns, trigger_ns, subscribed=subscribed(fn))))
        if all(caps[fn].state == STATE["done"] for fn in self.tracks):
            own.append(bytes([GRP.event["stopped"], STOPPED["complete"], 0]))
        return per, own

    def generations(self, caps: dict[int, FakeCapture]) -> bytes:
        """The start answer's TLV generations: n x (fn(u16) generation(u32))."""
        return m.tlv(GRP.tlv["start_answer"]["generations"],
                     b"".join(struct.pack("<HI", fn, caps[fn].generation) for fn in self.tracks))

    def stop(self, caps: dict[int, FakeCapture]) -> tuple[list[tuple[int, list[bytes]]], list[bytes]]:
        per = [(fn, caps[fn].stop()) for fn in self.tracks]
        return per, [bytes([GRP.event["stopped"], STOPPED["host"], 0])] if any(e for _, e in per) else []

    def state(self, caps: dict[int, FakeCapture]) -> int:
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

    def status(self, caps: dict[int, FakeCapture]) -> bytes:
        state = self.state(caps)
        return struct.pack("<BQQH", state, self.start_ns, self.trigger_ns,
                           self.trigger_fn if self.trigger_ns != NO_TIME else 0)
