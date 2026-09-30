# SPDX-License-Identifier: MIT
"""oep.fixture.capture in the fake probe (oep-spec docs/oep-if-capture.ja.md, revision 1, logic).

What it captures is known in advance, so a receiver can check it: sample i of a capture (counted from its start, across
segments) is the counter i, and channel k is bit k of it - a square wave of period 2^(k+1) samples. The layout is the
probe's (§1.1): w is the smallest width the describe allows (channels tag 0x44) that holds the channels, pos[k] = k.

Modes: one-shot (the segment is there as soon as start answers), repeat (segments come with the clock at the actual
rate, up to the ring; a full ring stops the capture, release frees it) and streaming (the bytes come with the clock and
go out as data pushes while subscribed). Triggers: level and edge on a channel, with a pretrigger. `slipped` sets flags
bit2 on every segment (a probe whose software pace fell behind).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from fractions import Fraction

from . import message as m, registry as reg

CAP = reg.FIXTURE_CAPTURE
OP = CAP.op
TLV, ANSWER = CAP.tlv["configure"], CAP.tlv["configure_answer"]
MODE, STATE, TRIGGER = CAP.enum["mode"], CAP.enum["state"], CAP.enum["trigger"]
STOPPED, FLAG = CAP.enum["stopped_reason"], CAP.enum["segment_flag"]
EVENT = CAP.event
NONE = 0xFFFFFFFF
MAX_SAMPLES = 1 << 20                  # a fake keeps its captures in memory
SEGMENT = struct.Struct("<IQIQIB")     # serial position samples start_us trigger_index flags (§2)


class Reject(Exception):
    def __init__(self, reason: int, payload: bytes = b""):
        self.reason, self.payload = reason, payload


@dataclass
class Segment:
    serial: int
    position: int
    samples: int
    start_us: int
    trigger_index: int = NONE
    flags: int = 0

    def pack(self) -> bytes:
        return SEGMENT.pack(self.serial, self.position, self.samples, self.start_us, self.trigger_index, self.flags)


@dataclass
class FakeCapture:
    """One capture interface. `now_ms` is the endpoint's clock; `roles()` the channels the plan gives it (role order)."""
    modes: set[int]                    # the modes the describe declares
    widths: set[int]                   # the w the describe allows
    min_hz: int
    max_hz: int
    ring: int = 8                      # segment_ring
    max_read: int = 4096
    slipped: bool = False
    state: int = STATE["unconfigured"]
    mode: int = MODE["one_shot"]
    rate: Fraction = Fraction(0)
    samples: int = 0
    segments_max: int = 0
    trigger: tuple[int, int, int] | None = None
    pretrigger: int = 0
    width: int = 0
    channels: int = 0
    data: bytearray = field(default_factory=bytearray)   # the stream from `base` on
    base: int = 0                                        # the byte position data[0] is at
    segs: list[Segment] = field(default_factory=list)
    serial_done: int = 0
    started_ms: int = 0
    produced: int = 0                  # samples produced since start (repeat / streaming)
    sent: int = 0                      # streaming: the byte position pushed so far
    first_sample: int = 0              # the counter value of the capture's first sample
    gap_next: bool = False             # repeat: the capture stopped for want of a segment; the next one says so

    # ---- configure ------------------------------------------------------------------------------------------------
    def settle(self, got: dict[int, bytes], critical: set[int], channels: int) -> dict:
        """The actual values for a configure / query request (nothing changed)."""
        mode = got[TLV["mode"]][0] if TLV["mode"] in got else MODE["one_shot"]
        if mode not in self.modes:
            raise Reject(m.UNSUPPORTED, bytes([TLV["mode"] | (m.TAG_CRITICAL if TLV["mode"] in critical else 0)]))
        if TLV["rate"] not in got or len(got[TLV["rate"]]) != 4:
            raise Reject(m.MALFORMED)
        asked = struct.unpack("<I", got[TLV["rate"]])[0]
        if not asked:
            raise Reject(m.MALFORMED)
        # the source clock divided by a whole number: the nearest rate at or under the one asked, inside the range
        div = max(1, -(-self.max_hz // min(max(asked, self.min_hz), self.max_hz)))
        rate = Fraction(self.max_hz, div)
        samples = struct.unpack("<I", got[TLV["samples"]])[0] if TLV["samples"] in got else 4096
        samples = max(1, min(samples, MAX_SAMPLES))
        segs = struct.unpack("<I", got[TLV["segments"]])[0] if TLV["segments"] in got else self.ring
        segs = max(1, min(segs, self.ring)) if mode == MODE["repeat"] else 1
        trigger = None
        if TLV["trigger"] in got:
            kind, role, value = struct.unpack("<BBH", got[TLV["trigger"]])
            if kind not in (TRIGGER["immediate"], TRIGGER["level"], TRIGGER["edge"]) or (kind and role >= channels):
                raise Reject(m.UNSUPPORTED, bytes([TLV["trigger"] | m.TAG_CRITICAL]))
            trigger = (kind, role, value) if kind else None
        pre = struct.unpack("<I", got[TLV["pretrigger"]])[0] if TLV["pretrigger"] in got else 0
        width = min((w for w in self.widths if w >= max(channels, 1)), default=None)
        if width is None or not channels:
            raise Reject(m.UNAVAILABLE)                            # no channels planned, or more than a sample holds
        if mode != MODE["one_shot"] and width < 8:
            samples = -(-samples // (8 // width)) * (8 // width)   # segments end on a byte
        return {"mode": mode, "rate": rate, "samples": samples, "segments": segs, "trigger": trigger,
                "pretrigger": min(pre, samples - 1), "width": width, "channels": channels}

    def apply(self, s: dict) -> None:
        self.mode, self.rate, self.samples, self.segments_max = s["mode"], s["rate"], s["samples"], s["segments"]
        self.trigger, self.pretrigger, self.width, self.channels = s["trigger"], s["pretrigger"], s["width"], s["channels"]
        self.state = STATE["configured"]
        self._clear()

    @staticmethod
    def answer(s: dict) -> bytes:
        def tlv(tag: int, v: bytes) -> bytes:
            return bytes([tag, len(v)]) + v
        return (tlv(ANSWER["actual_rate"], struct.pack("<II", s["rate"].numerator, s["rate"].denominator))
                + tlv(ANSWER["layout"], bytes([s["width"], s["channels"]]) + bytes(range(s["channels"])))
                + tlv(ANSWER["actual_samples"], struct.pack("<I", s["samples"]))
                + tlv(ANSWER["actual_segments"], struct.pack("<I", s["segments"]))
                + tlv(ANSWER["timing"], struct.pack("<BI", 1 if s["rate"].denominator != 1 else 0, 0))
                + tlv(ANSWER["blocking_ms"], struct.pack("<I", 0)))

    # ---- the data ---------------------------------------------------------------------------------------------------
    def _clear(self) -> None:
        self.data, self.base, self.segs, self.serial_done = bytearray(), 0, [], 0
        self.produced, self.sent, self.first_sample, self.gap_next = 0, 0, 0, False

    def _pack(self, first: int, n: int) -> bytes:
        """Samples first .. first+n-1 of the counter in the layout (w bits each, little endian, low samples first)."""
        w = self.width
        if w >= 8:
            per = w // 8
            mask = (1 << w) - 1
            return b"".join(((first + i) & mask).to_bytes(per, "little") for i in range(n))
        out = bytearray((n * w + 7) // 8)
        mask = (1 << w) - 1
        for i in range(n):
            bit = i * w
            out[bit >> 3] |= ((first + i) & mask) << (bit & 7)
        return bytes(out)

    def _trigger_at(self) -> int:
        """The first sample, at or after the pretrigger, where the trigger holds (the counter's bit `role`)."""
        kind, role, value = self.trigger
        period = 1 << (role + 1)
        i = self.pretrigger
        while True:                                                # within two periods of the channel
            now, before = (i >> role) & 1, ((i - 1) >> role) & 1 if i else None
            if kind == TRIGGER["level"] and now == (value & 1):
                return i
            if kind == TRIGGER["edge"] and before is not None and now != before:
                if value == 2 or (value == 0 and now == 1) or (value == 1 and now == 0):
                    return i
            i += 1
            if i > self.pretrigger + 2 * period + 1:
                return self.pretrigger

    def _segment(self, n: int, now_ms: int, trigger_index: int = NONE, flags: int = 0) -> Segment:
        if self.gap_next:
            flags, self.gap_next = flags | FLAG["gap"], False
        seg = Segment(self.serial_done, self.base + len(self.data), n, now_ms * 1000, trigger_index,
                      flags | (FLAG["slipped"] if self.slipped else 0))
        self.data += self._pack(self.first_sample + self.produced, n)
        self.produced += n
        self.segs.append(seg)
        self.serial_done += 1
        return seg

    # ---- the operations ---------------------------------------------------------------------------------------------
    def start(self, now_ms: int) -> list[bytes]:
        """-> the events it raises (kind(u8) payload), for the endpoint to send when subscribed."""
        if self.state == STATE["unconfigured"]:
            raise Reject(m.UNAVAILABLE)
        self._clear()
        self.started_ms = now_ms
        events = []
        if self.mode == MODE["one_shot"]:
            index = self._trigger_at() if self.trigger else NONE
            if self.trigger:
                events.append(bytes([EVENT["triggered"]]) + struct.pack("<II", 0, index))
            seg = self._segment(self.samples, now_ms, index)
            self.state = STATE["done"]
            events += [bytes([EVENT["segment"]]) + seg.pack(), bytes([EVENT["stopped"], STOPPED["complete"]])]
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
                if len(self.segs) >= self.segments_max:            # no free segment: the capture stops
                    self.state = STATE["paused"]
                    events.append(bytes([EVENT["stopped"], STOPPED["no_free_segment"]]))
                    break
                seg = self._segment(self.samples, now_ms)
                events.append(bytes([EVENT["segment"]]) + seg.pack())
        elif self.mode == MODE["streaming"]:
            per_byte = 8 // self.width if self.width < 8 else 1       # whole bytes only
            n = (due - self.produced) // per_byte * per_byte
            if n > 0:
                self.data += self._pack(self.first_sample + self.produced, min(n, MAX_SAMPLES))
                self.produced += min(n, MAX_SAMPLES)
        return events

    def stop(self) -> list[bytes]:
        if self.state not in (STATE["capturing"], STATE["waiting"], STATE["paused"]):
            return []
        self.state = STATE["done"]
        return [bytes([EVENT["stopped"], STOPPED["host"]])]

    def status(self) -> bytes:
        return struct.pack("<BIQB", self.state, self.serial_done, self.base + len(self.data), 0)

    def read(self, position: int, most: int, budget: int) -> bytes:
        flags = 0
        if position < self.base:
            position, flags = self.base, flags | 0x02              # re-used: from what is kept (gap)
        end = self.base + len(self.data)
        take = max(0, min(most, self.max_read, budget - 9, end - position))
        data = bytes(self.data[position - self.base:position - self.base + take])
        if position + take < end:
            flags |= 0x01                                          # more
        return struct.pack("<QB", position, flags) + data

    def segment_list(self, first: int, budget: int) -> bytes:
        out = [s.pack() for s in self.segs if s.serial >= first]
        out = out[:max(0, (budget - 1) // SEGMENT.size)][:255]
        return bytes([len(out)]) + b"".join(out)

    def release(self, serial: int, now_ms: int) -> None:
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
        """Streaming: the bytes not pushed yet, as data frames (position(u64) then data, common §1.5)."""
        if self.mode != MODE["streaming"]:
            return []
        out = []
        end = self.base + len(self.data)
        while self.sent < end:
            n = min(end - self.sent, budget - 13)
            chunk = bytes(self.data[self.sent - self.base:self.sent - self.base + n])
            out.append(bytes([m.ROLE_DATA]) + struct.pack("<HHQ", fn, next_seq(), self.sent) + chunk)
            self.sent += n
        del self.data[:self.sent - self.base]                      # pushed: the probe re-uses it
        self.base = self.sent
        return out
