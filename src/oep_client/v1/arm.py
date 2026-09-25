"""oep.wire.swd and oep.target.arm-adi, revision 1 (oep-spec v1-core-wire-delta §5.4, §5.6).

The probe moves raw DP / AP transfers and MEM-AP blocks; everything above - power-up, SELECT (ADIv5 APSEL/APBANKSEL or
ADIv6 AP addresses), CSW, the Cortex-M debug registers - is here, as target knowledge belongs to the host.
"""

from __future__ import annotations

import struct

from . import host as h, message as m, registry as reg
from .core import Interface
from .riscv import OK, TargetError, WireBase, check, ran, status_name  # noqa: F401

DP_DPIDR = DP_ABORT = 0x0
DP_CTRL_STAT = 0x4
DP_SELECT = 0x8
DP_RDBUFF = 0xC
_ADI = reg.TARGET_ARM_ADI


class SwdWire(WireBase):
    NAME = "oep.wire.swd"
    TAG_TARGETSEL = reg.WIRE_SWD.tlv["attach"]["targetsel"]

    def __init__(self, hst: h.Host):
        super().__init__(hst)
        self.speed_hz = 0
        self.existing = False

    def attach(self, targetsel: int | None = None, max_speed: int | None = None) -> tuple[int, int, bool]:
        """-> (connection, DPIDR, woke from dormant). targetsel (multidrop) and max_speed go as critical TLVs: a probe
        that cannot honour them refuses. self.existing: the wire was attached already (its connection returned)."""
        body = self._speed_tlv(max_speed)
        if targetsel is not None:
            body += m.tlv(self.TAG_TARGETSEL, struct.pack("<I", targetsel), critical=True)
        rd = m.Reader(self._call(self.ATTACH, body).payload)
        conn, dpidr, flags, self.speed_hz = rd.take("BIBI")
        self.existing = bool(flags & 2)
        rd.tail()
        return conn, dpidr, bool(flags & 1)


class AdiError(TargetError):
    pass


def transfer_reads(steps: bytes) -> list[bool]:
    """Per transfer in a packed list: True for a read (1 byte), False for a write (1 + 4 bytes)."""
    out, at = [], 0
    while at < len(steps):
        read = bool(steps[at] & 2)
        out.append(read)
        at += 1 if read else 5
    if at != len(steps):
        raise ValueError("the transfer list ends inside a write")
    return out


