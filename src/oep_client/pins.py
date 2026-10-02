"""oep pins: find where a target is wired to a probe whose pins the host chooses - its debug pins and its reset line -
from what worked and what misled on 2026-10-02 (ESP32-P4 probe, CH32V003 target; oep-spec host guide 「ピンの探し方
（参考）」 and docs/target-scan-notes.ja.md).

Steps, each optional and each saying what it did:

1. classify every channel oep.fixture.gpio allows (less the power channel, --exclude and pins a live connection
   holds): a short series of reads under the probe's pull-up, both pulls, then its pull-down. Something that changes
   between reads is active (an output the target toggles); the same level under both pulls is driven (push-pull, or
   a pull stronger than the probe's: an idle-high UART line looks like this); 1 under pull-up and 0 under pull-down is
   floating, and pulled-up when both pulls together still read 1 (a weak pull-up such as a reset line's). A weak
   pull-down reads as floating. Reading under a pull never drives a line - but a pull-down on the reset line holds the
   target in reset, so the pull-down series comes last.
2. --power CH: the same reads with the target off first; the channels that read differently follow the target's
   power (an unpowered target sinks the probe's pull-up). Only candidates: idle-high UART lines follow too.
3. reset line, by activity: hold each floating / pulled-up channel low (open drain only) and see whether the active
   channels stop. Only candidates - the attach under reset (5) decides.
4. scan the wire over the floating / pulled-up channels (swio: each; rvswd / swd: pairs, bounded) - never over a
   driven or active channel (a scan drives the line: contention with a push-pull output), the power channel or
   --exclude. Then attach (halt) to what answered: its target_id names the family (targets.FAMILIES); for a family
   whose option bytes say whether the reset line exists (CH32V00x), read them (read-only), then resume.
5. reset line, confirmed: attach under reset through each channel that stopped the activity (or, with nothing
   active, through each candidate) - the real line stops the hart at the reset vector.
6. print a suggested slot; --save writes it (config set + save). Nothing is written otherwise.

Everything is bounded in time (the whole run under a minute) and every plan is released at the end. A plan_apply
replaces the gpio's whole plan, and the probe lets every pin of the old plan go first (oep-core §8) - the power
channel too, for a moment - so with --power each new plan is followed by a clean power cycle.
"""

from __future__ import annotations

import dataclasses
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from . import arm, catalog, config, core, host as h, registry as reg, riscv, targets
from .fixture import Gpio

FLOATING, PULLED_UP, DRIVEN_HIGH, DRIVEN_LOW, ACTIVE = "floating", "pulled-up", "driven-high", "driven-low", "active"
KINDS = (FLOATING, PULLED_UP, DRIVEN_HIGH, DRIVEN_LOW, ACTIVE)
SAFE = (FLOATING, PULLED_UP)          # the only kinds a scan, a hold or an attach may move
GPIO_MODES_TAG = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
ROLE_LINE, ROLE_SWDIO, ROLE_SWCLK, ROLE_RESET = 1, 1, 2, 3
MAX_PAIRS = 600                       # rvswd / swd: the most pairs one run scans
STEPS = ("classify", "hold", "scan", "identify", "reset", "slot")


@dataclass
class Channel:
    channel: int
    kind: str
    pullup: str                       # the reads under each mode, oldest first ("1111...")
    pulldown: str
    both: str = ""                    # both pulls ("" when the probe lacks the mode)
    off: str = ""                     # --power: "pull-up/pull-down" levels with the target off, e.g. "0/0"
    follows_power: bool = False
    active_mode: str = ""             # the mode its changes showed under: pull-up / pull-down / both


@dataclass
class Report:
    wire: str
    power: int | None = None
    excluded: list[int] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    follows_power: list[int] = field(default_factory=list)
    stopped: list[int] = field(default_factory=list)      # holding these low stopped the activity
    scanned: list[int] = field(default_factory=list)
    found: list[tuple[int, int]] = field(default_factory=list)
    target_id: str | None = None
    family: str | None = None
    nrst: str | None = None           # what the option bytes say
    nrst_enabled: bool | None = None
    reset_channel: int | None = None
    reset_dpc: int | None = None
    reset_how: str = ""
    slot: str = ""
    saved: bool = False
    power_cycles: int = 0
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def ranges(chs) -> str:
    """[0, 1, 2, 5, 7, 8] -> '0-2,5,7-8'."""
    chs = sorted(chs)
    out, i = [], 0
    while i < len(chs):
        j = i
        while j + 1 < len(chs) and chs[j + 1] == chs[j] + 1:
            j += 1
        out.append(str(chs[i]) if i == j else f"{chs[i]}-{chs[j]}")
        i = j + 1
    return ",".join(out) or "-"


