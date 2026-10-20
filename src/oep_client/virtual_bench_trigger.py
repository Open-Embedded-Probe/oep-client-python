# SPDX-License-Identifier: MIT
"""capture-group §4.4 in the virtual bench: the trigger source - an expression (stages of OR'd terms of AND'd
conditions) over the bound tracks' original samples, judged at the samples of the fastest track a term looks at.

This bench has: up to 4 stages, 4 terms a stage, 4 conditions a term, across tracks; conditions level, edge, cross
up / down, above / below and timer; no external input (trig_in), no trigger_out, no serial; rearm 0 only.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from fractions import Fraction

from . import message as m, registry as reg
from .virtual_bench_capture import CAUSE, FN_TLV, Reject, wrong_state

GRP = reg.FIXTURE_CAPTURE_GROUP
BIND = GRP.tlv["bind"]
COND = GRP.enum["trigger_cond"]
SRC = GRP.enum["cond_src"]
LINKED = reg.FIXTURE_LOGIC.enum["trigger"]["linked"]
MAX_STAGES, MAX_TERMS, MAX_CONDS = 4, 4, 4
TYPES = ("level", "edge", "cross_up", "cross_down", "above", "below", "timer")
INSTANT = {COND[k] for k in ("edge", "cross_up", "cross_down", "pulse", "analog_pulse", "timer", "serial")}
LOGIC_ONLY = {COND["level"], COND["edge"]}
ANALOG_ONLY = {COND[k] for k in ("cross_up", "cross_down", "above", "below")}
ARGS = {COND["level"]: "BB", COND["edge"]: "B", COND["cross_up"]: "II", COND["cross_down"]: "II",
        COND["above"]: "IIB", COND["below"]: "IIB", COND["timer"]: "Q"}
COND_HEAD = struct.Struct("<BBBBHBB")     # stage part term src fn role type


def caps() -> bytes:
    """describe trigger_caps (§4.4): max_stages max_terms max_conds cross_track types(u32) protocols trigger_out_latency."""
    types = sum(1 << COND[k] for k in TYPES)
    return struct.pack("<BBBBIBI", MAX_STAGES, MAX_TERMS, MAX_CONDS, 1, types, 0, 0xFFFFFFFF)


@dataclass
class Cond:
    fn: int
    role: int
    kind: int
    args: tuple
    tag: int
    # what it saw: the previous value, whether the state was false since arming, cross / above's hysteresis
    previous: int | None = None
    false_seen: bool = False
    ready: bool = True
    held: bool = False                    # above / below: its state

    @property
    def instant(self) -> bool:
        return self.kind in INSTANT


@dataclass
class Stage:
    count: int = 1
    within_ns: int = 0
    on_timeout: int = 0
    terms: dict[int, list[Cond]] = field(default_factory=dict)
    aborts: dict[int, list[Cond]] = field(default_factory=dict)


@dataclass
class Expression:
    stages: list[Stage]
    holdoff_ns: int
    arm: int
    rearm: int


def parse(repeated: list[tuple[int, bytes]], fns: list[int], track_roles: dict[int, list[int]],
          analog: dict[int, bool]) -> Expression | None:
    """The bind's expression TLVs (§4.4) -> Expression, None without trigger_source. Refusals: a form or value the
    definition excludes -> malformed; past this bench's declaration -> unsupported with the TLV's tag as received; a
    role not in the track's plan -> unavailable cause 6 with the fn."""
    source = [(t, v) for t, v in repeated if t & 0x7F == BIND["trigger_source"]]
    stage_tlvs = [(t, v) for t, v in repeated if t & 0x7F == BIND["stage"]]
    conds = [(t, v) for t, v in repeated if t & 0x7F == BIND["cond"]]
    for t, v in repeated:
        if t & 0x7F == BIND["trigger_out"]:
            raise Reject(m.UNSUPPORTED, bytes([t]))                # no trig_out role here
    if not source:
        if stage_tlvs or conds:
            raise Reject(m.MALFORMED)                              # stages and conditions of no trigger source
        return None
    tag, v = source[0]
    if len(v) != 11:
        raise Reject(m.MALFORMED)
    n, holdoff, arm, rearm = struct.unpack("<BQBB", v)
    if not n or arm > 1 or rearm > 1:
        raise Reject(m.MALFORMED)
    if n > MAX_STAGES or rearm:
        raise Reject(m.UNSUPPORTED, bytes([tag]))                  # rearm 1: not this bench's
    stages = [Stage() for _ in range(n)]
    seen = set()
    for t, v in stage_tlvs:
        if len(v) != 14:
            raise Reject(m.MALFORMED)
        k, count, within, on_timeout = struct.unpack("<BIQB", v)
        if k >= n or k in seen or not count or on_timeout > 1 or on_timeout == 1 and not within:
            raise Reject(m.MALFORMED)                              # §4.4: on_timeout 1 needs within_ns
        seen.add(k)
        stages[k] = Stage(count, within, on_timeout)
    for t, v in conds:
        if len(v) < COND_HEAD.size:
            raise Reject(m.MALFORMED)
        k, part, term, src, fn, role, kind = COND_HEAD.unpack_from(v)
        fmt = ARGS.get(kind)
        if k >= n or part > 1:
            raise Reject(m.MALFORMED)
        if fmt is None or term >= MAX_TERMS or src != SRC["track"] and kind != COND["timer"]:
            raise Reject(m.UNSUPPORTED, bytes([t]))                # a type, a term or an input it does not have
        if len(v) != COND_HEAD.size + struct.calcsize("<" + fmt):
            raise Reject(m.MALFORMED)
        args = struct.unpack_from("<" + fmt, v, COND_HEAD.size)
        if kind != COND["timer"]:
            if fn not in fns:
                raise Reject(m.UNSUPPORTED, bytes([t]))            # not a bound track
            if role not in track_roles[fn]:
                raise wrong_state(fn)                              # the role is not in that track's plan
            if kind in LOGIC_ONLY and analog[fn] or kind in ANALOG_ONLY and not analog[fn]:
                raise Reject(m.UNSUPPORTED, bytes([t]))            # a logic condition on an analog track or so
        where = stages[k].aborts if part else stages[k].terms
        where.setdefault(term, []).append(Cond(fn, role, kind, args, t))
    for st in stages:
        if not st.terms:
            raise Reject(m.MALFORMED)                              # a stage with no term to hold
        for term in list(st.terms.values()) + list(st.aborts.values()):
            if len(term) > MAX_CONDS:
                raise Reject(m.UNSUPPORTED, bytes([term[-1].tag]))
            if sum(c.instant for c in term) > 1:
                raise Reject(m.UNSUPPORTED, bytes([[c for c in term if c.instant][1].tag]))   # one instant a term
    return Expression(stages, holdoff, arm, rearm)


