"""oep.wire.rvswd / oep.wire.swio and oep.target.riscv-dm (oep-spec docs/capability-name-hierarchy.ja.md).

The host knows the target; the probe only moves wires and DMI. Everything chip-specific (flash controller, RAM loaders,
register meanings) stays on this side.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import host as h
from .core import Interface
from .fixture import Gpio


@dataclass
class Found:
    kind: int
    pins: tuple[int, int]
    dmstatus: int


class WireBase(Interface):
    """oep.wire.<link>: scan / attach / detach. The shared part; each link's attach takes its own arguments."""
    SCAN, ATTACH, DETACH, ATTACH_UNDER_RESET = 0x01, 0x02, 0x03, 0x04

    def scan(self) -> list[Found]:
        p = self._call(self.SCAN).payload
        out, at = [], 1
        for _ in range(p[0]):
            kind, dio, clk, status = struct.unpack_from("<BHHI", p, at)
            out.append(Found(kind, (dio, clk), status))
            at += 9
        return out

    def detach(self, conn: int) -> None:
        self._call(self.DETACH, bytes([conn]))


class Wire(WireBase):
    """oep.wire.rvswd / oep.wire.swio (CH32 debug links to a RISC-V debug module)."""
    NAME = "oep.wire.rvswd"
    DEFAULT_RESET = 0xFFFF

    def __init__(self, hst: h.Host, name: str = "oep.wire.rvswd"):
        super().__init__(hst, name)

    def attach(self, halt: bool = True) -> tuple[int, int]:
        """-> (connection, DMSTATUS). self.had_reset: a pending havereset was acknowledged first (a V00x's
        DMSTATUS halt / run bits stay frozen until then)."""
        p = self._call(self.ATTACH, bytes([int(halt)])).payload
        conn, status = struct.unpack_from("<BI", p)
        self.had_reset = len(p) > 5 and bool(p[5] & 1)
        return conn, status

    def attach_under_reset(self, channel: int | None = None, hold_ms: int = 20) -> tuple[int, int]:
        """Hold the target in reset through `channel` (None: the probe's default reset line), attach, release and
        halt it at once - the way back from firmware that turns the debug pins into GPIOs. -> (connection, dpc)"""
        body = struct.pack("<HH", self.DEFAULT_RESET if channel is None else channel, hold_ms)
        conn, dpc = struct.unpack_from("<BI", self._call(self.ATTACH_UNDER_RESET, body).payload)
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

class StepListError(h.OepError):
    NAMES = {1: "malformed", 2: "access failed", 3: "poll gave up"}

    def __init__(self, done: int, status: int, values: list[int]):
        super().__init__(f"step list stopped after {done} steps: {self.NAMES.get(status, status)}")
        self.done, self.status, self.values = done, status, values


class RiscvDm(Interface):
    """oep.target.riscv-dm on one connection."""
    NAME = "oep.target.riscv-dm"
    DMI, HALT, RESUME, RESET, READ_BLOCK, WRITE_BLOCK, RUN, STEP = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08
    RESET_RUN, RESET_RUN_CONFIRM, RESET_HALT = 0, 1, 2

    def __init__(self, hst: h.Host, conn: int, name: str = "oep.target.riscv-dm"):
        super().__init__(hst, name, prefix=bytes([conn]))
        self.conn = conn

    def halt(self) -> None:
        self._call(self.HALT)

    def resume(self) -> None:
        self._call(self.RESUME)

    def reset(self, confirm: bool = True) -> tuple[int, int, int]:
        """Reset and let it run (confirm: seen running by a PC sample). -> (flags, attempts, pc)"""
        mode = self.RESET_RUN_CONFIRM if confirm else self.RESET_RUN
        flags, attempts, pc = struct.unpack("<BBI", self._call(self.RESET, bytes([mode])).payload)
        return flags, attempts, pc

    def reset_halt(self) -> int:
        """Reset and stop before the first instruction (haltreq held through the reset). -> dpc"""
        _, _, pc = struct.unpack("<BBI", self._call(self.RESET, bytes([self.RESET_HALT])).payload)
        return pc

    def step(self) -> tuple[bool, int, int]:
        """One instruction (dcsr.step, one resume, privilege kept). -> (moved, dpc before, dpc after)"""
        moved, before, after = struct.unpack("<BII", self._call(self.STEP).payload)
        return bool(moved), before, after

    def read_block(self, address: int, count: int) -> bytes:
        return self._call(self.READ_BLOCK, struct.pack("<IH", address, count)).payload

    def write_block(self, address: int, data: bytes) -> None:
        self._call(self.WRITE_BLOCK, struct.pack("<I", address) + data)

    def write32(self, address: int, value: int) -> None:
        self.write_block(address, struct.pack("<I", value))

    def read32(self, address: int) -> int:
        return struct.unpack("<I", self.read_block(address, 1))[0]

    @staticmethod
    def run_body(pc: int, regs: list[tuple[int, int]], timeout_ms: int = 200) -> bytes:
        return struct.pack("<IHB", pc, timeout_ms, len(regs)) + b"".join(struct.pack("<HI", r, v) for r, v in regs)

    @staticmethod
    def run_result(payload: bytes) -> tuple[bool, int, int, int]:
        stopped, dpc, a0, us = struct.unpack_from("<BIII", payload)
        return bool(stopped), dpc, a0, us

    def run(self, pc: int, regs: list[tuple[int, int]], timeout_ms: int = 200) -> tuple[bool, int, int, int]:
        """Set registers and dpc, resume, wait for the hart's own ebreak (forced halt at the timeout).
        -> (stopped on its own, dpc, a0, microseconds)"""
        return self.run_result(self._call(self.RUN, self.run_body(pc, regs, timeout_ms)).payload)

    def dmi(self, steps: bytes) -> tuple[int, list[int]]:
        """Run a step list. -> (steps done, values read). A list that stopped early raises StepListError (with what it
        did get): a caller can no longer mistake an unfinished poll for a met one."""
        p = self._call(self.DMI, steps).payload
        done, status = struct.unpack_from("<HB", p)
        values = list(struct.unpack_from(f"<{(len(p) - 3) // 4}I", p, 3))
        if status:
            raise StepListError(done, status, values)
        return done, values

    @staticmethod
    def step_write(address: int, value: int) -> bytes:
        return struct.pack("<BBI", 0x01, address, value)

    @staticmethod
    def step_read(address: int) -> bytes:
        return struct.pack("<BB", 0x02, address)

    @staticmethod
    def step_poll(address: int, mask: int, value: int, max_reads: int) -> bytes:
        return struct.pack("<BBIIH", 0x03, address, mask, value, max_reads)

    @staticmethod
    def step_delay(us: int) -> bytes:
        return struct.pack("<BI", 0x04, us)

    @staticmethod
    def step_poll_time(address: int, mask: int, value: int, max_us: int) -> bytes:
        """poll bounded by time rather than reads: the same meaning on a slow bit-banged link and a fast one."""
        return struct.pack("<BBIII", 0x05, address, mask, value, max_us)


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
        release, last = hst.pipeline([gpio.request_release(channel), wire_.request(Wire.ATTACH, bytes([1]))],
                                     exchange=exchange)
        if not release.succeeded:
            raise h.Failed(release)                       # never leave the reset line held
        if last.succeeded:
            conn, status = struct.unpack_from("<BI", last.payload)
            return conn, status
    raise h.Failed(last) if last is not None else ValueError("tries must be at least 1")