class ArmAdi(Interface):
    NAME = "oep.target.arm-adi"
    REVISION = 1
    TRANSFER, READ_BLOCK, WRITE_BLOCK = _ADI.op["transfer"], _ADI.op["read_block"], _ADI.op["write_block"]

    def __init__(self, hst: h.Host, conn: int, adiv6: bool = False):
        super().__init__(hst, prefix=bytes([conn]))
        self.conn = conn
        self.adiv6 = adiv6
        self._select: int | None = None

    # ---- raw transfers ----
    @staticmethod
    def req(ap: bool, read: bool, addr: int, value: int = 0) -> bytes:
        b = bytes([int(ap) | (int(read) << 1) | (((addr >> 2) & 3) << 2)])
        return b if read else b + struct.pack("<I", value)

    def transfer(self, steps: bytes) -> list[int]:
        """A packed transfer list (req() concatenated). -> the values read, in order (an AP read's value arrives one
        transfer late, as on the wire). A list that stopped raises AdiError (status, done, the values it read, and
        self.last_ack = the raw ACK of the last transfer)."""
        reads = transfer_reads(steps)
        r = self._request(self.TRANSFER, struct.pack("<H", len(reads)) + steps)
        rd = ran(r)
        done, status, self.last_ack = rd.take("HBB")
        values = rd.words(sum(reads[:done]))
        rd.tail()
        if status != OK or not r.succeeded or done != len(reads):
            raise AdiError(f"transfer (ack {self.last_ack:#x})", status, r, done=done, values=values)
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
        raise h.OepError(f"no power-up ack: CTRL/STAT {cs:#010x}")


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
        # Words per block operation, from the probe's frame limit: request header 6 + session 4 + connection 1 +
        # address 4 + count 2 on the way in (the answer's 5 + done 2 + status 1 is smaller).
        from .core import confirm
        self.chunk = max(1, (confirm(adi.host)["max_frame"] - 17) // 4)

    def write_many(self, pairs: list[tuple[int, int]]) -> None:
        """Scattered single-word writes in one transfer list (TAR, DRW per word, RDBUFF at the end so the last one
        has landed): one round trip instead of one per word - what a debug-register sequence needs."""
        self.adi._ap_select(self.ap, self.base)
        steps = b"".join(self.adi.req(True, False, self.base + 0x4, a) + self.adi.req(True, False, self.base + 0xC, v)
                         for a, v in pairs)
        self.adi.transfer(steps + self.adi.req(False, True, DP_RDBUFF))

    def read_block(self, address: int, words: int) -> list[int]:
        out, chunk = [], self.chunk
        for off in range(0, words, chunk):
            self.adi._ap_select(self.ap, self.base)
            n = min(chunk, words - off)
            r = self.adi._request(ArmAdi.READ_BLOCK, struct.pack("<IH", address + off * 4, n))
            rd = ran(r)
            done, status = rd.take("HB")
            got = rd.words(done)
            rd.tail()
            if status != OK or not r.succeeded or done != n:
                raise AdiError("read_block", status, r, done=off + done, values=out + got)
            out += got
        return out

    def write_block(self, address: int, values: list[int]) -> None:
        chunk = self.chunk
        for off in range(0, len(values), chunk):
            self.adi._ap_select(self.ap, self.base)
            part = values[off:off + chunk]
            r = self.adi._request(ArmAdi.WRITE_BLOCK, struct.pack("<IH", address + off * 4, len(part))
                                  + struct.pack(f"<{len(part)}I", *part))
            rd = ran(r)
            done, status = rd.take("HB")
            rd.tail()
            if status != OK or not r.succeeded:
                raise AdiError("write_block", status, r, done=off + done)

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
        """Registers for fn(args...): r0-r3, SP, LR to the breakpoint, PC, Thumb bit, no active exception. The
        writes go out as one transfer list; a register write takes the core a few cycles and each SWD transfer
        takes microseconds, so S_REGRDY is checked once at the end rather than after each."""
        xpsr = (self.reg(self.XPSR) | 1 << 24) & ~0x1FF
        regs = [*enumerate(args), (self.SP, self.stack_top), (self.LR, self.bkpt_at | 1), (self.PC, fn & ~1),
                (self.XPSR, xpsr)]
        pairs = [(self.bkpt_at, 0xBE00BE00)]                    # bkpt #0, twice
        for n, value in regs:
            pairs += [(self.DCRDR, value), (self.DCRSR, (1 << 16) | n)]
        self.mem.write_many(pairs)
        self._wait(self.S_REGRDY, 1.0)

    def call(self, fn: int, args=(), timeout: float = 10.0) -> int:
        """Run fn(args...) on the halted core with interrupts masked (their handlers may live in flash that the call
        makes unreadable), wait for the breakpoint, clear the mask, return r0."""
        self.prepare_call(fn, args)
        self.resume(mask_ints=True)
        self._wait(self.S_HALT, timeout)
        self.mem.write32(self.DHCSR, self.KEY | self.C_DEBUGEN | self.C_HALT)   # MASKINTS off while halted
        pc = self.reg(self.PC)
        if pc & ~3 != self.bkpt_at:
            raise h.OepError(f"stopped at {pc:#010x}, not at the return breakpoint")
        return self.reg(0)

    def sys_reset(self) -> None:
        """AIRCR.SYSRESETREQ: the core restarts; debug-domain state (DHCSR) survives, so clear the mask first."""
        self.mem.write32(self.DHCSR, self.KEY | self.C_DEBUGEN | self.C_HALT)
        self.mem.write32(self.AIRCR, 0x05FA0004)