def role_channels(hst: h.Host, fn: int, role: int) -> list[int]:
    """The channels fn's describe allows for `role` (role_channels, core §7.4)."""
    out = set()
    for tag, v in core.describe(hst, fn):
        if tag & 0x7F == catalog.ROLE_CHANNELS and len(v) >= 3 and v[0] == role:
            out.update(catalog.bitmap_to_channels(struct.unpack_from("<H", v, 1)[0], v[3:]))
    return sorted(out)


def changes(series) -> int:
    return sum(1 for a, b in zip(series, series[1:]) if a != b)


def classify(pu: list[int], pd: list[int], both: list[int] | None) -> tuple[str, str]:
    """-> (kind, the mode its activity showed under, or "")."""
    moved = {"pull-up": changes(pu), "pull-down": changes(pd), "both": changes(both) if both else 0}
    busiest = max(moved, key=moved.get)
    if moved[busiest]:
        return ACTIVE, busiest
    hi, lo = pu[0], pd[0]
    if hi and lo:
        return DRIVEN_HIGH, ""
    if not hi and not lo:
        return DRIVEN_LOW, ""
    if not hi and lo:
        return ACTIVE, ""                 # against both pulls: something drives it the other way - leave it alone
    return (PULLED_UP if both and both[0] else FLOATING), ""


