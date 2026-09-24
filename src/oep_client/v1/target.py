"""v1 draft clients for oep.wire.<link> and oep.target.riscv-dm (oep-spec docs/capability-name-hierarchy.ja.md).

The host knows the target; the probe only moves wires and DMI. Everything chip-specific (flash controller,
RAM loaders, register meanings) stays on this side.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import host as h, message as m, wire


def find(hst: h.Host, name: str) -> int:
    """fn of the first interface with exactly this name (lock-free list, paged)."""
    first = 0
    while True:
        total, page = wire.unpack_list_result(
            hst.request(m.CORE_FN, m.OP_LIST, wire.pack_list_request(name, True, first), locked=False).payload)
        if page:
            return page[0].fn
        if first + len(page) >= total:
            raise LookupError(f"probe does not offer {name}")
        first += len(page)


def confirm(hst: h.Host) -> dict:
    p = hst.request(m.CORE_FN, m.OP_CONFIRM, locked=False).payload
    magic, revision, max_frame, window, inflight = struct.unpack("<4sBHHB", p[:10])
    return {"magic": magic, "revision": revision, "max_frame": max_frame, "window": window, "max_inflight": inflight}


@dataclass
class Found:
    kind: int
    pins: tuple[int, int]
    dmstatus: int


class Wire:
    SCAN, ATTACH, DETACH, ATTACH_UNDER_RESET = 0x01, 0x02, 0x03, 0x04
    DEFAULT_RESET = 0xFFFF

    def __init__(self, hst: h.Host, name: str = "oep.wire.rvswd"):
        self.host, self.fn = hst, find(hst, name)

    def _call(self, op: int, body: bytes = b"") -> m.Result:
        r = self.host.request(self.fn, op, body)
        if not r.succeeded:
            raise h.Rejected(r)
        return r

    def scan(self) -> list[Found]:
        p = self._call(self.SCAN).payload
        out, at = [], 1
        for _ in range(p[0]):
            kind, dio, clk, status = struct.unpack_from("<BHHI", p, at)
            out.append(Found(kind, (dio, clk), status))
            at += 9
        return out

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
        conn, dpc = struct.unpack("<BI", self._call(self.ATTACH_UNDER_RESET, body).payload)
        return conn, dpc

    def detach(self, conn: int) -> None:
        self._call(self.DETACH, bytes([conn]))

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
                except h.Rejected as e:
                    if e.result.resolution == m.REJECTED:   # not a channel this probe allows
                        seen = e
                        break
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
                except h.Rejected:
                    pass   # a CH32L103 raises no allresumeack; a hart left halted mid-code still lands off the vector
                finally:
                    self.detach(conn)
                if dpc == reset_vector:
                    hits.append(channel)
                    break
            self.last_search[channel] = seen
        return hits

class RiscvDm:
    DMI, HALT, RESUME, RESET, READ_BLOCK, WRITE_BLOCK, RUN, STEP = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08
    RESET_RUN, RESET_RUN_CONFIRM, RESET_HALT = 0, 1, 2

    def __init__(self, hst: h.Host, conn: int, name: str = "oep.target.riscv-dm"):
        self.host, self.conn, self.fn = hst, conn, find(hst, name)

    def _call(self, op: int, body: bytes = b"") -> m.Result:
        r = self.host.request(self.fn, op, bytes([self.conn]) + body)
        if not r.succeeded:
            raise h.Rejected(r)
        return r

    def request(self, op: int, body: bytes = b"") -> tuple[int, int, bytes]:
        """For pipelining by the caller: the raw (fn, op, payload) of one operation."""
        return self.fn, op, bytes([self.conn]) + body

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

    def run(self, pc: int, regs: list[tuple[int, int]], timeout_ms: int = 200) -> tuple[bool, int, int, int]:
        body = struct.pack("<IHB", pc, timeout_ms, len(regs)) + b"".join(struct.pack("<HI", r, v) for r, v in regs)
        stopped, dpc, a0, us = struct.unpack("<BIII", self._call(self.RUN, body).payload)
        return bool(stopped), dpc, a0, us

    def dmi(self, steps: bytes) -> tuple[int, list[int]]:
        p = self._call(self.DMI, steps).payload
        done = struct.unpack_from("<H", p)[0]
        return done, list(struct.unpack_from(f"<{(len(p) - 3) // 4}I", p, 3))

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


@dataclass
class Mark:
    position: int
    kind: int
    time_ms: int
    detail: int


MARK_NAMES = {1: "reset", 2: "restart", 3: "attach", 4: "detach", 5: "lost", 6: "clear", 7: "host", 8: "link-lost"}


class Console:
    """oep.target.console: a stream on the debug connection; reads and marks need no lock."""
    OPEN, READ, MARKS, CLEAR, MARK, WRITE, CLOSE = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
    SDI, DMDATA, DMSEQ = 0, 1, 2
    FROM_POSITION, FROM_OLDEST, FROM_NOW, FROM_MARK = 0, 1, 2, 3

    def __init__(self, hst: h.Host, name: str = "oep.target.console"):
        self.host, self.fn, self.stream = hst, find(hst, name), 1

    def open(self, conn: int, mechanism: int = DMSEQ) -> int:
        self.stream = self.host.request(self.fn, self.OPEN, bytes([conn, mechanism])).payload[0]
        return self.stream

    def read(self, start: int = FROM_OLDEST, arg: int = 0, maximum: int = 1000) -> tuple[int, bool, bool, bytes]:
        """-> (start position, more, gap, data). start: FROM_* ; arg: a position or a mark kind."""
        p = self.host.request(self.fn, self.READ, struct.pack("<BBIH", self.stream, start, arg, maximum),
                              locked=False).payload
        pos, flags = struct.unpack_from("<IB", p)
        return pos, bool(flags & 1), bool(flags & 2), p[5:]

    def read_from(self, position: int, maximum: int = 1000) -> tuple[int, bool, bool, bytes]:
        return self.read(self.FROM_POSITION, position, maximum)

    def marks(self, since: int = 0) -> list[Mark]:
        p = self.host.request(self.fn, self.MARKS, struct.pack("<BI", self.stream, since), locked=False).payload
        return [Mark(*struct.unpack_from("<IBIB", p, 1 + 10 * i)) for i in range(p[0])]

    def clear(self) -> None:
        self.host.request(self.fn, self.CLEAR, bytes([self.stream]))

    def mark(self, value: int) -> None:
        self.host.request(self.fn, self.MARK, bytes([self.stream, value]))

    def write(self, data: bytes) -> int:
        return struct.unpack("<H", self.host.request(self.fn, self.WRITE, bytes([self.stream]) + data).payload)[0]

    def close(self) -> None:
        self.host.request(self.fn, self.CLOSE, bytes([self.stream]))


# ---- pin plan (core plan_apply / plan_release, the v0 shape) ---------------------------------------
OP_PLAN_APPLY, OP_PLAN_RELEASE = 0x04, 0x05


def plan_apply(hst: h.Host, assignments: list[tuple[int, int, int]]) -> None:
    """assignments: (fn, role, channel). All interfaces accept their roles or none is applied. The plan is
    probe state: it stays until plan_release, whatever happens to the session."""
    tlv = b"".join(bytes([0x90, 5]) + struct.pack("<HBH", fn, role, ch) for fn, role, ch in assignments)
    hst.request(m.CORE_FN, OP_PLAN_APPLY, tlv)


def plan_release(hst: h.Host) -> None:
    hst.request(m.CORE_FN, OP_PLAN_RELEASE)


def find_all(hst: h.Host, name: str) -> list[int]:
    """fns of every interface with exactly this name (instances of the same kind, e.g. two UARTs)."""
    out, first = [], 0
    while True:
        total, page = wire.unpack_list_result(
            hst.request(m.CORE_FN, m.OP_LIST, wire.pack_list_request(name, True, first), locked=False).payload)
        out += [e.fn for e in page]
        first += len(page)
        if not page or first >= total:
            return out


# ---- the probe's own declarations (oep.core's describe) -----------------------------------------
def probe_labels(hst: h.Host) -> dict[str, int]:
    """Channel labels the probe declares (core tag 0x46): {"NRST": 23, ...}."""
    data, first = b"", 0
    while True:
        p = hst.request(m.CORE_FN, m.OP_DESCRIBE, struct.pack("<HB", 0, first), locked=False).payload
        chunk = p[1:]
        data += chunk
        first += len(wire.split_tlv(chunk))
        if not p[0] or not chunk:
            break
    return {value[2:].decode("ascii", "replace"): struct.unpack_from("<H", value)[0]
            for tag, value in wire.split_tlv(data) if tag & 0x7F == 0x46}


# ---- byte-stream views for callers that poll read(n) / write(bytes) ----------------------------
class ConsoleIO:
    """A console stream read from a position onwards, as a plain byte stream."""

    def __init__(self, console: Console, start: int | None = None):
        self.console = console
        self.position = console.read(Console.FROM_NOW)[0] if start is None else start
        self.lost = 0

    def read(self, n: int = 512) -> bytes:
        start, more, gap, data = self.console.read_from(self.position, min(n, 1000))
        if gap:
            self.lost += start - self.position
        self.position = start + len(data)
        return data

    def write(self, data: bytes) -> None:
        while data:
            took = self.console.write(data[:64])
            data = data[took:]
            if not took:
                time.sleep(0.005)   # the target has not taken the last chunk yet


class FixtureUartIO:
    """oep.fixture.uart (v0 payloads for now) as a plain byte stream, after a plan gave it RX / TX."""

    def __init__(self, hst: h.Host, fn: int):
        from ..v0 import codec
        self.host, self.fn, self.codec = hst, fn, codec

    def configure(self, baud: int) -> None:
        c = self.codec
        self.host.request(self.fn, c.FIXTURE_UART_OP_CONFIGURE, c.FixtureUartConfigureRequest(baud=baud).pack())

    def read(self, n: int = 512) -> bytes:
        c = self.codec
        return c.FixtureUartReadResult.unpack(self.host.request(
            self.fn, c.FIXTURE_UART_OP_READ, c.FixtureUartReadRequest(maximum=min(n, 480)).pack()).payload).data

    def write(self, data: bytes) -> None:
        c = self.codec
        while data:
            r = c.FixtureUartWriteResult.unpack(self.host.request(
                self.fn, c.FIXTURE_UART_OP_WRITE, c.FixtureUartWriteRequest(data=data[:256]).pack()).payload)
            data = data[r.written:]
            if not r.written:
                time.sleep(0.005)


def attach_after_gpio_reset(hst: h.Host, wire_: Wire, gpio_fn: int, channel: int, exchange, *, tries: int = 10,
                            low_s: float = 0.02) -> tuple[int, int]:
    """For a probe without attach_under_reset: pull `channel` low through oep.fixture.gpio, then send its release
    and an attach (halt) in one exchange so the probe starts the attach right after the release, and retry - a race
    at the edge of the target's reset window (2026-09-24, CH32V003 with SWIO turned off: 2 of 5 pipelined, 0 of 5
    one request at a time). `exchange` is the link's pipelining exchange. -> (connection, DMSTATUS)"""
    from ..v0 import codec
    low, release = 6, 7   # fixture.gpio open-drain low / released (Hi-Z), never driven high
    configure = lambda mode: codec.FixtureGpioConfigureRequest(channel=channel, mode=mode).pack()
    for _ in range(tries):
        hst.request(gpio_fn, codec.FIXTURE_GPIO_OP_CONFIGURE, configure(low))
        time.sleep(low_s)
        results = hst.pipeline([(gpio_fn, codec.FIXTURE_GPIO_OP_CONFIGURE, configure(release)),
                                (wire_.fn, Wire.ATTACH, bytes([1]))], exchange=exchange)
        if results[1].succeeded:
            conn, status = struct.unpack_from("<BI", results[1].payload)
            return conn, status
    raise h.Rejected(results[1])
