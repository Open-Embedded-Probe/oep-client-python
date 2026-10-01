"""oep.wire.rvswd / oep.wire.swio and oep.target.riscv-dm, revision 1 (oep-spec oep-if-debug §1-§4).

The host knows the target; the probe only moves wires and DMI. Everything chip-specific (flash controller, RAM loaders,
register meanings) stays on this side.

common §3: wire and target results carry a status (ok, wait, line, fault, timeout, state; any other value is a failure). A
request the probe ran but that did not get through is completed failed (nothing done) or partial (some done) with the
success shape, so `done` and `status` say how far it went: this module raises TargetError with them. Every block op is
self-contained (oep-if-debug §4): the probe restores the GPRs, DATA0 / DATA1 and abstractauto before it answers, so
nothing of the probe's own is left in the target between requests.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import catalog, host as h, message as m, registry as reg
from .core import Interface, describe, max_op_ms
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


def status_name(status: int) -> str:
    return STATUS_NAMES.get(status, f"unknown status 0x{status:02x}")


class TargetError(h.OepError):
    """A wire or target operation that did not get through: `status` (§5.4), `done` (steps / words completed, where
    the op has it), `values` (what it did read), `result` (the probe's answer)."""

    def __init__(self, what: str, status: int, result: m.Result | None = None, done: int | None = None,
                 values: list[int] | None = None, data: bytes = b""):
        at = f" after {done}" if done is not None else ""
        outcome = f" ({result.describe()})" if result is not None else ""
        super().__init__(f"{what} stopped{at}: {status_name(status)}{outcome}")
        self.status, self.result, self.done, self.values, self.data = status, result, done, values or [], data


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

    def _reset_tlv(self, reset: tuple[int, int] | None) -> bytes:
        # (channel, hold_ms), critical: hold the reset line (open drain, low) that long, then attach (oep-if-debug §3)
        return b"" if reset is None else m.tlv(self.TAG_RESET, struct.pack("<HH", *reset), critical=True)

    def scan(self, pairs: list[tuple[int, int]] | None = None, max_speed: int | None = None,
             idle_clock: str | None = None) -> list[Found]:
        """Try `pairs` of (swdio, swclk); None = every pair the probe allows and nothing holds (describe's
        channel_group / role_channels, oep-if-debug §1). A pair the probe does not allow, or one whose pins something
        holds, refuses the whole scan (rejected unavailable). max_speed (critical; None: the probe's slowest) and
        idle_clock ("high" / "low", rvswd only, critical) are the target's line settings (§3). The probe tries at most
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
            rd = m.Reader(self._call(self.SCAN, body + extra).payload)
            tried, count = rd.take("BB")
            for _ in range(count):
                kind, dio, clk, status = rd.element().take("BHHI")
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
                e = rd.element()
                conn, swdio, swclk, speed, users, slot, scheme, n = e.take("HHHIBBBB")
                out.append(ConnectionInfo(conn, (swdio, swclk), speed, users, None if slot == 0xFF else slot,
                                          (scheme, e.bytes(n)) if scheme else None))
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
    SCHEME_WCH_DMI_7F = reg.WIRE_RVSWD.enum["target_id_scheme"]["wch_dmi_7f"]

    TAG_DPC = reg.WIRE_RVSWD.tlv["attach_answer"]["dpc"]

    def __init__(self, hst: h.Host, name: str = "oep.wire.rvswd"):
        super().__init__(hst, name)
        self.had_reset = self.existing = self.halted = False
        self.flags = 0
        self.speed_hz = 0
        self.dpc: int | None = None                        # the halted hart's dpc (attach flags bit3), else None
        self.ignored: list[int] = []
        self.target_id: tuple[int, bytes] | None = None   # (scheme, value) the last attach read, or None

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
        declared max_clock_hz); idle_clock: "high" / "low", how rvswd rests SWCLK (critical). Both are the target's,
        known by the host (oep-if-debug §3). reset = (channel, hold_ms): hold that reset line (one of reset_channels())
        low for hold_ms, then attach - halting before the first instruction with halt=True - the way back from firmware
        that turns the debug pins into GPIOs. self.target_id: (scheme, value) of the target's identity when the probe
        could read one."""
        rd = m.Reader(self._call(self.ATTACH, self.attach_body(halt, max_speed, pins, idle_clock, reset)).payload)
        conn, status, self.flags, self.speed_hz = rd.take("HIBI")
        self.had_reset = bool(self.flags & self.FLAGS["havereset_acked"])
        self.existing = bool(self.flags & self.FLAGS["existing"])
        self.halted = bool(self.flags & self.FLAGS["halted"])
        tail = rd.tail()
        self.ignored = tail.ignored
        self._take_target_id(tail)
        dpc = tail.get(self.TAG_DPC)
        self.dpc = int.from_bytes(dpc, "little") if self.halted and dpc else None
        return conn, status

    def attach_under_reset(self, channel: int, hold_ms: int = 20, max_speed: int | None = None,
                           pins: tuple[int, int] | None = None, idle_clock: str | None = None) -> tuple[int, int | None]:
        """attach(halt=True, reset=(channel, hold_ms)): hold the target in reset through `channel` (always named:
        there is no default reset line; the probe allows reset_channels()), attach, release and halt it at once.
        -> (connection, dpc) (dpc None when the hart was not halted)."""
        conn, _ = self.attach(True, max_speed, pins, idle_clock, reset=(channel, hold_ms))
        return conn, self.dpc

    def find_reset_line(self, candidates: list[int], reset_vector: int = 0, hold_ms: int = 20,
                        tries: int = 3) -> list[int]:
        """Which of `candidates` resets the target: attach under reset through each, and see where the hart stops.
        The real line stops it before its first instruction (dpc = reset_vector); any other channel leaves the
        target running, so the halt lands somewhere in its code. A channel counts once any of `tries` lands on the
        vector: the CH32L103 is caught by polling right after the release (it keeps no haltreq through NRST), which
        misses now and then (1 in 60 after the probe fix of 2026-09-24), while landing on the vector by chance is
        not a worry. Channels the probe does not allow (rejected) are skipped; a failed attach
        counts as a miss and is tried again. Each try pulls one channel
        low (open drain) for hold_ms. The target is left running (or halted, where resume is not acknowledged)."""
        hits = []
        self.last_search = {}   # channel -> list of dpc values (None: attach failed), or the rejection
        for channel in candidates:
            seen = []
            for _ in range(tries):
                try:
                    conn, dpc = self.attach_under_reset(channel, hold_ms)
                except h.Rejected as e:                    # not a channel this probe allows
                    seen = e
                    break
                except h.Failed:
                    seen.append(None)                      # the attach itself failed: try again
                    continue
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
                    self.detach(conn)
                if dpc == reset_vector:
                    hits.append(channel)
                    break
            self.last_search[channel] = seen
        return hits


class StepListError(TargetError):
    """A DMI step list that stopped early: `done` = the failed step's index, `values` = what it did read."""

    def __init__(self, done: int, status: int, values: list[int], result: m.Result | None = None):
        super().__init__("step list", status, result, done=done, values=values)


RUN_STOPPED = _RV.enum["run_stopped"]     # 0 the limit passed and the probe halted it, 1 stopped on its own, 2 not halted


@dataclass
class RunResult:
    status: int
    stopped: bool                 # the hart halted on its own (ebreak) before timeout_ms
    dpc: int
    elapsed_us: int
    values: list[int] = field(default_factory=list)   # the registers asked for in `outs`, in order
    not_halted: bool = False      # the limit passed and the probe could not halt the hart: dpc and values mean nothing


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


def dmi_value_count(kinds: list[int], done: int, status: int) -> int:
    """§5.5: the reads and polls among the first `done` steps, plus the failed step's last value when it is a poll
    that timed out (a poll whose read failed on the line adds nothing)."""
    n = sum(k in VALUE_STEPS for k in kinds[:done])
    if status == TIMEOUT and done < len(kinds) and kinds[done] in POLL_STEPS:
        n += 1
    return n


class RiscvDm(Interface):
    """oep.target.riscv-dm on one connection (every request starts with the connection, u16)."""
    NAME = "oep.target.riscv-dm"
    REVISION = 1
    DMI, HALT, RESUME, RESET, READ_BLOCK, WRITE_BLOCK, RUN, STEP = (
        _RV.op[k] for k in ("dmi", "halt", "resume", "reset", "read_block", "write_block", "run", "step"))
    RESET_RUN, RESET_RUN_CONFIRM, RESET_HALT = (_RV.enum["reset_mode"][k] for k in ("run", "run_verified", "halt_at_reset"))
    METHOD_DEFAULT, METHOD_NDMRESET, METHOD_SYSTEM = (_RV.enum["reset_method"][k]
                                                      for k in ("probe_default", "ndmreset", "system_reset"))
    TAG_RESET_METHOD = _RV.tlv["reset"]["method"]

    def __init__(self, hst: h.Host, conn: int, name: str = "oep.target.riscv-dm"):
        super().__init__(hst, name, prefix=struct.pack("<H", conn))
        self.conn = conn

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
        (the CH32 rule) are the host's: ch32_flash.resume."""
        self._status_only("resume", self.RESUME)

    DPC = 0x07B1

    def read_register(self, regno: int) -> int:
        """A GPR / CSR of the halted hart through an abstract command (access register, 32 bits) in plain DMI steps, so
        any probe with dmi does it. A cmderr is cleared, then raised."""
        _, values = self.dmi([self.step_write(0x17, 0x00220000 | regno), self.step_poll(0x16, 1 << 12, 0, 100),
                              self.step_read(0x04)])
        cs, data0 = values[0], values[1]
        if (cs >> 8) & 7:
            self.dmi([self.step_write(0x16, 0x700)])
            raise RuntimeError(f"abstract command for register {regno:#x} failed (cmderr {(cs >> 8) & 7})")
        return data0

    def _reset(self, mode: int, method: int | None) -> tuple[int, int, int]:
        body = bytes([mode])
        if method is not None:
            body += m.tlv(self.TAG_RESET_METHOD, bytes([method]), critical=True)
        r = self._request(self.RESET, body)
        rd = ran(r)
        status, flags, attempts, pc = rd.take("BBBI")
        rd.tail()
        check("reset", r, status)
        return flags, attempts, pc

    def reset(self, confirm: bool = True, method: int | None = None) -> tuple[int, int, int]:
        """Reset and let it run (confirm: seen running). method: METHOD_* (critical; None: the probe chooses).
        -> (flags, attempts, pc)"""
        return self._reset(self.RESET_RUN_CONFIRM if confirm else self.RESET_RUN, method)

    def reset_halt(self, method: int | None = None) -> int:
        """Reset and stop before the first instruction (haltreq held through the reset). -> dpc"""
        return self._reset(self.RESET_HALT, method)[2]

    def step(self) -> tuple[bool, int, int]:
        """One instruction (dcsr.step, one resume, privilege kept). -> (moved, dpc before, dpc after)"""
        r = self._request(self.STEP)
        rd = ran(r)
        status, moved, before, after = rd.take("BBII")
        rd.tail()
        check("step", r, status)
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
                         not_halted=stopped == RUN_STOPPED["not_halted"])

    def run(self, pc: int, regs: list[tuple[int, int]], timeout_ms: int | None = 200,
            outs: tuple[int, ...] = (REG_A0,)) -> RunResult:
        """Set registers and dpc (dcsr.ebreakm, prv = M), resume, wait for the hart's own ebreak (forced halt at the
        timeout: stopped False, status timeout - returned, not raised; not_halted when the probe could not even stop
        it). timeout_ms None: the probe's max_op_ms (core §7.5), the most it allows. Other statuses raise TargetError."""
        if timeout_ms is None:
            timeout_ms = max_op_ms(self.host)
        r = self._request(self.RUN, self.run_body(pc, regs, timeout_ms, outs))
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
        r = self._request(self.DMI, struct.pack("<H", len(kinds)) + raw)
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
        release, last = hst.pipeline([gpio.request_release(channel), attach], exchange=exchange)
        if not release.succeeded:
            raise h.Failed(release)                       # never leave the reset line held
        if last.succeeded:
            conn, status = m.Reader(last.payload).take("HI")
            return conn, status
    raise h.Failed(last) if last is not None else ValueError("tries must be at least 1")