class PinFinder:
    SAMPLES, SAMPLE_GAP_S, SETTLE_S = 16, 0.005, 0.03
    OFF_S, BOOT_S = 0.3, 0.4              # power off long enough to drain, then on long enough for the app to start
    HOLD_SETTLE_S, BACK_S, POLL_S = 0.03, 3.0, 0.001  # a hold's settle, the wait for the activity again, read gap
    BASE_S, WATCH_MIN_S, WATCH_MAX_S = 1.0, 0.3, 1.0   # the running activity measured; a hold watched (3 x its lull)
    RESET_HOLD_MS, RESET_TRIES = 20, 3

    def __init__(self, hst: h.Host, wire: str = "swio", power: int | None = None, exclude=(),
                 say: Callable[[str], None] = print, budget_s: float = 55.0, sleep=time.sleep, clock=time.monotonic,
                 probe: str = "<probe>", save: bool = False, slot: int = 0, name: str | None = None):
        self.hst, self.say, self.sleep, self.clock = hst, say, sleep, clock
        self.power, self.exclude = power, sorted(set(exclude))
        self.started = clock()
        self.deadline = self.started + budget_s
        self.probe, self.save, self.slot_no, self.name = probe, save, slot, name
        self.report = Report(wire=wire, power=power, excluded=self.exclude)
        self.gpio = Gpio(hst)
        self.modes = next((struct.unpack_from("<I", v)[0] for t, v in core.describe(hst, self.gpio.fn)
                           if t & 0x7F == GPIO_MODES_TAG and len(v) >= 4), 0xFF)
        self.has_both = bool(self.modes >> Gpio.INPUT_PULLUP_PULLDOWN & 1)
        self.wire = arm.SwdWire(hst) if wire == "swd" else riscv.Wire(hst, f"oep.wire.{wire}")
        self.riscv = wire != "swd"
        self.kinds: dict[int, Channel] = {}
        self.family: targets.Family | None = None
        self.pins: tuple[int, int] | None = None
        self.planned: list[int] = []   # the gpio's plan now (the power channel aside)
        self.mine: list[int] = []      # connections this run opened and has not closed

    # ---- plumbing ----
    def left(self) -> float:
        return self.deadline - self.clock()

    def note(self, text: str) -> None:
        self.report.notes.append(text)
        self.say("  note: " + text)

    def _power_cycle(self) -> None:
        g = self.gpio
        g.set([(self.power, g.OUTPUT_LOW)])
        self.sleep(self.OFF_S)
        g.set([(self.power, g.OUTPUT_HIGH)])
        self.sleep(self.BOOT_S)
        self.report.power_cycles += 1

    def _plan(self, channels: list[int], cycle: bool = True) -> list[int]:
        """oep.fixture.gpio's whole plan = `channels` (+ the power channel). Channels something else holds (the
        settings' plans, a slot, a disabled channel) are dropped and named. A new plan lets every old pin go first,
        the power channel too: with --power, `cycle` power-cycles cleanly after it. -> the channels planned."""
        channels = [c for c in channels if c != self.power]
        if channels == self.planned:
            return channels
        for _ in range(4):
            want = channels + ([self.power] if self.power is not None else [])
            try:
                if want:
                    core.plan_apply(self.hst, [(self.gpio.fn, ROLE_LINE, c) for c in want])
                else:
                    core.plan_release(self.hst, [self.gpio.fn])
                self.planned = channels
                if self.power is not None and cycle:
                    self._power_cycle()
                return channels
            except core.PinsTaken as e:
                taken = {ch for ch, _ in e.holders}
                self.note("left out, held by the settings: " + "; ".join(f"{ch} ({who})" for ch, who in e.holders))
            except h.Unavailable as e:
                taken = set(e.channels)
                if not taken:
                    raise
                self.note(f"left out, held: {ranges(taken)}")
            if self.power in taken:
                raise SystemExit(f"the power channel {self.power} is held by something else")
            channels = [c for c in channels if c not in taken]
        raise SystemExit("could not plan the gpio channels")

    def _series(self, chans: list[int], mode: int) -> dict[int, list[int]]:
        self.gpio.set([(c, mode) for c in chans])
        self.sleep(self.SETTLE_S)
        out: dict[int, list[int]] = {c: [] for c in chans}
        for _ in range(self.SAMPLES):
            for c, v in zip(chans, self.gpio.read(chans)):
                out[c].append(v)
            self.sleep(self.SAMPLE_GAP_S)
        return out

    def _read(self, chans: list[int], mode: int) -> dict[int, int]:
        self.gpio.set([(c, mode) for c in chans])
        self.sleep(self.SETTLE_S)
        return dict(zip(chans, self.gpio.read(chans)))

    @staticmethod
    def _pins_text(pins: tuple[int, int]) -> str:
        return str(pins[0]) if pins[1] == 0xFFFF else f"{pins[0]},{pins[1]}"

    # ---- 1 and 2: classify, follow the power ----
    def classify(self) -> list[int]:
        allowed = role_channels(self.hst, self.gpio.fn, ROLE_LINE)
        if self.power is not None and self.power not in allowed:
            raise SystemExit(f"--power {self.power}: not a channel oep.fixture.gpio allows ({ranges(allowed)})")
        held = set()
        try:
            for c in self.wire.connections():
                held |= {p for p in c.pins if p != 0xFFFF}
        except h.OepError:
            pass
        if held:
            self.note(f"a live connection holds {ranges(held)}: left out (oep config state)")
        chans = [c for c in allowed if c != self.power and c not in self.exclude and c not in held]
        chans = self._plan(chans, cycle=False)
        g = self.gpio
        off_pu = off_pd = None
        if self.power is not None:
            g.set([(self.power, g.OUTPUT_LOW)] + [(c, g.INPUT_PULLDOWN) for c in chans])
            self.sleep(self.OFF_S)
            off_pd = dict(zip(chans, g.read(chans)))
            off_pu = self._read(chans, g.INPUT_PULLUP)
            g.set([(c, g.INPUT) for c in chans] + [(self.power, g.OUTPUT_HIGH)])
            self.sleep(self.BOOT_S)
            self.report.power_cycles += 1
            self.say(f"power: channel {self.power} low {self.OFF_S * 1000:.0f} ms (target off: read), then high "
                     f"{self.BOOT_S * 1000:.0f} ms before the reads")
        pu = self._series(chans, g.INPUT_PULLUP)
        both = self._series(chans, g.INPUT_PULLUP_PULLDOWN) if self.has_both else None
        pd = self._series(chans, g.INPUT_PULLDOWN)        # last: a pull-down on the reset line resets the target
        g.set([(c, g.INPUT) for c in chans])
        for c in chans:
            kind, mode = classify(pu[c], pd[c], both[c] if both else None)
            ch = Channel(c, kind, "".join(map(str, pu[c])), "".join(map(str, pd[c])),
                         "".join(map(str, both[c])) if both else "", active_mode=mode)
            if off_pu is not None:
                ch.off = f"{off_pu[c]}/{off_pd[c]}"
                ch.follows_power = (off_pu[c], off_pd[c]) != (int(any(pu[c])), int(any(pd[c])))
            self.kinds[c] = ch
        self.report.channels = list(self.kinds.values())
        modes = "pull-up, " + ("both pulls, " if both else "") + "pull-down"
        self.say(f"classify: {len(chans)} channels, {self.SAMPLES} reads each under {modes}")
        for kind in KINDS:
            these = [c for c, k in self.kinds.items() if k.kind == kind]
            if these:
                extra = ""
                if kind == ACTIVE:
                    extra = "  (" + "; ".join(f"{c}: {changes(self._mode_series(c))} changes under "
                                              f"{self.kinds[c].active_mode or 'both pulls'}" for c in these) + ")"
                elif kind in (DRIVEN_HIGH, DRIVEN_LOW):
                    extra = "  (never scanned or held)"
                self.say(f"  {kind:13}{ranges(these)}{extra}")
        if self.power is not None:
            follow = [c for c, k in self.kinds.items() if k.follows_power]
            self.report.follows_power = follow
            self.say(f"  {'follow power':13}{ranges(follow)}  (read otherwise with the target off: wired to it; "
                     f"candidates only)")
        return chans

    def _mode_series(self, c: int) -> str:
        k = self.kinds[c]
        return {"pull-up": k.pullup, "pull-down": k.pulldown, "both": k.both}.get(k.active_mode, k.pullup)

    def candidates(self) -> list[int]:
        """The channels a scan or a hold may move (floating / pulled-up): those that follow the power first."""
        safe = [c for c, k in self.kinds.items() if k.kind in SAFE]
        return sorted(safe, key=lambda c: (not self.kinds[c].follows_power, c))

    def active(self) -> list[int]:
        return [c for c, k in self.kinds.items() if k.kind == ACTIVE and k.active_mode]

    # ---- 3: hold low, watch the activity ----
    def hold_search(self) -> list[int]:
        """Hold each candidate low (open drain) and watch the active channels: -> the channels that stopped them."""
        active = self.active()
        if not active:
            self.say("reset line (hold low): nothing active to watch - the attach under reset alone decides")
            return []
        g = self.gpio
        mode = {"pull-up": g.INPUT_PULLUP, "pull-down": g.INPUT_PULLDOWN, "both": g.INPUT_PULLUP_PULLDOWN}
        cands = self.candidates()
        cands.sort(key=lambda c: (self.kinds[c].kind != PULLED_UP, not self.kinds[c].follows_power, c))
        planned = set(self.planned)                       # the classify plan holds them all: no new plan
        g.set([(a, mode[self.kinds[a].active_mode]) for a in active if a in planned]
              + [(c, g.OPEN_DRAIN_RELEASE) for c in cands if c in planned])

        def moved(seconds: float) -> bool:
            """True at the first change of an active channel within `seconds` (read as fast as the link allows)."""
            t_end = self.clock() + seconds
            last = g.read(active)
            while self.clock() < t_end:
                self.sleep(self.POLL_S)
                now = g.read(active)
                if now != last:
                    return True
            return False

        def quiet_gap(seconds: float) -> tuple[int, float]:
            """-> (changes, the longest stretch without one) over `seconds` of running."""
            t0 = t_last = self.clock()
            last, n, gap = g.read(active), 0, 0.0
            while self.clock() - t0 < seconds:
                self.sleep(self.POLL_S)
                now = g.read(active)
                if now != last:
                    t = self.clock()
                    n, gap, t_last, last = n + 1, max(gap, t - t_last), t, now
            return n, max(gap, self.clock() - t_last)

        def back() -> bool:
            """Wait for the activity to come back (a reset's boot can take a second or more: a CH32V003 with a
            bootloader waits after a pin reset); with --power, power-cycle once if it does not."""
            if moved(self.BACK_S):
                return True
            if self.power is not None:
                self._power_cycle()
                return moved(self.BACK_S / 2)
            return False

        if not back():
            self.say(f"reset line (hold low): {ranges(active)} did not move again - skipped")
            return []
        n, gap = quiet_gap(self.BASE_S)
        if n < 3:
            self.say(f"reset line (hold low): {ranges(active)} too quiet ({n} changes in {self.BASE_S:.1f} s) - skipped")
            return []
        window = min(self.WATCH_MAX_S, max(self.WATCH_MIN_S, 3 * gap))   # longer than any lull of the running target
        self.say(f"reset line (hold low): {len(cands)} candidates, each held low (open drain) up to "
                 f"{window * 1000:.0f} ms while watching {ranges(active)} ({n} changes in {self.BASE_S:.1f} s running, "
                 f"longest lull {gap * 1000:.0f} ms)")
        stopped = []
        t0 = self.clock()
        for c in cands:
            if c not in planned:
                continue
            if self.left() < 15:
                self.note("time is short: the hold-low search stopped early")
                break
            g.set([(c, g.OPEN_DRAIN_LOW)])
            self.sleep(self.HOLD_SETTLE_S)
            still = moved(window)
            g.set([(c, g.OPEN_DRAIN_RELEASE)])
            if still:
                continue
            stopped.append(c)
            came = back()
            self.say(f"  hold {c} low: {ranges(active)} stopped" + ("" if came else "; did not move again"))
            if not came:
                break
        self.say(f"  {len(cands)} held in {self.clock() - t0:.1f} s: " +
                 (f"stopped by {ranges(stopped)}" if stopped else "nothing stopped the activity"))
        self.report.stopped = stopped
        return stopped

    # ---- 4: scan, identify ----
    def scan(self) -> list[riscv.Found]:
        cands = self.candidates()
        self._plan([])                                     # nothing of the gpio's on the pins scanned (power stays)
        name = self.report.wire
        if name == "swio":
            allowed = set(role_channels(self.hst, self.wire.fn, ROLE_SWDIO))
            chans = sorted(c for c in cands if c in allowed)
            pairs = [(c, 0xFFFF) for c in chans]
            self.report.scanned = chans
        else:
            dio = set(role_channels(self.hst, self.wire.fn, ROLE_SWDIO))
            clk = set(role_channels(self.hst, self.wire.fn, ROLE_SWCLK))
            if dio or clk:
                pool = cands
                follow = [c for c in cands if self.kinds[c].follows_power]
                if len(pool) * (len(pool) - 1) > MAX_PAIRS and len(follow) >= 2:
                    pool = follow
                    self.say(f"scan: {len(cands)} candidates make too many pairs: only those that follow the power")
                pairs = [(d, c) for d in pool for c in pool if d != c and d in dio and c in clk]
                if len(pairs) > MAX_PAIRS:
                    self.note(f"{len(pairs)} pairs: only the first {MAX_PAIRS} scanned (narrow with --exclude or "
                              f"--power)")
                    pairs = pairs[:MAX_PAIRS]
            else:                                          # fixed pairs (channel_group): those wholly safe
                pairs = []
                for tag, v in core.describe(self.hst, self.wire.fn):
                    if tag & 0x7F == catalog.CHANNEL_GROUP:
                        roles = dict(catalog.unpack_channel_group(v)[1])
                        pair = (roles.get(1, 0xFFFF), roles.get(2, 0xFFFF))
                        if all(p in self.kinds and self.kinds[p].kind in SAFE for p in pair if p != 0xFFFF):
                            pairs.append(pair)
            self.report.scanned = sorted({p for pr in pairs for p in pr if p != 0xFFFF})
        unsafe = sorted(c for c, k in self.kinds.items() if k.kind not in SAFE)
        found: list[riscv.Found] = []
        t0 = self.clock()
        for attempt in range(2):
            try:
                found = self.wire.scan(pairs) if pairs else []
            except h.Rejected as e:
                self.note(f"scan refused: {e}")
                break
            if found or not pairs or self.left() < 10:
                break
            if self.power is not None:
                self._power_cycle()                        # once more, from a clean power-on
            else:
                self.sleep(0.5)
        what = f"{len(pairs)} {'channels' if name == 'swio' else 'pairs'}"
        self.say(f"scan {name}: {what} ({ranges(self.report.scanned)}; not {ranges(unsafe)}: driven / active) "
                 f"in {self.clock() - t0:.2f} s -> "
                 + (", ".join(self._pins_text(f.pins) for f in found) or "nothing answered"))
        self.report.found = [f.pins for f in found]
        if found:
            self.pins = found[0].pins
            if len(found) > 1:
                self.note(f"{len(found)} answered; the rest of this run uses {self._pins_text(self.pins)}")
        return found

    def identify(self) -> None:
        if self.pins is None:
            return
        w = self.wire
        try:
            if not self.riscv:
                conn, dpidr, _ = w.attach(pins=self.pins)
                self.say(f"attach swd {self._pins_text(self.pins)}: DPIDR {dpidr:08x}")
                w.detach(conn)
                return
            conn, _ = w.attach(halt=True, pins=self.pins)
        except h.OepError as e:
            self.note(f"attach on {self._pins_text(self.pins)} did not get through: {e}")
            return
        self.mine.append(conn)
        try:
            tid = w.target_id
            self.family = targets.identify(tid)
            self.report.target_id = targets.describe_id(tid)
            self.report.family = self.family.name if self.family else None
            dpc = "" if w.dpc is None else f", halted at dpc {w.dpc:#x}"
            self.say(f"attach {self.report.wire} {self._pins_text(self.pins)}: target_id {self.report.target_id} -> "
                     f"{self.family.name if self.family else 'a family this client does not know'}{dpc}")
            if self.family and self.family.nrst and w.halted:
                try:
                    st = self.family.nrst(riscv.RiscvDm(self.hst, conn))
                    self.report.nrst, self.report.nrst_enabled = st.detail, st.enabled
                    self.say(f"  option bytes (read only): {st.detail}")
                except h.OepError as e:
                    self.note(f"option bytes not read: {e}")
            try:
                riscv.RiscvDm(self.hst, conn).resume()
            except h.OepError:
                pass
        finally:
            self._detach(conn)

    def _detach(self, conn: int) -> None:
        try:
            self.wire.detach(conn)
        except h.OepError:
            pass
        if conn in self.mine:
            self.mine.remove(conn)

    # ---- 5: reset line, confirmed ----
    def confirm_reset(self) -> int | None:
        if self.pins is None:
            return None
        if self.report.nrst_enabled is False:
            self.say("reset line: none to find (the option bytes turn it off)")
            return None
        allowed = set(role_channels(self.hst, self.wire.fn, ROLE_RESET))
        if not allowed:
            self.say(f"reset line: {self.wire.name} offers no reset channel (no attach under reset to confirm with)")
            return None
        pins = set(self.pins)
        vector = self.family.reset_vector if self.family else 0
        self._plan([])                                     # a reset line in a plan is refused by the attach
        stopped = [c for c in self.report.stopped if c in allowed and c not in pins]
        for c in stopped:
            if self.riscv and self._confirm(c, vector, loud=True, how="hold low stopped the activity, dpc confirmed"):
                return c
        if not self.riscv:
            if stopped:
                self.report.reset_channel, self.report.reset_how = stopped[0], "hold low (swd: not confirmed)"
                self.say(f"reset line: {stopped[0]} (holding it low stopped the activity; swd cannot confirm)")
                return stopped[0]
            return None
        rest = [c for c in self.candidates() if c in allowed and c not in pins and c not in stopped]
        rest.sort(key=lambda c: (self.kinds[c].kind != PULLED_UP, not self.kinds[c].follows_power, c))
        self.say(f"reset line: attach under reset through each of {len(rest)} candidates (dpc = {vector:#x})")
        t0 = self.clock()
        for c in rest:
            if self.left() < 2:
                self.note("time is up: the attach-under-reset search stopped early")
                break
            if self._confirm(c, vector, loud=False, how="dpc"):
                return c
        self.say(f"  not found ({self.clock() - t0:.1f} s)")
        return None

    def _confirm(self, c: int, vector: int, loud: bool, how: str) -> bool:
        """Attach under reset through c (find_reset_line on one channel, RESET_TRIES tries): True when the hart stops
        at the vector. `loud`: say the result whatever it is (else only a hit)."""
        hits = self.wire.find_reset_line([c], reset_vector=vector, hold_ms=self.RESET_HOLD_MS,
                                         tries=self.RESET_TRIES, pins=self.pins)
        seen = self.wire.last_search.get(c)
        if isinstance(seen, Exception):
            if loud:
                self.say(f"  attach under reset through {c}: refused ({seen})")
            return False
        text = ", ".join("failed" if d is None else f"{d:#x}" for d in seen)
        if hits:
            self.report.reset_channel, self.report.reset_dpc, self.report.reset_how = c, vector, how
            self.say(f"  attach under reset through {c} (held {self.RESET_HOLD_MS} ms): dpc {text} -> the reset line")
            return True
        if loud:
            self.say(f"  attach under reset through {c}: dpc {text} (not the vector {vector:#x})")
        return False

    # ---- 6: the slot ----
    def suggest(self) -> None:
        r = self.report
        if self.pins is None:
            return
        name = self.name or (self.family.name if self.family else "target")
        fam = self.family
        parts = [f"oep config slot {self.probe} --name {name} --wire {r.wire} --pins {self._pins_text(self.pins)}"]
        if fam and fam.max_speed:
            parts.append(f"--max-speed {fam.max_speed}")
        if fam and fam.idle_clock and r.wire == "rvswd":
            parts.append(f"--idle-clock {fam.idle_clock}")
        has_reset = "reset_channel" in {f.name for f in dataclasses.fields(config.Slot)}
        if r.reset_channel is not None and has_reset:
            parts.append(f"--reset-channel {r.reset_channel}")
        r.slot = " ".join(parts)
        self.say("slot: " + r.slot + ("" if self.save else "   (not written; --save writes it)"))
        if r.reset_channel is not None and not has_reset:
            self.say(f"  # reset_channel {r.reset_channel} (probe.config §1.1; this client's slot has no "
                     f"reset_channel yet: name it in the attach's reset TLV)")
        if self.save:
            kw = dict(slot=self.slot_no, wire_fn=self.wire.fn, pins=self.pins, name=name,
                      max_speed=(fam.max_speed or 0) if fam else 0,
                      idle_clock=(fam.idle_clock or "high") if fam and r.wire == "rvswd" else "high")
            if has_reset and r.reset_channel is not None:
                kw["reset_channel"] = r.reset_channel
            core.plan_release(self.hst)                    # a slot's pins must be free of this run's plans
            self.planned = []
            cfg = config.ProbeConfig(self.hst)
            cfg.set([config.Slot(**kw)])
            cfg.save()
            r.saved = True
            self.say(f"  written to slot {self.slot_no} and saved")

    # ---- the run ----
    def run(self, steps=STEPS) -> Report:
        r = self.report
        unknown = set(steps) - set(STEPS)
        if unknown:
            raise SystemExit(f"--steps: {', '.join(sorted(unknown))} unknown (steps: {', '.join(STEPS)})")
        try:
            if "classify" in steps:
                self.classify()
            if "hold" in steps and self.left() > 20:
                self.hold_search()
            if "scan" in steps and self.left() > 8:
                self.scan()
            if "identify" in steps and self.left() > 4:
                self.identify()
            if "reset" in steps and self.left() > 3:
                self.confirm_reset()
            if "slot" in steps:
                self.suggest()
        finally:
            for conn in list(self.mine):
                self._detach(conn)
            try:
                core.plan_release(self.hst)
                tail = (f" (channel {self.power} is back to its idle state: the target is powered only while "
                        f"something drives it)") if self.power is not None else ""
                self.say("released every plan" + tail)
            except h.OepError as e:
                self.note(f"plan release: {e}")
            r.elapsed_s = round(self.clock() - self.started, 2)
        return r
