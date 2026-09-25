"""oep.wire.rvswd / oep.wire.swio and oep.target.riscv-dm, revision 1 (oep-spec v1-core-wire-delta §5.4, §5.5).

The host knows the target; the probe only moves wires and DMI. Everything chip-specific (flash controller, RAM loaders,
register meanings) stays on this side.

§5.4: wire and target results carry a status (ok, wait, line, fault, timeout, state; any other value is a failure). A
request the probe ran but that did not get through is completed failed (nothing done) or partial (some done) with the
success shape, so `done` and `status` say how far it went: this module raises TargetError with them.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import host as h, message as m, registry as reg
from .core import Interface
from .fixture import Gpio

STATUS = reg.STATUS
OK = STATUS["ok"]
STATUS_NAMES = {v: k for k, v in STATUS.items()}
_RV = reg.TARGET_RISCV_DM
STEP = _RV.enum["dmi_step"]
STEP_WRITE, STEP_READ, STEP_POLL_READS, STEP_WAIT_US, STEP_POLL_US = (
    STEP["write"], STEP["read"], STEP["poll_reads"], STEP["wait_us"], STEP["poll_us"])
STEP_SIZES = {STEP_WRITE: 6, STEP_READ: 2, STEP_POLL_READS: 12, STEP_WAIT_US: 5, STEP_POLL_US: 14}
VALUE_STEPS = {STEP_READ, STEP_POLL_READS, STEP_POLL_US}      # steps that add a value to the result
POLL_STEPS = {STEP_POLL_READS, STEP_POLL_US}                  # ... and add their last value even when they fail
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


class WireBase(Interface):
    """oep.wire.<link>: scan / attach / detach. The shared part; each link's attach takes its own arguments."""
    SCAN, ATTACH, DETACH, ATTACH_UNDER_RESET = 0x01, 0x02, 0x03, 0x04
    REVISION = 1
    TAG_MAX_SPEED = 0x01

    def scan(self) -> list[Found]:
        rd = m.Reader(self._call(self.SCAN).payload)
        out = []
        for _ in range(rd.u8()):
            kind, dio, clk, status = rd.take("BHHI")
            out.append(Found(kind, (dio, clk), status))
        rd.tail()
        return out

    def detach(self, conn: int) -> None:
        self._call(self.DETACH, bytes([conn]))

    def _speed_tlv(self, max_speed: int | None) -> bytes:
        # critical: a probe that cannot keep to a ceiling must refuse, not ignore it (§0: safety arguments)
        return b"" if max_speed is None else m.tlv(self.TAG_MAX_SPEED, struct.pack("<I", max_speed), critical=True)


class Wire(WireBase):
    """oep.wire.rvswd / oep.wire.swio (CH32 debug links to a RISC-V debug module)."""
    NAME = "oep.wire.rvswd"
    DEFAULT_RESET = 0xFFFF
    RUN, HALT = reg.WIRE_RVSWD.enum["attach_method"]["run"], reg.WIRE_RVSWD.enum["attach_method"]["halt"]

    def __init__(self, hst: h.Host, name: str = "oep.wire.rvswd"):
        super().__init__(hst, name)
        self.had_reset = self.existing = False
        self.speed_hz = 0
        self.ignored: list[int] = []

    def attach(self, halt: bool = True, max_speed: int | None = None) -> tuple[int, int]:
        """-> (connection, DMSTATUS). Attaching an attached wire returns its connection as it is (self.existing).
        self.had_reset: a pending havereset was acknowledged first (a V00x's DMSTATUS halt / run bits stay frozen
        until then); self.speed_hz: the speed the probe chose; max_speed: a ceiling the probe must keep (critical)."""
        body = bytes([self.HALT if halt else self.RUN]) + self._speed_tlv(max_speed)
        rd = m.Reader(self._call(self.ATTACH, body).payload)
        conn, status, flags, self.speed_hz = rd.take("BIBI")
        self.had_reset, self.existing = bool(flags & 1), bool(flags & 2)
        self.ignored = rd.tail().ignored
        return conn, status

    def attach_under_reset(self, channel: int | None = None, hold_ms: int = 20,
                           max_speed: int | None = None) -> tuple[int, int]:
        """Hold the target in reset through `channel` (None: the probe's default reset line), attach, release and
        halt it at once - the way back from firmware that turns the debug pins into GPIOs. -> (connection, dpc)"""
        body = struct.pack("<HH", self.DEFAULT_RESET if channel is None else channel, hold_ms) + self._speed_tlv(max_speed)
        rd = m.Reader(self._call(self.ATTACH_UNDER_RESET, body).payload)
        conn, dpc, self.speed_hz = rd.take("BII")
        rd.tail()
        return conn, dpc

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


