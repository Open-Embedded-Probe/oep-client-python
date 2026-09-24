"""v1 draft clients for oep.wire.swd and oep.target.arm-adi (op tables provisional, 2026-09-24).

The probe moves raw DP / AP transfers and MEM-AP blocks; everything above - power-up, SELECT (ADIv5 APSEL/APBANKSEL or
ADIv6 AP addresses), CSW, the Cortex-M debug registers - is here, as target knowledge belongs to the host.
"""

from __future__ import annotations

import struct

from . import host as h, message as m, target

DP_DPIDR = DP_ABORT = 0x0
DP_CTRL_STAT = 0x4
DP_SELECT = 0x8
DP_RDBUFF = 0xC
STATUS_NAMES = {0: "ok", 1: "malformed", 2: "FAULT", 3: "no reply", 4: "WAIT"}


class SwdWire(target.Wire):
    def __init__(self, hst: h.Host):
        super().__init__(hst, "oep.wire.swd")

    def scan(self) -> list[target.Found]:
        return super().scan()

    def attach(self, targetsel: int | None = None) -> tuple[int, int, bool]:
        """-> (connection, DPIDR, woke from dormant)."""
        p = self._call(self.ATTACH, b"" if targetsel is None else struct.pack("<I", targetsel)).payload
        conn, dpidr, flags = struct.unpack("<BIB", p[:6])
        return conn, dpidr, bool(flags & 1)


class AdiError(RuntimeError):
    pass


class ArmAdi:
    TRANSFER, READ_BLOCK, WRITE_BLOCK = 0x01, 0x02, 0x03

    def __init__(self, hst: h.Host, conn: int, adiv6: bool = False):
        self.host, self.conn, self.fn = hst, conn, target.find(hst, "oep.target.arm-adi")
        self.adiv6 = adiv6
        self._select: int | None = None

    def _call(self, op: int, body: bytes = b"") -> m.Result:
        r = self.host.request(self.fn, op, bytes([self.conn]) + body)
        if not r.succeeded:
            raise h.Rejected(r)
        return r

    # ---- raw transfers ----
    @staticmethod
    def req(ap: bool, read: bool, addr: int, value: int = 0) -> bytes:
        b = bytes([int(ap) | (int(read) << 1) | (((addr >> 2) & 3) << 2)])
        return b if read else b + struct.pack("<I", value)

    def transfer(self, steps: bytes) -> list[int]:
        p = self._call(self.TRANSFER, steps).payload
        done, status, ack = struct.unpack_from("<HBB", p)
        values = list(struct.unpack_from(f"<{(len(p) - 4) // 4}I", p, 4))
        if status:
            raise AdiError(f"transfer stopped after {done} steps: {STATUS_NAMES.get(status, status)} (ack {ack:#x})")
        return values

    def dp_read(self, addr: int) -> int:
        return self.transfer(self.req(False, True, addr))[0]

    def dp_write(self, addr: int, value: int) -> None:
        self.transfer(self.req(False, False, addr, value))

    def select(self, value: int) -> None:
        if value != self._select:
            self.dp_write(DP_SELECT, value)
            self._select = value

    def _ap_select(self, ap: int, reg: int) -> None:
        """ADIv5: ap = APSEL (0..255), reg = register offset in the AP. ADIv6: ap = the AP's base address."""
        if self.adiv6:
            self.select((ap + reg) & ~0xF)
        else:
            self.select((ap << 24) | (reg & 0xF0))

    def ap_read(self, ap: int, reg: int) -> int:
        self._ap_select(ap, reg)
        return self.transfer(self.req(True, True, reg) + self.req(False, True, DP_RDBUFF))[1]   # posted

    def ap_write(self, ap: int, reg: int, value: int) -> None:
        self._ap_select(ap, reg)
        self.transfer(self.req(True, False, reg, value))

    def power_up(self) -> int:
        """Clear sticky errors, request debug + system power, wait for both acks. -> CTRL/STAT"""
        self.dp_write(DP_ABORT, 0x1E)
        self._select = None
        self.select(0)
        self.dp_write(DP_CTRL_STAT, 0x50000000)
        for _ in range(100):
            cs = self.dp_read(DP_CTRL_STAT)
            if (cs >> 29) & 1 and (cs >> 31) & 1:
                return cs
        raise AdiError(f"no power-up ack: CTRL/STAT {cs:#010x}")


class MemAp:
    """One MEM-AP (ADIv5 APSEL or ADIv6 base address) with 32-bit, auto-incrementing access."""

    def __init__(self, adi: ArmAdi, ap: int, csw_set: int = 0, csw_clear: int = 0):
        """csw_set / csw_clear: target-specific CSW bits (protection, security). The RP2350's AHB-APs come up
        non-secure (CSW bit 30), and its SRAM then faults: pass csw_clear=1 << 30 there (2026-09-24)."""
        self.adi, self.ap = adi, ap
        self.base = 0xD00 if adi.adiv6 else 0x00        # CSW, TAR, DRW at base + 0x0 / 0x4 / 0xC
        csw = adi.ap_read(ap, self.base)
        adi.ap_write(ap, self.base, (((csw & ~0x37) | 0x12) | csw_set) & ~csw_clear)   # 32 bits, AddrInc single
        adi._ap_select(ap, self.base)                        # the bank the block operations assume

    def read_block(self, address: int, words: int) -> list[int]:
        out, chunk = [], 240
        for off in range(0, words, chunk):
            self.adi._ap_select(self.ap, self.base)
            n = min(chunk, words - off)
            p = self.adi._call(ArmAdi.READ_BLOCK, struct.pack("<IH", address + off * 4, n)).payload
            out += struct.unpack(f"<{n}I", p)
        return out

    def write_block(self, address: int, values: list[int]) -> None:
        chunk = 240
        for off in range(0, len(values), chunk):
            self.adi._ap_select(self.ap, self.base)
            part = values[off:off + chunk]
            self.adi._call(ArmAdi.WRITE_BLOCK, struct.pack("<I", address + off * 4) + struct.pack(f"<{len(part)}I", *part))

    def read32(self, address: int) -> int:
        return self.read_block(address, 1)[0]

    def write32(self, address: int, value: int) -> None:
        self.write_block(address, [value])


