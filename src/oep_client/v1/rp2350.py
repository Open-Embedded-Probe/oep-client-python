"""RP2350 target knowledge for the host: its boot ROM's function table and the ROM-driven flash sequence (the
pico-sdk's hardware_flash order), run on the halted core through CortexM.call. The probe knows none of this.

Measured 2026-09-24 through an RP2040-Zero OEP probe (bit-bang SWD, 500 ns half period) on a Pro Micro RP2350:
98 KiB erased in 0.3 s, programmed in 3.0 s, verified (fast-XIP read) in 2.0 s; ROM reboot re-enumerates the USB.
"""

from __future__ import annotations

import struct
import time

from .arm import CortexM

FLASH_XIP = 0x10000000
SECTOR, BLOCK, BLOCK_ERASE_CMD, PAGE = 4096, 65536, 0xD8, 256
RT_FLAG_FUNC_ARM_SEC = 0x0004
TABLE_LOOKUP_PTR = 0x16          # halfword in the ROM: rom_table_lookup(code, mask) (Arm, RP2350)
# RAM the target can spare while the host drives it (main SRAM ends at 0x20082000): the return breakpoint, the data
# buffer for programming, and the stack the ROM functions run on. Whatever ran before is restarted afterwards.
BKPT_AT, BUF, BUF_BYTES, STACK_TOP = 0x20040000, 0x20041000, 16 * 1024, 0x20080000


def code(a: str, b: str) -> int:
    return ord(a) | ord(b) << 8


class Rom:
    def __init__(self, core: CortexM):
        self.core = core
        self._fn: dict[str, int] = {}

    def lookup(self, c: str) -> int:
        if c not in self._fn:
            word = self.core.mem.read32(TABLE_LOOKUP_PTR & ~3)
            lookup = (word >> (8 * (TABLE_LOOKUP_PTR & 3))) & 0xFFFF
            addr = self.core.call(lookup, (code(*c), RT_FLAG_FUNC_ARM_SEC))
            if not 0 < addr < 0x8000:
                raise LookupError(f"ROM has no function {c!r} (lookup returned {addr:#x})")
            self._fn[c] = addr
        return self._fn[c]

    def reboot(self, delay_ms: int = 10) -> None:
        """reboot(flags=0: normal boot, delay_ms, 0, 0): a whole-chip reboot, so the USB device re-enumerates. The core
        is let go into it with interrupts enabled (its handlers are in flash, readable again by now)."""
        self.core.prepare_call(self.lookup("RB"), (0, delay_ms, 0, 0))
        self.core.release()


class Flash:
    def __init__(self, core: CortexM):
        self.core, self.rom = core, Rom(core)

    def program(self, offset: int, data: bytes, log=lambda s: None) -> bytes:
        """Erase and program at flash `offset` (sector aligned). Leaves the flash in command-XIP mode: reads through
        0x10000000+ work (slowly) for verification; a reboot restores the fast mode. Returns the page-padded image."""
        if offset % SECTOR:
            raise ValueError("offset must be a multiple of the 4 KiB sector")
        data = data + b"\xff" * (-len(data) % PAGE)
        erase = len(data) + (-len(data) % SECTOR)
        fn = {c: self.rom.lookup(c) for c in ("IF", "EX", "RE", "RP", "FC", "CX")}
        t0 = time.monotonic()
        self.core.call(fn["IF"])                                                # connect_internal_flash
        self.core.call(fn["EX"])                                                # flash_exit_xip
        self.core.call(fn["RE"], (offset, erase, BLOCK, BLOCK_ERASE_CMD), timeout=120)   # flash_range_erase
        t1 = time.monotonic()
        for at in range(0, len(data), BUF_BYTES):
            chunk = data[at:at + BUF_BYTES]
            self.core.mem.write_block(BUF, list(struct.unpack(f"<{len(chunk) // 4}I", chunk)))
            self.core.call(fn["RP"], (offset + at, BUF, len(chunk)), timeout=30)   # flash_range_program
        t2 = time.monotonic()
        self.core.call(fn["FC"])                                                # flash_flush_cache
        self.core.call(fn["CX"])                                                # flash_enter_cmd_xip
        log(f"erased {erase} B in {t1 - t0:.2f} s, programmed {len(data)} B in {t2 - t1:.2f} s")
        return data

    def read(self, offset: int, length: int) -> bytes:
        words = self.core.mem.read_block(FLASH_XIP + offset, (length + 3) // 4)
        return struct.pack(f"<{len(words)}I", *words)[:length]


def program_and_verify(core: CortexM, image: bytes, offset: int = 0, log=lambda s: None) -> bool:
    flash = Flash(core)
    padded = flash.program(offset, image, log)
    return flash.read(offset, len(padded)) == padded
