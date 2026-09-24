"""v1 draft clients for oep.wire.<link> and oep.target.riscv-dm (oep-spec docs/capability-name-hierarchy.ja.md).

The host knows the target; the probe only moves wires and DMI. Everything chip-specific (flash controller,
RAM loaders, register meanings) stays on this side.
"""

from __future__ import annotations

import struct
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
    SCAN, ATTACH, DETACH = 0x01, 0x02, 0x03

    def __init__(self, hst: h.Host, name: str = "oep.wire.rvswd"):
        self.host, self.fn = hst, find(hst, name)

    def scan(self) -> list[Found]:
        p = self.host.request(self.fn, self.SCAN).payload
        out, at = [], 1
        for _ in range(p[0]):
            kind, dio, clk, status = struct.unpack_from("<BHHI", p, at)
            out.append(Found(kind, (dio, clk), status))
            at += 9
        return out

    def attach(self, halt: bool = True) -> tuple[int, int]:
        conn, status = struct.unpack("<BI", self.host.request(self.fn, self.ATTACH, bytes([int(halt)])).payload)
        return conn, status

    def detach(self, conn: int) -> None:
        self.host.request(self.fn, self.DETACH, bytes([conn]))


class RiscvDm:
    DMI, HALT, RESUME, RESET, READ_BLOCK, WRITE_BLOCK, RUN = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07

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
        flags, attempts, pc = struct.unpack("<BBI", self._call(self.RESET, bytes([int(confirm)])).payload)
        return flags, attempts, pc

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
