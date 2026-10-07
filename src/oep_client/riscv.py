"""oep.wire.rvswd / oep.wire.swio and oep.target.riscv-dm, revision 1 (oep-spec oep-if-debug §1-§4).

The host knows the target; the probe only moves wires and DMI. Everything chip-specific (flash controller, RAM loaders,
register meanings) stays on this side.

common §3: wire and target results carry a status (ok, wait, line, fault, timeout, state; any other value is a failure). A
request the probe ran but that did not get through is completed failed (nothing done) or partial (some done) with the
success shape, so `done` and `status` say how far it went: this module raises TargetError with them. Every block op is
self-contained (oep-if-debug §4): the probe restores the GPRs, DATA0 / DATA1 and abstractauto before it answers, so
nothing of the probe's own is left in the target between requests. DATA0 / DATA1 that this host's own dmi sequences
change (read_register, write_register) are the host's to write back before the hart runs (debug §4): the console's
mailbox lives there (`RiscvDm.data_saved`, `RiscvDm.restore_data`).

The waits: attach, scan and riscv-dm's reset take as long as the probe needs, up to max_op_ms - their argument time
(core §4.4, debug §1, §4.3).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import catalog, host as h, message as m, registry as reg
from .core import FALLBACK_MAX_OP_MS, Interface, describe, max_op_ms
from .fixture import Gpio

STATUS = reg.STATUS
OK = STATUS["ok"]
TIMEOUT = STATUS["timeout"]
STATUS_NAMES = {v: k for k, v in STATUS.items()}
_RV = reg.TARGET_RISCV_DM
STEP = _RV.enum["dmi_step"]
STEP_WRITE, STEP_READ, STEP_POLL_READS, STEP_WAIT_US, STEP_POLL_US = (
    STEP["write"], STEP["read"], STEP["poll_reads"], STEP["wait_us"], STEP["poll_us"])
STEP_SIZES = {STEP_WRITE: 6, STEP_READ: 2, STEP_POLL_READS: 12, STEP_WAIT_US: 5, STEP_POLL_US: 14}
VALUE_STEPS = {STEP_READ, STEP_POLL_READS, STEP_POLL_US}      # steps that add a value to the result
POLL_STEPS = {STEP_POLL_READS, STEP_POLL_US}                  # ... and add their last value when they time out
REG_A0, REG_A1 = 0x100A, 0x100B
DATA0, DMCONTROL, DMSTATUS, ABSTRACTCS, COMMAND = 0x04, 0x10, 0x11, 0x16, 0x17   # DMI addresses (debug spec 0.13 / 1.0)
HELD_TRIES = 4             # tries of a held DMI group (RiscvDm.held) before it is given up
HELD_RETRY_S = 0.005       # the pause before a held group's next try: past the 0.7 - 2.1 ms a dropped link has been
                           # seen to stay down, and long enough for the probe to bring an idle link back up
HELD_RETRY_STATUSES = {STATUS["line"], STATUS["timeout"], STATUS["wait"]}   # what a drop can make a step list answer
CMDERR_PARITY = 6          # QingKe's ABSTRACTCS cmderr 6, "parity bit error during communication": a frame it ignored
READ_SENTINELS = (0x00000000, 0xFFFFFFFF)   # DATA0 before each of read_register's two commands (they differ in every bit)


def _cmderr_of(abstractcs: int) -> int:
    return (abstractcs >> 8) & 7


def status_name(status: int) -> str:
    return STATUS_NAMES.get(status, f"unknown status 0x{status:02x}")


class NoMaxLength(h.OepError):
    """The probe declares no max_length for an interface with read_block / write_block. oep-if-debug §4.5 / §6 make it
    mandatory there, and the host takes its block size from it alone - never from max_frame."""


def declared_max_length(hst: h.Host, fn: int, name: str) -> int:
    """The describe common tag max_length (core §7.4) of interface `fn`, in bytes, rounded down to a word. The probe
    declares it so that both a read_block answer and a write_block request fit its max_frame (oep-if-debug §4.5).
    NoMaxLength when the probe does not declare it (or declares less than one word)."""
    for tag, value in describe(hst, fn):
        if tag & 0x7F == catalog.MAX_LENGTH and len(value) >= 2:
            length = struct.unpack_from("<H", value)[0] // 4 * 4
            if length >= 4:
                return length
            break
    raise NoMaxLength(f"{name} (fn {fn}) declares no usable max_length: read_block / write_block need it "
                      f"(oep-if-debug §4.5; the host does not compute a block size from max_frame)")


class BlockLength:
    """`max_length` (bytes one read_block / write_block may move, from the probe's describe) and `max_words`, for the
    interfaces with block operations. Read once per instance, on first use."""
    host: h.Host
    fn: int
    name: str
    _max_length: int | None = None

    @property
    def max_length(self) -> int:
        """Bytes one block operation may move (describe max_length, oep-if-debug §4.5): what the probe declared, never
        a value computed from max_frame. NoMaxLength when the probe declares none."""
        if self._max_length is None:
            self._max_length = declared_max_length(self.host, self.fn, self.name)
        return self._max_length

    @property
    def max_words(self) -> int:
        """Words (u32) one block operation may move: max_length / 4."""
        return self.max_length // 4


class TargetError(h.OepError):
    """A wire or target operation that did not get through: `status` (§5.4), `done` (steps / words completed, where
    the op has it), `values` (what it did read), `result` (the probe's answer)."""

    def __init__(self, what: str, status: int, result: m.Result | None = None, done: int | None = None,
                 values: list[int] | None = None, data: bytes = b""):
        at = f" after {done}" if done is not None else ""
        outcome = f" ({result.describe()})" if result is not None else ""
        super().__init__(f"{what} stopped{at}: {status_name(status)}{outcome}")
        self.status, self.result, self.done, self.values, self.data = status, result, done, values or [], data


class StepError(TargetError):
    """step did not succeed (oep-if-debug §4.2): `step_left` - the probe could not halt the hart again (it runs,
    dcsr.step may still be set: halt it and clear dcsr.step); otherwise the hart is halted again (or was not halted to
    begin with). The answer's moved, dpc_before and dpc_after mean something only with status ok - the probe sends 0
    otherwise and they are not read: dpc_before / dpc_after are None; a halted hart's dpc is read with dmi."""

    def __init__(self, status: int, result: m.Result, step_left: bool):
        super().__init__("step", status, result)
        self.dpc_before = self.dpc_after = None
        self.step_left = step_left


def ran(result: m.Result) -> m.Reader:
    """The payload of a result the probe ran (success, failed or partial: all in the success shape); an unknown
    resolution or outcome raises Failed (§0)."""
    if not result.ran:
        raise h.Failed(result)
    return m.Reader(result.payload)


def check(what: str, result: m.Result, status: int, **kw) -> None:
    """Success needs outcome success AND status ok; anything else (an unknown status included) raises."""
    if status != OK or not result.succeeded:
        raise TargetError(what, status, result, **kw)


@dataclass
class Found:
    kind: int
    pins: tuple[int, int]
    dmstatus: int


_WIRE = reg.WIRE_RVSWD          # the three wires share their op and tag numbers (oep-if-debug §3, §5)


class WireBase(Interface):
    """oep.wire.<link>: scan / attach / detach / connections. The shared part; each link's attach takes its own
    arguments. A failed scan, attach or detach is completed failed with `status(u8) [TLV]` (common §3)."""
    SCAN, ATTACH, DETACH, CONNECTIONS = (_WIRE.op[k] for k in ("scan", "attach", "detach", "connections"))
    REVISION = 1
    TAG_MAX_SPEED = _WIRE.tlv["attach"]["max_speed"]   # u32 Hz, critical; attach requires it (oep-if-debug §1)
    TAG_PINS = _WIRE.tlv["attach"]["pins"]             # swdio(u16) swclk(u16, 0xFFFF on one wire), critical
    TAG_RESET = _WIRE.tlv["attach"]["reset"]           # channel(u16) hold_ms(u16), critical: attach under reset (§3)
    TAG_SCAN_MAX_SPEED = _WIRE.tlv["scan"]["max_speed"]
    TAG_SKIP = _WIRE.tlv["scan"]["skip"]               # scan, count 0 only: pairs of the count-0 list to skip (§1)
    TAG_SCAN_IDLE_CLOCK = _WIRE.tlv["scan"]["idle_clock"]
    FLAGS = _WIRE.enum["attach_flags"]                 # bit0 havereset_acked, bit1 existing, bit2 dormant_woken, bit3 halted
    DEFAULT_MAX_SPEED = 1_000_000                      # when the wire declares no max_clock_hz either

    def _speed_or_default(self, max_speed: int | None) -> int:
        """attach's max_speed is required (malformed without): None takes the wire's declared max_clock_hz (its
        describe, cached), else DEFAULT_MAX_SPEED. The target's ceiling is the host's to know (oep-if-debug §3)."""
        if max_speed is not None:
            return max_speed
        for tag, v in describe(self.host, self.fn):
            if tag & ~m.TAG_CRITICAL == catalog.MAX_CLOCK_HZ and len(v) >= 4:
                return struct.unpack_from("<I", v)[0]
        return self.DEFAULT_MAX_SPEED

    def _max_op_ms(self) -> int:
        """The probe's max_op_ms (core §7.5), FALLBACK_MAX_OP_MS when it cannot be read."""
        try:
            return max_op_ms(self.host)
        except (h.OepError, AttributeError, TypeError):
            return FALLBACK_MAX_OP_MS

    def attach_ms(self, reset: tuple[int, int] | None = None) -> int:
        """attach's argument time (core §4.4, oep-if-debug §1): max_op_ms - the probe answers within it, its search,
        retries, the reset TLV's hold and the wait for a silent DM included. The host's wait adds host_wait_add_ms and
        the transfer time."""
        return self._max_op_ms()

    def scan_ms(self) -> int:
        """scan's argument time: max_op_ms (oep-if-debug §1)."""
        return self._max_op_ms()

    def _reset_tlv(self, reset: tuple[int, int] | None) -> bytes:
        # (channel, hold_ms), critical: hold the reset line (open drain, low) that long, then attach (oep-if-debug §3)
        return b"" if reset is None else m.tlv(self.TAG_RESET, struct.pack("<HH", *reset), critical=True)

    def scan(self, pairs: list[tuple[int, int]] | None = None, max_speed: int | None = None,
             idle_clock: str | None = None) -> list[Found]:
        """Try `pairs` of (swdio, swclk); None = every pair the probe allows and nothing holds (describe's
        channel_group / role_channels, oep-if-debug §1). A pair the probe does not allow, or one whose pins something
        holds, refuses the whole scan (rejected unavailable). max_speed (critical; None: the wire's slowest speed) and
        idle_clock ("high" / "low", rvswd only, critical) are the target's line settings (§3); they do not change a
        live connection's settings (a live pair is read over its connection, §1). The probe tries at most
        255 pairs a request and stops early when its answer would not fit one frame; this goes on until every pair is
        tried (count 0: with skip until tried = 0; the probe tries at least one pair while any remain)."""
        out = []
        pairs = list(pairs or [])
        skip = 0
        extra = b""
        if max_speed is not None:
            extra += m.tlv(self.TAG_SCAN_MAX_SPEED, struct.pack("<I", max_speed), critical=True)
        if idle_clock is not None:
            extra += m.tlv(self.TAG_SCAN_IDLE_CLOCK, bytes([_WIRE.enum["idle_clock"][idle_clock]]), critical=True)
        while True:
            chunk = pairs[:255]
            body = bytes([len(chunk)]) + b"".join(struct.pack("<HH", d, c) for d, c in chunk)
            if not pairs and skip:
                body += m.tlv(self.TAG_SKIP, struct.pack("<H", skip))
            rd = m.Reader(self._call(self.SCAN, body + extra, expect_ms=self.scan_ms()).payload)
            tried, count = rd.take("BB")
            for _ in range(count):
                kind, dio, clk, status = rd.take("BHHI")         # 9 bytes, no element length (core §2.3)
                out.append(Found(kind, (dio, clk), status))
            rd.tail()
            if tried == 0:
                return out
            if pairs:
                pairs = pairs[tried:]
                if not pairs:
                    return out
            else:
                skip += tried

    def detach(self, conn: int, force: bool = False) -> None:
        """Take this session's share of the connection (it closes when nobody uses it); force closes it whatever
        else uses it (oep-if-debug §2)."""
        body = struct.pack("<H", conn)
        if force:
            body += m.tlv(_WIRE.tlv["detach"]["force"], b"", critical=True)
        self._call(self.DETACH, body)

    def connections(self) -> list["ConnectionInfo"]:
        """The wire's live connections, in the order they were made (oep-if-debug §2.1, lock-free; paged: the request
        is first(u8), the answer more count entries, followed until more is 0)."""
        out: list[ConnectionInfo] = []
        while True:
            rd = m.Reader(self._call(self.CONNECTIONS, bytes([len(out)]), locked=False).payload)
            more, count = rd.take("BB")
            for _ in range(count):
                conn, swdio, swclk, speed, users, slot, scheme, n = rd.take("HHHIBBBB")
                tid = rd.bytes(n)
                out.append(ConnectionInfo(conn, (swdio, swclk), speed, users, None if slot == 0xFF else slot,
                                          (scheme, tid) if scheme else None))
            rd.tail()
            if not more or not count or len(out) > 0xFF:
                return out

    def _speed_tlv(self, max_speed: int | None) -> bytes:
        # critical: a probe that cannot keep to a ceiling must refuse, not ignore it (core §2.3: safety arguments)
        return b"" if max_speed is None else m.tlv(self.TAG_MAX_SPEED, struct.pack("<I", max_speed), critical=True)

    def _pins_tlv(self, pins: tuple[int, int] | None) -> bytes:
        # the pair to attach on (a scan result's .pins); None: the probe's only pair
        return b"" if pins is None else m.tlv(self.TAG_PINS, struct.pack("<HH", *pins), critical=True)


@dataclass(frozen=True)
class ConnectionInfo:
    """One row of connections (oep-if-debug §2.1): users bit0 = a host session, bit1 = a slot; slot = the slot it is
    the connection of (None: none); target_id = (scheme, value) read at attach (None: none)."""
    connection: int
    pins: tuple[int, int]
    speed_hz: int
    users: int
    slot: int | None
    target_id: tuple[int, bytes] | None


class Wire(WireBase):
    """oep.wire.rvswd / oep.wire.swio (CH32 debug links to a RISC-V debug module)."""
    NAME = "oep.wire.rvswd"
    ROLE_RESET = reg.WIRE_RVSWD.enum["pin_role"]["reset"]
    TAG_IDLE_CLOCK = reg.WIRE_RVSWD.tlv["attach"]["idle_clock"]
    IDLE_CLOCK = reg.WIRE_RVSWD.enum["idle_clock"]
    RUN, HALT = reg.WIRE_RVSWD.enum["attach_method"]["run"], reg.WIRE_RVSWD.enum["attach_method"]["halt"]
    TAG_TARGET_ID = reg.WIRE_RVSWD.tlv["attach_answer"]["target_id"]
    SCHEME_DMI_7F = reg.COMMON.enum["target_id_scheme"]["dmi_7f"]   # the u32 at DMI 0x7F (oep-if-debug §3)

    TAG_DPC = reg.WIRE_RVSWD.tlv["attach_answer"]["dpc"]
    TAG_SEARCH_RETRIES = reg.WIRE_RVSWD.tlv["attach_answer"]["search_retries"]

    def __init__(self, hst: h.Host, name: str = "oep.wire.rvswd"):
        super().__init__(hst, name)
        self.had_reset = self.existing = self.halted = False
        self.flags = 0
        self.speed_hz = 0
        self.dpc: int | None = None                        # the halted hart's dpc (attach flags bit3), else None
        self.target_id: tuple[int, bytes] | None = None   # (scheme, value) the last attach read, or None
        self.search_retries: int | None = None           # the last attach's failed speed-search tries (None: not said)

    def _take_target_id(self, tail: m.Tail) -> None:
        v = tail.get(self.TAG_TARGET_ID)
        self.target_id = (v[0], bytes(v[1:])) if v else None

    def _idle_tlv(self, idle_clock: str | None) -> bytes:
        # rvswd only, critical: a probe that cannot rest the line that way must refuse (oep-if-debug §3)
        return b"" if idle_clock is None else m.tlv(self.TAG_IDLE_CLOCK, bytes([self.IDLE_CLOCK[idle_clock]]), critical=True)

    def reset_channels(self) -> list[int]:
        """The channels an attach's reset TLV may take (describe role_channels, role reset). There is no default reset
        line: the host names one every time (oep-if-debug §3)."""
        out = set()
        for tag, v in describe(self.host, self.fn):
            if tag & ~m.TAG_CRITICAL == catalog.ROLE_CHANNELS and v[0] == self.ROLE_RESET:
                out.update(catalog.bitmap_to_channels(struct.unpack_from("<H", v, 1)[0], v[3:]))
        return sorted(out)

    def attach_body(self, halt: bool = True, max_speed: int | None = None, pins: tuple[int, int] | None = None,
                    idle_clock: str | None = None, reset: tuple[int, int] | None = None) -> bytes:
        """The attach request: method, then the TLVs (max_speed always: it is required)."""
        return (bytes([self.HALT if halt else self.RUN]) + self._speed_tlv(self._speed_or_default(max_speed))
                + self._pins_tlv(pins) + self._idle_tlv(idle_clock) + self._reset_tlv(reset))

    def attach(self, halt: bool = True, max_speed: int | None = None, pins: tuple[int, int] | None = None,
               idle_clock: str | None = None, reset: tuple[int, int] | None = None) -> tuple[int, int]:
        """-> (connection, DMSTATUS). Attaching an attached wire returns its connection as it is (self.existing).
        self.had_reset: a pending havereset was acknowledged first (a V00x's DMSTATUS halt / run bits stay frozen
        until then); self.halted / self.dpc: the hart is halted and where (attach flags bit3, TLV dpc); self.speed_hz:
        the speed the probe chose; max_speed: the ceiling the probe must keep (critical, required; None: the wire's
        declared max_clock_hz); idle_clock: "high" / "low", how rvswd rests SWCLK (critical; sent whenever given, high
        included). Both are the target's, known by the host (oep-if-debug §3). An attach that joins a live connection
        (a slot's, another session's) changes only what it carries: idle_clock None keeps the connection's current
        rest (None means high only for a new connection), and max_speed only lowers its speed (§1) - a caller that
        knows the target's rest (a slot's idle_clock) passes it, high included. reset = (channel, hold_ms): hold that reset line (one of reset_channels())
        low for hold_ms, then attach - halting before the first instruction with halt=True - the way back from firmware
        that turns the debug pins into GPIOs. self.target_id: (scheme, value) of the target's identity when the probe
        could read one."""
        rd = m.Reader(self._call(self.ATTACH, self.attach_body(halt, max_speed, pins, idle_clock, reset),
                                 expect_ms=self.attach_ms(reset)).payload)
        conn, status, self.flags, self.speed_hz = rd.take("HIBI")
        self.had_reset = bool(self.flags & self.FLAGS["havereset_acked"])
        self.existing = bool(self.flags & self.FLAGS["existing"])
        self.halted = bool(self.flags & self.FLAGS["halted"])
        tail = rd.tail()
        self._take_target_id(tail)
        dpc = tail.get(self.TAG_DPC)
        self.dpc = int.from_bytes(dpc, "little") if self.halted and dpc else None
        tries = tail.get(self.TAG_SEARCH_RETRIES)                 # 0xFFFF = 65535 or more (oep-if-debug §1)
        self.search_retries = struct.unpack_from("<H", tries)[0] if tries and len(tries) >= 2 else None
        return conn, status

    def attach_under_reset(self, channel: int, hold_ms: int = 20, max_speed: int | None = None,
                           pins: tuple[int, int] | None = None, idle_clock: str | None = None) -> tuple[int, int | None]:
        """attach(halt=True, reset=(channel, hold_ms)): hold the target in reset through `channel` (always named:
        there is no default reset line; the probe allows reset_channels()), attach, release and halt it at once.
        -> (connection, dpc) (dpc None when the hart was not halted)."""
        conn, _ = self.attach(True, max_speed, pins, idle_clock, reset=(channel, hold_ms))
        return conn, self.dpc

    def find_reset_line(self, candidates: list[int], reset_vector: int = 0, hold_ms: int = 20,
                        tries: int = 3, pins: tuple[int, int] | None = None) -> list[int]:
        """Which of `candidates` resets the target: attach under reset through each, and see where the hart stops.
        The real line stops it before its first instruction (dpc = reset_vector); any other channel leaves the
        target running, so the halt lands somewhere in its code. A channel counts once any of `tries` lands on the
        vector: the CH32L103 is caught by polling right after the release (it keeps no haltreq through NRST), which
        misses now and then (1 in 60 after the probe fix of 2026-09-24), while landing on the vector by chance is
        not a worry. Each try pulls one channel low (open drain) for hold_ms. The target is left running (or halted,
        where resume is not acknowledged).

        pins: the debug pins to attach on (a scan result's .pins). Name them on a probe whose wire takes its pins
        from the host (role_channels): without pins such a probe attaches only to the wire's one live connection, so
        once the first try's detach closed it every later attach was refused (rejected unavailable) - and was taken
        for "not a reset line" (2026-10-02, ESP32-P4 + CH32V003: [] with the real line at channel 4). None takes the
        pins of the wire's one live connection, if there is one. A connection that was there before the search
        (attach flags existing) is left open.

        Skipped: a channel the probe does not offer as a reset line (rejected unsupported) and one something else
        holds (rejected unavailable naming that channel); `last_search[channel]` keeps the rejection. Any other
        rejection is about the pins or the wire, not the channel, and is raised. A failed attach counts as a miss and
        is tried again (the first attach under reset of a session failed once in 7 on the P4)."""
        if pins is None:
            live = self.connections()
            if len(live) == 1:
                pins = live[0].pins
        hits = []
        self.last_search = {}   # channel -> list of dpc values (None: attach failed), or the rejection
        for channel in candidates:
            seen = []
            for _ in range(tries):
                try:
                    conn, dpc = self.attach_under_reset(channel, hold_ms, pins=pins)
                except h.Unavailable as e:
                    if channel not in e.channels:          # the pins or the wire, not this channel: no search
                        raise
                    seen = e                               # held by something else (a plan, a slot)
                    break
                except h.Rejected as e:
                    if e.result.detail != m.UNSUPPORTED:
                        raise
                    seen = e                               # not a channel this probe offers as a reset line
                    break
                except h.Failed:
                    seen.append(None)                      # the attach itself failed: try again
                    continue
                existing = self.existing
                seen.append(dpc)
                dm = RiscvDm(self.host, conn)
                try:
                    if dpc == reset_vector:
                        # Leave the vector for real: a hart left halted there reads dpc = vector again through the
                        # next, wrong channel (2026-09-24: a CH32L103 whose resume was not acknowledged made the
                        # channel after NRST a false hit). A reset-and-run always gets it going.
                        dm.reset(confirm=True)
                    else:
                        dm.resume()
                except h.OepError:
                    pass   # a CH32L103 raises no allresumeack; a hart left halted mid-code still lands off the vector
                finally:
                    if not existing:
                        self.detach(conn)                  # the caller's own connection stays
                if dpc == reset_vector:
                    hits.append(channel)
                    break
            self.last_search[channel] = seen
        return hits


class StepListError(TargetError):
    """A DMI step list that stopped early: `done` = the failed step's index, `values` = what it did read."""

    def __init__(self, done: int, status: int, values: list[int], result: m.Result | None = None):
        super().__init__("step list", status, result, done=done, values=values)


class LinkNotHeld(h.OepError):
    """A held DMI group (RiscvDm.held) that never met the debug link up from its first transaction to its last in
    `tries` tries: nothing it read can be trusted and any of its writes may be lost. `last` is the last try's
    StepListError, or the looks (DMSTATUS, DMCONTROL before, then after) that did not pass."""

    def __init__(self, what: str, tries: int, last):
        super().__init__(f"{what}: no try of {tries} ran with the debug link held over the whole DMI group (a dropped "
                         f"link loses writes and reads back stale values or all ones), so nothing it read is "
                         f"confirmed; last: {last}")
        self.what, self.tries, self.last = what, tries, last


def link_held(dmstatus: int, dmcontrol: int) -> bool:
    """One look at the link (two DMI reads): DMSTATUS a module's (a version scan finds: 2 or more, not 15) with
    authenticated (bit 7) set, and DMCONTROL with dmactive set and hart 0 selected (hartsel / hasel, bits 6..26, clear).
    A dropped link reads all ones, or the last value read, for both - one value cannot have bit 7 set and clear - so a
    look that passes says the link was up for both reads."""
    version = dmstatus & 0xF
    return (2 <= version != 15 and bool(dmstatus & 0x80)
            and bool(dmcontrol & 1) and not dmcontrol & 0x07FFFFC0)


RUN_STOPPED = _RV.enum["run_stopped"]     # 0 the limit passed and the probe halted it, 1 stopped on its own, 2 not
                                          # halted, 3 not run (debug §4.4)


@dataclass
class RunResult:
    status: int
    stopped: bool                 # the hart halted on its own (ebreak) before timeout_ms
    dpc: int                      # 0 when it means nothing (not_halted, not_run; debug §4.4): see dpc_valid
    elapsed_us: int               # from the resumereq that started the hart to the halt seen, made or given up (the
                                  # probe's measure, debug §4.4); 0 when the hart was not run
    values: list[int] = field(default_factory=list)   # the registers asked for in `outs`, in order
    not_halted: bool = False      # the limit passed and the probe could not halt the hart: dpc and values mean nothing
    not_run: bool = False         # the preparation (registers, dcsr, pc) failed: the hart was not run and is still
                                  # halted, the loader did not run (dpc means nothing; debug §4.4)

    @property
    def dpc_valid(self) -> bool:
        """Whether dpc means something: the hart halted (on its own or at the limit)."""
        return not (self.not_halted or self.not_run)

    def where(self) -> str:
        """The dpc for a message, never an invalid one."""
        if self.not_run:
            return "not run"
        return f"dpc {self.dpc:#x}" if self.dpc_valid else "not halted, dpc unknown"


def count_steps(steps: bytes) -> list[int]:
    """The kinds of a packed step list, in order (so the count and the value rule need no bookkeeping by callers)."""
    kinds, at = [], 0
    while at < len(steps):
        kind = steps[at]
        if kind not in STEP_SIZES:
            raise ValueError(f"DMI step kind 0x{kind:02x} at byte {at} is not one this client knows")
        kinds.append(kind)
        at += STEP_SIZES[kind]
    if at != len(steps):
        raise ValueError("the step list ends inside a step")
    return kinds


def dmi_wait_ms(raw: bytes) -> int:
    """The time a step list may take by its waits and time-bounded polls (wait_us, poll_us), in ms (rounded up)."""
    us, at = 0, 0
    while at < len(raw):
        kind = raw[at]
        if kind == STEP_WAIT_US:
            us += struct.unpack_from("<I", raw, at + 1)[0]
        elif kind == STEP_POLL_US:
            us += struct.unpack_from("<I", raw, at + 10)[0]
        size = STEP_SIZES.get(kind)
        if size is None:
            break
        at += size
    return -(-us // 1000)


def dmi_value_count(kinds: list[int], done: int, status: int) -> int:
    """§5.5: the reads and polls among the first `done` steps, plus the failed step's last value when it is a poll
    that timed out (a poll whose read failed on the line adds nothing)."""
    n = sum(k in VALUE_STEPS for k in kinds[:done])
    if status == TIMEOUT and done < len(kinds) and kinds[done] in POLL_STEPS:
        n += 1
    return n


class RiscvDm(Interface, BlockLength):
    """oep.target.riscv-dm on one connection (every request starts with the connection, u16). `max_length` /
    `max_words` bound read_block / write_block (from the probe's describe)."""
    NAME = "oep.target.riscv-dm"
    REVISION = 1
    DMI, HALT, RESUME, RESET, READ_BLOCK, WRITE_BLOCK, RUN, STEP = (
        _RV.op[k] for k in ("dmi", "halt", "resume", "reset", "read_block", "write_block", "run", "step"))
    RESET_RUN, RESET_RUN_CONFIRM, RESET_HALT = (_RV.enum["reset_mode"][k] for k in ("run", "run_verified", "halt_at_reset"))
    TAG_STEP_LEFT = _RV.tlv["step_answer"]["step_left"]
    OPTIONAL = {"block": (_RV.op["read_block"], _RV.op["write_block"]), "run": (_RV.op["run"],),
                "reset": (_RV.op["reset"],), "step": (_RV.op["step"],)}   # the optional ops (debug §4), by name

    def __init__(self, hst: h.Host, conn: int, name: str = "oep.target.riscv-dm"):
        super().__init__(hst, name, prefix=struct.pack("<H", conn))
        self.conn = conn
        self._max_length = None
        self.data_saved: int | None = None   # DATA0 as it was before this host's own abstract commands changed it

    def declared(self) -> set[str]:
        """The optional ops this probe offers, from its describe's ops tag (debug §4, core §1.2, §7.4): "block"
        (read_block / write_block, offered as a pair), "run", "reset", "step". dmi, halt and resume are always there;
        an op not offered is answered unknown_operation, and the host builds the same thing from dmi. A describe
        without an ops tag (not a conforming probe) declares none."""
        offered = self.ops() or set()
        return {name for name, ops in self.OPTIONAL.items() if all(op in offered for op in ops)}

    def _status_only(self, what: str, op: int) -> None:
        r = self._request(op)
        rd = ran(r)
        status = rd.u8()
        rd.tail()
        check(what, r, status)

    def halt(self) -> None:
        """Idempotent: an already halted hart is ok."""
        self._status_only("halt", self.HALT)

    def resume(self) -> None:
        """One resumereq; ok = the hart left debug mode (status state if the probe saw it not go). Parts that need more
        (the CH32 rule) are the host's: ch32_flash.resume. DATA0 is written back first (`restore_data`)."""
        self.restore_data()
        self._status_only("resume", self.RESUME)

    def _save_data(self) -> None:
        """Before this host's first abstract command since the hart last ran: read DATA0 as it is (the console's
        mailbox, console §3), to write it back before the hart runs again (debug §4)."""
        if self.data_saved is None:
            (self.data_saved,) = self.held([self.step_read(DATA0)], "saving DATA0")

    def restore_data(self) -> None:
        """Write DATA0 back as it was before this host's own abstract commands changed it (debug §4: before the hart
        runs; read_register / write_register leave it changed). Nothing when they did not run since the hart last ran;
        resume, step and run call it first."""
        if self.data_saved is not None:
            self.held([self.step_write(DATA0, self.data_saved)], "restoring DATA0")
            self.data_saved = None

    DPC = 0x07B1

    def look(self) -> bytes:
        """The steps of one look at the link: read DMSTATUS, read DMCONTROL (link_held judges the two values)."""
        return self.step_read(DMSTATUS) + self.step_read(DMCONTROL)

    def held(self, steps: bytes | list[bytes], what: str = "DMI group", tries: int = HELD_TRIES,
             agree=None) -> list[int]:
        """Run a DMI group with the link looked at around it, in one dmi request: look, the steps, look. -> the
        values of the steps (the looks' taken off). A debug link may drop for a while after a change of hart state (a
        CH32L103 behind an RVSWD probe, about 0.7 - 2 ms): writes are then lost and reads give the last value read or
        all ones, with nothing in the answer to say so - a stale DATA0 reads like a register. A drop lasts until the
        link is brought up again; when the look after the steps passes, every step met the link up - provided the
        probe did not bring it up again between the looks (its revive after a rest), which a probe with the per-request
        check (oep-probe-arduino 0.0.29-dev, the 2026-10-06 debug-link proposal P4) answers status line. A try whose
        looks do not pass, whose values `agree` (when given) does not take, or that stopped with a status a drop can
        cause (line, timeout, wait), is run again after HELD_RETRY_S, `tries` times at most; then LinkNotHeld. Any other
        stop raises StepListError at once. The group must be one that may run twice: it writes values fixed before it
        and reads what nothing in it changes (a cmderr left by an earlier try is the group's to clear)."""
        raw = b"".join(steps) if isinstance(steps, list) else steps
        look = self.look()
        last = None
        for attempt in range(tries):
            if attempt:
                time.sleep(HELD_RETRY_S)
            try:
                _, values = self.dmi(look + raw + look)
            except StepListError as e:
                if e.status not in HELD_RETRY_STATUSES:
                    raise
                last = e
                continue
            if not (link_held(*values[:2]) and link_held(*values[-2:])):
                last = "looks " + " ".join(f"{v:#010x}" for v in values[:2] + values[-2:])
                continue
            if agree is not None and not agree(values[2:-2]):
                last = "values that do not agree: " + " ".join(f"{v:#010x}" for v in values[2:-2])
                continue
            return values[2:-2]
        raise LinkNotHeld(what, tries, last)

    def _abstract(self, command: int, before: bytes = b"") -> bytes:
        """The steps of one access-register command with the link held around it: clear cmderr (a try's redo, or
        one left behind, would have the command ignored), `before` (DATA0 for a write), the command, wait for it (the
        poll adds ABSTRACTCS, cmderr in it)."""
        return (self.step_write(ABSTRACTCS, 0x700) + before + self.step_write(COMMAND, command)
                + self.step_poll(ABSTRACTCS, 1 << 12, 0, 100))

    def _cmderr(self, cs: int, what: str) -> None:
        if (cs >> 8) & 7:
            self.held([self.step_write(ABSTRACTCS, 0x700)], f"{what}: clearing cmderr")
            raise RuntimeError(f"{what} failed (cmderr {(cs >> 8) & 7})")

    def _held_abstract(self, steps: bytes, what: str, agree) -> list[int]:
        """held() for a group of access-register commands that begins by clearing cmderr: a try that ends with cmderr 6
        is a failed try - QingKe's "parity error": the module took one of the group's frames for a bad one and ignored
        it, a missed access with the link up again at once (bench, oep-probe-arduino 0.0.29-dev+f594f04 / 4c310a2,
        tests/hw test_wire on a CH32L103 through an RVSWD probe: "read_register ... failed (cmderr 6)") - and the group
        is run again within the tries, as for a drop (it was: RuntimeError). Still cmderr 6 after every try: cmderr
        cleared, LinkNotHeld."""
        parity = []

        def take(values: list[int]) -> bool:
            if any(_cmderr_of(v) == CMDERR_PARITY for v in values):
                parity.append(True)
            return agree(values)
        try:
            return self.held(steps, what, agree=take)
        except LinkNotHeld:
            if parity:                            # leave no cmderr 6 behind (one try: the link did not hold)
                try:
                    self.held([self.step_write(ABSTRACTCS, 0x700)], f"{what}: clearing cmderr", tries=1)
                except (LinkNotHeld, StepListError):
                    pass
            raise

    def read_register(self, regno: int) -> int:
        """A GPR / CSR of the halted hart through an abstract command (access register, 32 bits) in plain DMI steps, so
        any probe with dmi does it. The register is read twice in the group - DATA0 first set to READ_SENTINELS[0],
        then to READ_SENTINELS[1], and before the second DATA0 read DMSTATUS is read - and taken only when the two
        agree: a command lost on its own leaves its sentinel in DATA0, and a DATA0 read that gives the value of the read
        before it gives ABSTRACTCS the first time and DMSTATUS the second, so one such miss between looks that pass
        cannot make the two agree on anything but the register's value (bench, oep-probe-arduino 0.0.29-dev+bd19b00,
        tests/hw test_wire on a CH32L103 through an RVSWD probe: a0 read as s1's value after a read_block, the looks
        passing). The group is held (`held`): a value read over a dropped link is never returned - the group is tried
        again, and LinkNotHeld raised when it could not be confirmed. cmderr 6 (a frame the module took for a bad
        parity: a missed access) is such a failed try too (`_held_abstract`); any other cmderr is cleared, then raised
        (RuntimeError).
        DATA0 is left holding the value; its value from before is kept (`data_saved`) and written back before the hart
        runs (`restore_data`, debug §4). abstractauto is the host's and must be clear (writing DATA0 would run the
        command again)."""
        what = f"read_register {regno:#x}"
        self._save_data()
        command = 0x00220000 | regno
        poll = self.step_poll(ABSTRACTCS, 1 << 12, 0, 100)
        steps = (self.step_write(ABSTRACTCS, 0x700)
                 + self.step_write(DATA0, READ_SENTINELS[0]) + self.step_write(COMMAND, command) + poll
                 + self.step_read(DATA0)
                 + self.step_write(DATA0, READ_SENTINELS[1]) + self.step_write(COMMAND, command) + poll
                 + self.step_read(DMSTATUS) + self.step_read(DATA0))

        def agree(values: list[int]) -> bool:   # cmderr 6: tried again; another is raised below whatever DATA0 says
            cs1, first, cs2, _, second = values
            err = _cmderr_of(cs2) or _cmderr_of(cs1)
            if err == CMDERR_PARITY:
                return False
            return bool(err) or first == second

        cs1, data0, cs2, _, _ = self._held_abstract(steps, what, agree)
        self._cmderr(cs1 | cs2, what)
        return data0

    def write_register(self, regno: int, value: int) -> None:
        """Write a GPR / CSR of the halted hart through an abstract command (access register, 32 bits, DATA0 first),
        held as read_register is: when it returns, every write met the link up and no cmderr came. Whether the
        register took the value (read-only or WARL bits) is the caller's to read back. cmderr 6 is a failed try, the
        write done again (the same value: a register write may be redone), as in read_register."""
        what = f"write_register {regno:#x}"
        self._save_data()
        (cs,) = self._held_abstract(self._abstract(0x00230000 | regno, self.step_write(DATA0, value)), what,
                                    lambda values: _cmderr_of(values[0]) != CMDERR_PARITY)
        self._cmderr(cs, what)

    def _reset(self, mode: int) -> tuple[int, int]:
        # its argument time: max_op_ms - the probe answers within it, a DM silent after the release included (oep-if-debug
        # §4.3, core §4.4)
        r = self._request(self.RESET, bytes([mode]), expect_ms=self.reset_ms())
        rd = ran(r)
        status, flags, pc = rd.take("BBI")                 # status flags(bit0 reached, bit1 verified) pc (§4.3)
        rd.tail()
        check("reset", r, status)
        return flags, pc

    def reset_ms(self) -> int:
        """reset's argument time (core §4.4): max_op_ms (oep-if-debug §4.3)."""
        try:
            return max_op_ms(self.host)
        except (h.OepError, AttributeError, TypeError):
            return FALLBACK_MAX_OP_MS

    def reset(self, confirm: bool = True) -> tuple[int, int]:
        """Reset through ndmreset and let it run (confirm: seen running, the pc read). The reset op never drives a
        reset line (debug §4.3): a line moves only through attach's reset TLV (`Wire.attach_under_reset`) or a
        fixture. -> (flags, pc)"""
        return self._reset(self.RESET_RUN_CONFIRM if confirm else self.RESET_RUN)

    def reset_halt(self) -> int:
        """Reset and stop before the first instruction (haltreq held through the reset). -> dpc"""
        return self._reset(self.RESET_HALT)[1]

    def step(self) -> tuple[bool, int, int]:
        """One instruction (dcsr.step, one resume, privilege kept). -> (moved, dpc before, dpc after). A hart that did
        not come back, and any status but ok, raises StepError (oep-if-debug §4.2, P2-○4) without moved or the dpcs
        (0 unless status ok, not read): `step_left` False - the probe halted it with haltreq and restored it (read its
        dpc with dmi); True (answer TLV step_left) - it could not halt it again: the hart runs and dcsr.step may still
        be set, so the host halts it and clears dcsr.step. DATA0 is written back first (`restore_data`)."""
        self.restore_data()
        r = self._request(self.STEP)
        rd = ran(r)
        status, moved, before, after = rd.take("BBII")
        tail = rd.tail()
        if status != OK or not r.succeeded:
            raise StepError(status, r, tail.get(self.TAG_STEP_LEFT) is not None)   # moved / dpcs: 0, not read (§4.2)
        return bool(moved), before, after

    def read_block(self, address: int, count: int) -> bytes:
        """`count` words from `address`. A read that stopped raises TargetError (.data = the words it did read)."""
        r = self._request(self.READ_BLOCK, struct.pack("<IH", address, count))
        data, done, status = self.read_block_result(r)
        if status != OK or not r.succeeded or done != count:
            raise TargetError("read_block", status, r, done=done, data=data)
        return data

    @staticmethod
    def read_block_result(r: m.Result) -> tuple[bytes, int, int]:
        """-> (the words read as bytes, done, status)."""
        rd = ran(r)
        done, status = rd.take("HB")
        data = rd.bytes(4 * done)
        rd.tail()
        return data, done, status

    @staticmethod
    def write_block_body(address: int, data: bytes) -> bytes:
        if len(data) % 4:
            raise ValueError("write_block writes whole words")
        return struct.pack("<IH", address, len(data) // 4) + data

    def write_block(self, address: int, data: bytes) -> None:
        r = self._request(self.WRITE_BLOCK, self.write_block_body(address, data))
        rd = ran(r)
        done, status = rd.take("HB")
        rd.tail()
        check("write_block", r, status, done=done)

    def write32(self, address: int, value: int) -> None:
        self.write_block(address, struct.pack("<I", value))

    def read32(self, address: int) -> int:
        return struct.unpack("<I", self.read_block(address, 1))[0]

    @classmethod
    def run_body(cls, pc: int, regs: list[tuple[int, int]], timeout_ms: int = 200,
                 outs: tuple[int, ...] = (REG_A0,)) -> bytes:
        """pc, timeout_ms (1 .. the probe's max_op_ms; run() turns None into that ceiling), the registers to set,
        then the registers to read back (`outs`)."""
        if timeout_ms is None:
            raise ValueError("run_body needs a timeout_ms (1 .. the probe's max_op_ms); run(timeout_ms=None) takes "
                             "the probe's ceiling")
        return (struct.pack("<IIB", pc, timeout_ms, len(regs)) + b"".join(struct.pack("<HI", r, v) for r, v in regs)
                + struct.pack("<B", len(outs)) + b"".join(struct.pack("<H", r) for r in outs))

    @staticmethod
    def run_result(result: m.Result, n_out: int | None = None) -> RunResult:
        """Decode a run result (any known outcome) without judging it: status stopped dpc elapsed_us nvals(u8) values
        [TLV] (oep-if-debug §4.4; `n_out` is not needed any more, the answer counts its values)."""
        rd = ran(result)
        status, stopped, dpc, us, nvals = rd.take("BBIIB")
        values = rd.words(nvals)
        rd.tail()
        return RunResult(status, stopped == RUN_STOPPED["stopped"], dpc, us, values,
                         not_halted=stopped == RUN_STOPPED["not_halted"], not_run=stopped == RUN_STOPPED["not_run"])

    def run(self, pc: int, regs: list[tuple[int, int]], timeout_ms: int | None = 200,
            outs: tuple[int, ...] = (REG_A0,)) -> RunResult:
        """Set registers and dpc (dcsr.ebreakm, prv = M), resume, wait for the hart's own ebreak (forced halt at the
        timeout: stopped False, status timeout - returned, not raised; not_halted when the probe could not even stop
        it). timeout_ms None: the probe's max_op_ms (core §7.5), the most it allows. Other statuses raise TargetError -
        a preparation that failed (stopped 3, debug §4.4) too: the hart was not run, it is still halted, and the error's
        `result` says so (`run_result(e.result).not_run`)."""
        if timeout_ms is None:
            timeout_ms = max_op_ms(self.host)
        self.restore_data()
        r = self._request(self.RUN, self.run_body(pc, regs, timeout_ms, outs), expect_ms=timeout_ms)
        res = self.run_result(r)
        if res.status == STATUS["timeout"] and not res.stopped:
            return res
        check("run", r, res.status)
        return res

    def dmi(self, steps: bytes | list[bytes]) -> tuple[int, list[int]]:
        """Run a step list (the step_* builders, concatenated or as a list). -> (steps done, values: one per read and
        poll step). The answer is done status nvals(u16) values [TLV] (oep-if-debug §4.1). A list that stopped early
        raises StepListError (with what it did read): a caller cannot mistake an unfinished poll for a met one."""
        raw = b"".join(steps) if isinstance(steps, list) else steps
        kinds = count_steps(raw)
        r = self._request(self.DMI, struct.pack("<H", len(kinds)) + raw, expect_ms=dmi_wait_ms(raw))
        rd = ran(r)
        done, status, nvals = rd.take("HBH")
        values = rd.words(nvals)
        rd.tail()
        if status != OK or not r.succeeded or done != len(kinds):
            raise StepListError(done, status, values, r)
        return done, values

    @staticmethod
    def step_write(address: int, value: int) -> bytes:
        return struct.pack("<BBI", STEP_WRITE, address, value)

    @staticmethod
    def step_read(address: int) -> bytes:
        return struct.pack("<BB", STEP_READ, address)

    @staticmethod
    def step_poll(address: int, mask: int, value: int, max_reads: int) -> bytes:
        """Read until (value & mask) == value, at most max_reads times; adds the last value read (met or not)."""
        return struct.pack("<BBIIH", STEP_POLL_READS, address, mask, value, max_reads)

    @staticmethod
    def step_delay(us: int) -> bytes:
        return struct.pack("<BI", STEP_WAIT_US, us)

    @staticmethod
    def step_poll_time(address: int, mask: int, value: int, max_us: int) -> bytes:
        """poll bounded by time rather than reads: the same meaning on a slow bit-banged link and a fast one."""
        return struct.pack("<BBIII", STEP_POLL_US, address, mask, value, max_us)


def attach_after_gpio_reset(hst: h.Host, wire_: Wire, gpio_fn: int, channel: int, exchange=None, *, tries: int = 10,
                            low_s: float = 0.02) -> tuple[int, int]:
    """For a probe without the attach reset TLV: pull `channel` low through oep.fixture.gpio, then send its release
    and an attach (halt) in one exchange so the probe starts the attach right after the release, and retry - a race
    at the edge of the target's reset window (2026-09-24, CH32V003 with SWIO turned off: 2 of 5 pipelined, 0 of 5
    one request at a time). `exchange` is the link's pipelining exchange (default: the host's). -> (connection, DMSTATUS)"""
    gpio = Gpio(hst, gpio_fn)
    attach = wire_.request(Wire.ATTACH, wire_.attach_body(True))   # built once: its describe is not in the race
    last = None
    for _ in range(tries):
        gpio.pull_low(channel)
        time.sleep(low_s)
        with hst.expecting(wire_.attach_ms()):            # the attach's budget is its argument time (core §4.4)
            release, last = hst.pipeline([gpio.request_release(channel), attach], exchange=exchange)
        if not release.succeeded:
            raise h.Failed(release)                       # never leave the reset line held
        if last.succeeded:
            conn, status = m.Reader(last.payload).take("HI")
            return conn, status
    raise h.Failed(last) if last is not None else ValueError("tries must be at least 1")