class CortexM:
    """Armv7-M / Armv8-M core debug through a MEM-AP: halt, resume, core registers through DCRSR / DCRDR, and running
    a function on the target (arguments in r0-r3, LR at a BKPT in RAM, run until the core halts on it) - the way a
    host-side flash algorithm drives the target's own ROM or a RAM loader."""

    DHCSR, DCRSR, DCRDR, AIRCR = 0xE000EDF0, 0xE000EDF4, 0xE000EDF8, 0xE000ED0C
    KEY = 0xA05F0000
    C_DEBUGEN, C_HALT, C_MASKINTS = 1, 2, 8
    S_REGRDY, S_HALT = 1 << 16, 1 << 17
    SP, LR, PC, XPSR = 13, 14, 15, 16

    def __init__(self, mem: MemAp, bkpt_at: int, stack_top: int):
        """bkpt_at: a word of RAM the target does not need (the return breakpoint goes there); stack_top: where the
        called function's stack starts (its RAM below is clobbered)."""
        self.mem, self.bkpt_at, self.stack_top = mem, bkpt_at, stack_top

    def _wait(self, mask: int, timeout: float):
        import time
        deadline = time.monotonic() + timeout
        while True:
            v = self.mem.read32(self.DHCSR)
            if v & mask:
                return v
            if time.monotonic() > deadline:
                raise TimeoutError(f"DHCSR {v:#010x}: waiting for {mask:#x}")

    def halted(self) -> bool:
        return bool(self.mem.read32(self.DHCSR) & self.S_HALT)

    def halt(self) -> None:
        self.mem.write32(self.DHCSR, self.KEY | self.C_DEBUGEN | self.C_HALT)
        self._wait(self.S_HALT, 1.0)

    def resume(self, mask_ints: bool = False) -> None:
        self.mem.write32(self.DHCSR, self.KEY | self.C_DEBUGEN | (self.C_MASKINTS if mask_ints else 0))

    def release(self) -> None:
        """Run, debug off. C_MASKINTS is cleared first: it lives in the debug domain and survives every reset but
        power-on, and firmware left with it set runs without SysTick / USB interrupts (RP2350, 2026-09-24)."""
        self.mem.write32(self.DHCSR, self.KEY | self.C_DEBUGEN | self.C_HALT)
        self.mem.write32(self.DHCSR, self.KEY)

    def reg(self, n: int) -> int:
        self.mem.write32(self.DCRSR, n)
        self._wait(self.S_REGRDY, 1.0)
        return self.mem.read32(self.DCRDR)

    def set_reg(self, n: int, value: int) -> None:
        self.mem.write32(self.DCRDR, value)
        self.mem.write32(self.DCRSR, (1 << 16) | n)
        self._wait(self.S_REGRDY, 1.0)

    def prepare_call(self, fn: int, args=()) -> None:
        """Registers for fn(args...): r0-r3, SP, LR to the breakpoint, PC, Thumb bit, no active exception."""
        self.mem.write32(self.bkpt_at, 0xBE00BE00)              # bkpt #0, twice
        for i, a in enumerate(args):
            self.set_reg(i, a)
        self.set_reg(self.SP, self.stack_top)
        self.set_reg(self.LR, self.bkpt_at | 1)
        self.set_reg(self.PC, fn & ~1)
        self.set_reg(self.XPSR, (self.reg(self.XPSR) | 1 << 24) & ~0x1FF)

    def call(self, fn: int, args=(), timeout: float = 10.0) -> int:
        """Run fn(args...) on the halted core with interrupts masked (their handlers may live in flash that the call
        makes unreadable), wait for the breakpoint, clear the mask, return r0."""
        self.prepare_call(fn, args)
        self.resume(mask_ints=True)
        self._wait(self.S_HALT, timeout)
        self.mem.write32(self.DHCSR, self.KEY | self.C_DEBUGEN | self.C_HALT)   # MASKINTS off while halted
        pc = self.reg(self.PC)
        if pc & ~3 != self.bkpt_at:
            raise AdiError(f"stopped at {pc:#010x}, not at the return breakpoint")
        return self.reg(0)

    def sys_reset(self) -> None:
        """AIRCR.SYSRESETREQ: the core restarts; debug-domain state (DHCSR) survives, so clear the mask first."""
        self.mem.write32(self.DHCSR, self.KEY | self.C_DEBUGEN | self.C_HALT)
        self.mem.write32(self.AIRCR, 0x05FA0004)