class Evaluator:
    """Runs an Expression over the tracks' samples as time passes (only elapsed samples are looked at)."""

    def __init__(self, expr: Expression, caps: dict, begin_ns: int):
        self.expr, self.caps = expr, caps
        self.stage, self.entered_ns, self.done, self.prev_or = 0, begin_ns, 0, False
        self.at_ns = begin_ns                                      # judged up to (exclusive of later samples)
        self.event: tuple[int, int, int] | None = None             # (fn, ns, uncertainty_ns)

    def _conds(self):
        for st in self.expr.stages:
            for term in list(st.terms.values()) + list(st.aborts.values()):
                yield from term

    def _grid(self, conds) -> tuple[int | None, Fraction]:
        """The fastest track among the conditions: (its fn, its rate); timer only: no track."""
        tracks = {c.fn for c in conds if c.kind != COND["timer"]}
        if not tracks:
            return None, Fraction(0)
        fn = max(tracks, key=lambda f: self.caps[f].rate)
        return fn, self.caps[fn].rate

    def _sample_at(self, fn: int, role: int, ns: int, back: int = 0) -> int | None:
        """The track's last sample of `role` at or before `ns` (its channel's own time, analog skew included), or the
        one `back` samples before it."""
        cap = self.caps[fn]
        k = cap.analog_roles.index(role) if cap.analog else cap.logic_roles.index(role)
        skew = cap.channel_skew(role) if cap.analog else 0
        i = int((Fraction(ns) - cap.t0_ns - skew) * cap.rate // 1_000_000_000) - back
        return None if i < 0 else cap.value(role if cap.analog else k, i)

    def _truth(self, c: Cond, ns: int, prev_ns: int) -> bool:
        if c.kind == COND["timer"]:
            due = self.entered_ns + c.args[0]
            return prev_ns < due <= ns
        v = self._sample_at(c.fn, c.role, ns)
        if v is None:
            return False
        if c.previous is None:                                     # arming: the last sample before it (§4.4)
            c.previous = self._sample_at(c.fn, c.role, ns, back=1)
        before, c.previous = c.previous, v
        if c.kind == COND["level"]:
            level, when = c.args
            if v != level:
                c.false_seen = True
                return False
            return when or c.false_seen
        if c.kind == COND["edge"]:
            edge = c.args[0]
            return before is not None and v != before and (edge == 2 or (edge == 0) == bool(v))
        if c.kind in (COND["cross_up"], COND["cross_down"]):
            value, hyst = c.args
            up = c.kind == COND["cross_up"]
            if not c.ready and (v < value - hyst if up else v > value + hyst):
                c.ready = True
            if before is not None and c.ready and (before < value <= v if up else before > value >= v):
                c.ready = False
                return True
            return False
        value, hyst, when = c.args                                 # above / below: a state with hysteresis
        up = c.kind == COND["above"]
        if up:
            c.held = v >= value if not c.held else v >= value - hyst
        else:
            c.held = v <= value if not c.held else v <= value + hyst
        if not c.held:
            c.false_seen = True
        return c.held and (when or c.false_seen)

    def run(self, now_ns: int) -> tuple[int, int, int] | None:
        """Judge up to `now_ns`. -> the event (fn, ns, uncertainty_ns) once it happened."""
        while self.event is None:
            st = self.expr.stages[self.stage]
            conds = [c for t in list(st.terms.values()) + list(st.aborts.values()) for c in t]
            fn, rate = self._grid(conds)
            step = Fraction(1_000_000_000) / rate if fn is not None else Fraction(max(1, now_ns - self.at_ns))
            nxt = self._next_ns(fn, step)
            timers = [self.entered_ns + c.args[0] for c in conds if c.kind == COND["timer"]
                      and self.entered_ns + c.args[0] > self.at_ns]
            if timers and (min(timers) < nxt):
                nxt = min(timers)
            if st.within_ns and self.entered_ns + st.within_ns < nxt:
                nxt = self.entered_ns + st.within_ns
            if nxt > now_ns:
                return None
            prev, self.at_ns = self.at_ns, nxt
            self._step(st, conds, prev, nxt, fn, rate)
        return self.event

    def _next_ns(self, fn: int | None, step: Fraction) -> int:
        if fn is None:
            return int(self.at_ns + step)
        cap = self.caps[fn]
        i = int((Fraction(self.at_ns) - cap.t0_ns) * cap.rate // 1_000_000_000) + 1
        return max(self.at_ns + 1, int(cap.t0_ns + Fraction(i * 1_000_000_000) / cap.rate))

    def _step(self, st: Stage, conds, prev: int, ns: int, fn, rate) -> None:
        truth = {id(c): self._truth(c, ns, prev) for c in conds}     # every one each step: they keep what they saw
        held = any(all(truth[id(c)] for c in t) for t in st.terms.values())
        aborted = any(all(truth[id(c)] for c in t) for t in st.aborts.values())
        if aborted:
            self._enter(0, ns)
            return
        if st.within_ns and ns >= self.entered_ns + st.within_ns:
            if st.on_timeout:                                      # "should come" did not: the stage holds
                self._advance(ns, fn, conds, timed_out=True)
            else:
                self._enter(0, ns)
            return
        rising, self.prev_or = held and not self.prev_or, held
        if rising:
            if st.on_timeout:
                self._enter(0, ns)                                 # it came: no event
                return
            self.done += 1
            if self.done >= st.count:
                self._advance(ns, fn, conds)

    def _enter(self, stage: int, ns: int) -> None:
        """A stage entered: a state condition with when 0 counts only after it goes false to true from here (§4.4);
        when 1 counts one already true (the OR's first rising is the entry's)."""
        self.stage, self.entered_ns, self.done, self.prev_or = stage, ns, 0, False
        st = self.expr.stages[stage]
        for term in list(st.terms.values()) + list(st.aborts.values()):
            for c in term:
                c.false_seen = False

    def _advance(self, ns: int, fn, conds, timed_out: bool = False) -> None:
        if self.stage + 1 < len(self.expr.stages):
            self._enter(self.stage + 1, ns)
            return
        # the uncertainty (§4.4): the deciding stage's tracks' period, across tracks the slowest's; a timed-out
        # on_timeout 1 stage that or this bench's timing step (the judging track's period), the larger
        tracks = {c.fn for c in conds if c.kind != COND["timer"]}
        slow = min((self.caps[f].rate for f in tracks), default=None)
        unc = int(-(-Fraction(1_000_000_000) // slow)) if slow else 0
        if timed_out and fn is not None:
            unc = max(unc, int(-(-Fraction(1_000_000_000) // self.caps[fn].rate)))
        self.event = (fn or 0, ns, unc)
