# SPDX-License-Identifier: MIT
"""capture-group §4.4 in the virtual bench: the trigger source - an expression (stages of OR'd terms of AND'd
conditions) over the bound tracks' original samples, judged at the samples of the fastest track a term looks at.

This bench has: up to 4 stages, 4 terms a stage, 4 conditions a term, across tracks, holdoff and rearm 1. Its
conditions are level, edge, cross up / down, above / below and timer (caps(full=False), the profiles'), and with
virtual_bench.with_capture_options also pulse, pattern, analog_pulse, serial (UART only: it decodes the logic waves'
8N1 lines) and the external input trig_in (a TEST HOOK level per role: VirtualGroup.inputs, judged at EXTERNAL_HZ) and
trigger_out (each pulse kept in VirtualGroup.pulses).
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
MORE_TYPES = ("pulse", "pattern", "analog_pulse", "serial")
INSTANT = {COND[k] for k in ("edge", "cross_up", "cross_down", "pulse", "analog_pulse", "timer", "serial")}
LOGIC_ONLY = {COND[k] for k in ("level", "edge", "pulse", "pattern", "serial")}
ANALOG_ONLY = {COND[k] for k in ("cross_up", "cross_down", "above", "below", "analog_pulse")}
EXTERNAL = {COND[k] for k in ("level", "edge", "pulse")}   # what an external input takes (§4.4's table)
ARGS = {COND["level"]: "BB", COND["edge"]: "B", COND["cross_up"]: "II", COND["cross_down"]: "II",
        COND["above"]: "IIB", COND["below"]: "IIB", COND["timer"]: "Q", COND["pulse"]: "BBQQ",
        COND["analog_pulse"]: "IIBBQQ"}
SERIAL_HEAD = "BBBBIBBBB"                 # protocol event role2 role3 baud bits parity flags n, then mask / value
PROTOCOL, SERIAL_EVENT = GRP.enum["serial_protocol"], GRP.enum["serial_event"]
UART_EVENTS = {SERIAL_EVENT[k] for k in ("match", "uart_framing", "parity", "uart_break")}
NO_ROLE = 0xFF                            # serial: a role it does not use
COND_HEAD = struct.Struct("<BBBBHBB")     # stage part term src fn role type
EXTERNAL_HZ = 1_000_000                   # its own: how fast it judges an external input
TRIGGER_OUT_LATENCY_NS = 1_000            # its own: the event to trigger_out's edge (declared in trigger_caps)
PIN_ROLE = GRP.enum["pin_role"]
NONE = 0xFFFFFFFF


def caps(full: bool = False) -> bytes:
    """describe trigger_caps (§4.4): max_stages max_terms max_conds cross_track types(u32) protocols trigger_out_latency;
    `full`: the conditions of MORE_TYPES, UART and trigger_out too."""
    types = sum(1 << COND[k] for k in TYPES + (MORE_TYPES if full else ()))
    return struct.pack("<BBBBIBI", MAX_STAGES, MAX_TERMS, MAX_CONDS, 1, types,
                       1 << PROTOCOL["uart"] if full else 0, TRIGGER_OUT_LATENCY_NS if full else 0xFFFFFFFF)


def caps_of(v: bytes) -> tuple[int, int, bool]:
    """A trigger_caps value -> (types, protocols, has trigger_out)."""
    *_, types, protocols, latency = struct.unpack("<BBBBIBI", v)
    return types, protocols, latency != 0xFFFFFFFF


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
    src: int = 1                          # 1 a track, 2 the external input (the group's trig_in role)
    run_ns: int | None = None             # pulse: when the line came to the level (None: not there, or before arming)
    reached: bool = False                 # pulse cond 0: this run already counted
    start_ns: int | None = None           # serial: the start bit's edge of the frame being received
    got: bytes = b""                      # serial: the last bytes received (as many as the match's)

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
    out: tuple[int, int, int] | None = None   # trigger_out: role width_ns polarity


def parse(repeated: list[tuple[int, bytes]], fns: list[int], track_roles: dict[int, list[int]],
          analog: dict[int, bool], declared: tuple[int, int, bool] | None = None,
          group_roles: list[int] = ()) -> Expression | None:
    """The bind's expression TLVs (§4.4) -> Expression, None without trigger_source. Refusals: a form or value the
    definition excludes -> malformed; past this bench's declaration (`declared`: caps_of its trigger_caps) ->
    unsupported with the TLV's tag as received; a role not in the track's plan (or trig_in / trig_out not in the
    group's, `group_roles`) -> unavailable cause 6 (with the fn for a track)."""
    types, protocols, has_out = declared or caps_of(caps())
    source = [(t, v) for t, v in repeated if t & 0x7F == BIND["trigger_source"]]
    stage_tlvs = [(t, v) for t, v in repeated if t & 0x7F == BIND["stage"]]
    conds = [(t, v) for t, v in repeated if t & 0x7F == BIND["cond"]]
    out = None
    for t, v in repeated:
        if t & 0x7F == BIND["trigger_out"]:
            if len(v) != 6:
                raise Reject(m.MALFORMED)
            role, width, polarity = struct.unpack("<BIB", v)
            if polarity > 1:
                raise Reject(m.MALFORMED)
            if not has_out or not width:
                raise Reject(m.UNSUPPORTED, bytes([t]))            # no trigger_out here, or a pulse of no width
            if role != PIN_ROLE["trig_out"] or role not in group_roles:
                raise wrong_state()                                # trig_out not in the group's plan (§4.1)
            out = (role, width, polarity)
    if not source:
        if stage_tlvs or conds or out:
            raise Reject(m.MALFORMED)                              # stages and conditions of no trigger source
        return None
    tag, v = source[0]
    if len(v) != 11:
        raise Reject(m.MALFORMED)
    n, holdoff, arm, rearm = struct.unpack("<BQBB", v)
    if not n or arm > 1 or rearm > 1:
        raise Reject(m.MALFORMED)
    if n > MAX_STAGES:
        raise Reject(m.UNSUPPORTED, bytes([tag]))
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
        if k >= n or part > 1:
            raise Reject(m.MALFORMED)
        if not 0 < kind < 32 or not types >> kind & 1 or term >= MAX_TERMS or src not in SRC.values():
            raise Reject(m.UNSUPPORTED, bytes([t]))                # a type, a term or an input it does not have
        args = _args(kind, v[COND_HEAD.size:], t, protocols)
        if src == SRC["external"]:
            if kind not in EXTERNAL:
                raise Reject(m.UNSUPPORTED, bytes([t]))            # an external input takes level, edge, pulse
            if fn:
                raise Reject(m.MALFORMED)                          # fn 0 for src 2 (§4.4)
            if role != PIN_ROLE["trig_in"] or role not in group_roles:
                raise wrong_state()                                # trig_in not in the group's plan (§4.1)
        elif kind != COND["timer"]:
            if fn not in fns:
                raise Reject(m.UNSUPPORTED, bytes([t]))            # not a bound track
            mine = track_roles[fn]
            if kind == COND["pattern"]:                            # bit k: the fn's role k (role not used)
                mask = int.from_bytes(args[1], "little")
                if any(mask >> r & 1 and r not in mine for r in range(len(args[1]) * 8)):
                    raise wrong_state(fn)
            elif role not in mine or kind == COND["serial"] and any(r != NO_ROLE and r not in mine for r in args[2:4]):
                raise wrong_state(fn)                              # the role is not in that track's plan
            if kind in LOGIC_ONLY and analog[fn] or kind in ANALOG_ONLY and not analog[fn]:
                raise Reject(m.UNSUPPORTED, bytes([t]))            # a logic condition on an analog track or so
        where = stages[k].aborts if part else stages[k].terms
        where.setdefault(term, []).append(Cond(fn, role, kind, args, t, src=src))
    for st in stages:
        if not st.terms:
            raise Reject(m.MALFORMED)                              # a stage with no term to hold
        for term in list(st.terms.values()) + list(st.aborts.values()):
            if len(term) > MAX_CONDS:
                raise Reject(m.UNSUPPORTED, bytes([term[-1].tag]))
            if sum(c.instant for c in term) > 1:
                raise Reject(m.UNSUPPORTED, bytes([[c for c in term if c.instant][1].tag]))   # one instant a term
    return Expression(stages, holdoff, arm, rearm, out)


def _args(kind: int, v: bytes, tag: int, protocols: int) -> tuple:
    """A condition's arguments by its type (§4.4's table); a length or value the definition excludes -> malformed; a
    serial protocol / event it does not declare -> unsupported."""
    if kind == COND["pattern"]:                                    # n mask(n) value(n) when
        if not v or not v[0] or len(v) != 2 + 2 * v[0] or v[-1] > 1:
            raise Reject(m.MALFORMED)
        n = v[0]
        return n, v[1:1 + n], v[1 + n:1 + 2 * n], v[-1]
    if kind == COND["serial"]:
        size = struct.calcsize("<" + SERIAL_HEAD)
        if len(v) < size:
            raise Reject(m.MALFORMED)
        head = struct.unpack_from("<" + SERIAL_HEAD, v)
        protocol, event, role2, role3, baud, bits, parity, flags, n = head
        if len(v) != size + 2 * n or not baud or parity > 2:
            raise Reject(m.MALFORMED)
        if not protocols >> protocol & 1 or protocol != PROTOCOL["uart"] or event not in UART_EVENTS \
                or not 5 <= bits <= 9 or event == SERIAL_EVENT["match"] and not n:
            raise Reject(m.UNSUPPORTED, bytes([tag]))
        return head + (v[size:size + n], v[size + n:])
    fmt = ARGS[kind]
    if len(v) != struct.calcsize("<" + fmt):
        raise Reject(m.MALFORMED)
    args = struct.unpack("<" + fmt, v)
    if kind == COND["pulse"] and (args[0] > 1 or args[1] > 2) or kind == COND["analog_pulse"] and (args[2] > 1 or args[3] > 2):
        raise Reject(m.MALFORMED)
    return args


class Evaluator:
    """Runs an Expression over the tracks' samples as time passes (only elapsed samples are looked at)."""

    def __init__(self, expr: Expression, caps: dict, begin_ns: int, inputs: dict | None = None):
        self.expr, self.caps, self.inputs = expr, caps, inputs if inputs is not None else {}
        self.stage, self.entered_ns, self.done, self.prev_or = 0, begin_ns, 0, False
        self.at_ns = begin_ns                                      # judged up to (exclusive of later samples)
        self.event: tuple[int, int, int] | None = None             # (fn, ns, uncertainty_ns)

    def _conds(self):
        for st in self.expr.stages:
            for term in list(st.terms.values()) + list(st.aborts.values()):
                yield from term

    def _grid(self, conds) -> tuple[int | None, Fraction]:
        """The fastest track among the conditions: (its fn, its rate); the external input judged at EXTERNAL_HZ when
        it is faster (no fn); timer only: no track."""
        tracks = {c.fn for c in conds if c.kind != COND["timer"] and c.src == SRC["track"]}
        external = any(c.src == SRC["external"] for c in conds)
        fn = max(tracks, key=lambda f: self.caps[f].rate) if tracks else None
        if external and (fn is None or self.caps[fn].rate < EXTERNAL_HZ):
            return None, Fraction(EXTERNAL_HZ)
        return (fn, self.caps[fn].rate) if fn is not None else (None, Fraction(0))

    def _sample_at(self, fn: int, role: int, ns: int, back: int = 0) -> int | None:
        """The track's last sample of `role` at or before `ns` (its channel's own time, analog skew included), or the
        one `back` samples before it."""
        cap = self.caps[fn]
        k = cap.analog_roles.index(role) if cap.analog else cap.logic_roles.index(role)
        skew = cap.channel_skew(role) if cap.analog else 0
        i = int((Fraction(ns) - cap.t0_ns - skew) * cap.rate // 1_000_000_000) - back
        return None if i < 0 else cap.value(role if cap.analog else k, i)

    def _input_at(self, role: int, ns: int) -> int:
        """The external input `role` at `ns` (TEST HOOK VirtualGroup.inputs: role -> level(ns); idle high)."""
        wave = self.inputs.get(role)
        return 1 if wave is None else wave(ns)

    def _level_at(self, c: Cond, ns: int, back: int = 0) -> int | None:
        if c.src == SRC["external"]:
            return self._input_at(c.role, int(ns - back * Fraction(1_000_000_000, EXTERNAL_HZ)))
        return self._sample_at(c.fn, c.role, ns, back)

    def _pulse(self, c: Cond, inside: bool, stuck: bool, ns: int, cond: int, lo: int, hi: int) -> bool:
        """pulse / analog_pulse (§4.4): cond 0 the instant the run reaches lo (a line `stuck` there since before arming
        counted from the arming); cond 1 / 2 the run's end when its width is at most hi / within lo .. hi (a run begun
        before arming is not measured)."""
        if inside:
            if c.run_ns is None and not c.reached:
                if stuck and cond != 0:
                    c.reached = True                               # begun before arming: its width is not known
                else:
                    c.run_ns = ns
            if cond == 0 and c.run_ns is not None and not c.reached and ns - c.run_ns >= lo:
                c.reached = True
                return True
            return False
        began, c.run_ns, c.reached = c.run_ns, None, False
        if began is None or cond == 0:
            return False
        width = ns - began
        return width <= hi if cond == 1 else lo <= width <= hi

    def _serial(self, c: Cond, v: int, before: int | None, ns: int) -> bool:
        """serial, UART (§4.4): a frame from its start bit's edge, read at its bits' middles once the stop bit's middle
        has passed; the event's instant there."""
        protocol, event, _, _, baud, bits, parity, flags, n, mask, value = c.args
        idle = 0 if flags & 0x20 else 1                           # bit 5: an inverted line idles low
        if c.start_ns is None:
            if before is not None and before == idle and v != idle:
                c.start_ns = ns                                    # the start bit's edge
            return False
        bit_ns = Fraction(1_000_000_000, baud)
        frame = 1 + bits + (1 if parity else 0)                    # start, data, parity; the stop bit after
        if ns < c.start_ns + (frame + Fraction(1, 2)) * bit_ns:
            return False
        at = [self._level_at(c, int(c.start_ns + (k + Fraction(1, 2)) * bit_ns)) ^ (idle ^ 1) for k in range(frame + 1)]
        c.start_ns = None
        data = at[1:1 + bits]
        if flags & 1:
            data = data[::-1]                                      # bit 0: MSB first
        byte = sum(b << k for k, b in enumerate(data))
        stop = at[-1]
        if event == SERIAL_EVENT["uart_break"]:
            return not any(at[1:])
        if event == SERIAL_EVENT["uart_framing"]:
            return stop == 0 and any(at[1:-1])
        if event == SERIAL_EVENT["parity"]:
            return bool(parity) and (sum(data) + at[1 + bits]) % 2 != (1 if parity == 1 else 0)
        c.got = (c.got + bytes([byte & 0xFF]))[-n:]
        return len(c.got) == n and all(g & k == w & k for g, k, w in zip(c.got, mask, value))

    def _truth(self, c: Cond, ns: int, prev_ns: int) -> bool:
        if c.kind == COND["timer"]:
            due = self.entered_ns + c.args[0]
            return prev_ns < due <= ns
        if c.kind == COND["pattern"]:                              # a state: every masked role at its value's bit
            n, mask, value, when = c.args
            m_, w_ = int.from_bytes(mask, "little"), int.from_bytes(value, "little")
            hit = all(self._sample_at(c.fn, r, ns) == (w_ >> r & 1) for r in range(n * 8) if m_ >> r & 1)
            if not hit:
                c.false_seen = True
                return False
            return bool(when) or c.false_seen
        v = self._level_at(c, ns)
        if v is None:
            return False
        first = c.previous is None
        if first:                                                  # arming: the last sample before it (§4.4)
            c.previous = self._level_at(c, ns, back=1)
        before, c.previous = c.previous, v
        if c.kind == COND["pulse"]:
            level, cond, lo, hi = c.args
            return self._pulse(c, v == level, first and before == level, ns, cond, lo, hi)
        if c.kind == COND["analog_pulse"]:
            value, hyst, side, cond, lo, hi = c.args
            beyond = (lambda x: x >= value) if side else (lambda x: x <= value)
            stuck = first and before is not None and beyond(before)
            c.held = (v >= (value - hyst if c.held or stuck else value)) if side else \
                (v <= (value + hyst if c.held or stuck else value))
            return self._pulse(c, c.held, stuck, ns, cond, lo, hi)
        if c.kind == COND["serial"]:
            return self._serial(c, v, None if first else before, ns)
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
            step = Fraction(1_000_000_000) / rate if rate else Fraction(max(1, now_ns - self.at_ns))
            nxt = self._next_ns(fn, step, bool(rate))
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

    def _next_ns(self, fn: int | None, step: Fraction, ticks: bool) -> int:
        if fn is None:                                             # the external input's ticks, or timers only
            return int((self.at_ns // step + 1) * step) if ticks else int(self.at_ns + step)
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
            for c in term:                                         # instants count only after entering it (§4.4)
                c.false_seen, c.previous, c.run_ns, c.reached, c.start_ns = False, None, None, False, None

    def _advance(self, ns: int, fn, conds, timed_out: bool = False) -> None:
        if self.stage + 1 < len(self.expr.stages):
            self._enter(self.stage + 1, ns)
            return
        # the uncertainty (§4.4): the deciding stage's tracks' period, across tracks the slowest's; a timed-out
        # on_timeout 1 stage that or this bench's timing step (the judging track's period), the larger
        tracks = {c.fn for c in conds if c.kind != COND["timer"] and c.src == SRC["track"]}
        rates = [self.caps[f].rate for f in tracks] + [Fraction(EXTERNAL_HZ)] * any(c.src == SRC["external"] for c in conds)
        slow = min(rates, default=None)
        unc = int(-(-Fraction(1_000_000_000) // slow)) if slow else 0
        if timed_out and fn is not None:
            unc = max(unc, int(-(-Fraction(1_000_000_000) // self.caps[fn].rate)))
        self.event = (fn or 0, ns, unc)