@dataclass
class RunResult:
    status: int
    stopped: bool                 # the hart halted on its own (ebreak) before timeout_ms
    dpc: int
    elapsed_us: int
    values: list[int] = field(default_factory=list)   # the registers asked for in `outs`, in order


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
    """§5.5: the reads and polls among the first `done` steps, plus the failed step's last value when it is a poll."""
    n = sum(k in VALUE_STEPS for k in kinds[:done])
    if status != OK and done < len(kinds) and kinds[done] in POLL_STEPS:
        n += 1
    return n


class RiscvDm(Interface):
    """oep.target.riscv-dm on one connection (every request starts with the connection byte)."""
    NAME = "oep.target.riscv-dm"
    REVISION = 1
    DMI, HALT, RESUME, RESET, READ_BLOCK, WRITE_BLOCK, RUN, STEP = (
        _RV.op[k] for k in ("dmi", "halt", "resume", "reset", "read_block", "write_block", "run", "step"))
    RESET_RUN, RESET_RUN_CONFIRM, RESET_HALT = (_RV.enum["reset_mode"][k] for k in ("run", "run_verified", "halt_at_reset"))
    METHOD_DEFAULT, METHOD_NDMRESET, METHOD_SYSTEM = (_RV.enum["reset_method"][k]
                                                      for k in ("probe_default", "ndmreset", "system_reset"))
    TAG_RESET_METHOD = _RV.tlv["reset"]["method"]
    NO_TIMEOUT = 0xFFFFFFFF

    def __init__(self, hst: h.Host, conn: int, name: str = "oep.target.riscv-dm"):
        super().__init__(hst, name, prefix=bytes([conn]))
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
        """ok = the hart left debug mode at least once (status state if it never did)."""
        self._status_only("resume", self.RESUME)

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
    def run_body(cls, pc: int, regs: list[tuple[int, int]], timeout_ms: int | None = 200,
                 outs: tuple[int, ...] = (REG_A0,)) -> bytes:
        """pc, timeout_ms (None: no limit), the registers to set, then the registers to read back (`outs`)."""
        t = cls.NO_TIMEOUT if timeout_ms is None else timeout_ms
        return (struct.pack("<IIB", pc, t, len(regs)) + b"".join(struct.pack("<HI", r, v) for r, v in regs)
                + struct.pack("<B", len(outs)) + b"".join(struct.pack("<H", r) for r in outs))

    @staticmethod
    def run_result(result: m.Result, n_out: int = 1) -> RunResult:
        """Decode a run result (any known outcome) without judging it."""
        rd = ran(result)
        status, stopped, dpc, us = rd.take("BBII")
        values = rd.words(n_out)
        rd.tail()
        return RunResult(status, bool(stopped), dpc, us, values)

    def run(self, pc: int, regs: list[tuple[int, int]], timeout_ms: int | None = 200,
            outs: tuple[int, ...] = (REG_A0,)) -> RunResult:
        """Set registers and dpc (dcsr.ebreakm, prv = M), resume, wait for the hart's own ebreak (forced halt at the
        timeout: stopped False, status timeout - returned, not raised). Other statuses raise TargetError."""
        r = self._request(self.RUN, self.run_body(pc, regs, timeout_ms, outs))
        res = self.run_result(r, len(outs))
        if res.status == STATUS["timeout"] and not res.stopped:
            return res
        check("run", r, res.status)
        return res

    def dmi(self, steps: bytes | list[bytes]) -> tuple[int, list[int]]:
        """Run a step list (the step_* builders, concatenated or as a list). -> (steps done, values: one per read and
        poll step). A list that stopped early raises StepListError (with what it did read): a caller cannot mistake
        an unfinished poll for a met one."""
        raw = b"".join(steps) if isinstance(steps, list) else steps
        kinds = count_steps(raw)
        r = self._request(self.DMI, struct.pack("<H", len(kinds)) + raw)
        rd = ran(r)
        done, status = rd.take("HB")
        values = rd.words(dmi_value_count(kinds, done, status))
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
    """For a probe without attach_under_reset: pull `channel` low through oep.fixture.gpio, then send its release
    and an attach (halt) in one exchange so the probe starts the attach right after the release, and retry - a race
    at the edge of the target's reset window (2026-09-24, CH32V003 with SWIO turned off: 2 of 5 pipelined, 0 of 5
    one request at a time). `exchange` is the link's pipelining exchange (default: the host's). -> (connection, DMSTATUS)"""
    gpio = Gpio(hst, gpio_fn)
    last = None
    for _ in range(tries):
        gpio.pull_low(channel)
        time.sleep(low_s)
        release, last = hst.pipeline([gpio.request_release(channel), wire_.request(Wire.ATTACH, bytes([Wire.HALT]))],
                                     exchange=exchange)
        if not release.succeeded:
            raise h.Failed(release)                       # never leave the reset line held
        if last.succeeded:
            conn, status = m.Reader(last.payload).take("BI")
            return conn, status
    raise h.Failed(last) if last is not None else ValueError("tries must be at least 1")
