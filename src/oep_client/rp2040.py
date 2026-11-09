"""RP2040 target knowledge for the host: its boot ROM's function lookup, the RAM a host-driven call can use, and a reboot
- the flash sequence itself is RP2350's (rp2350.Flash: the pico-sdk's hardware_flash order through the same ROM function
codes). The probe knows none of this.

Facts from the pico-sdk (boot_bootrom_headers/bootrom_constants.h, pico_bootrom/bootrom.h, regs/addressmap.h): the
ROM keeps a halfword pointer to its public function table at 0x14 and to rom_table_lookup(table, code) at 0x18; the
codes are two characters ('I', 'F') = 'I' | 'F' << 8; SRAM is 0x20000000 - 0x20042000. The RP2040 ROM has no reboot
function: the watchdog's trigger restarts the chip (USB included).
"""

from __future__ import annotations

from .arm import CortexM
from . import rp2350

FUNC_TABLE_PTR, TABLE_LOOKUP_PTR = 0x14, 0x18
# RAM the target can spare while the host drives it (SRAM ends at 0x20042000; SRAM4 / 5 above 0x20040000 kept free):
# the return breakpoint, the data buffer for programming, and the stack the ROM functions run on.
BKPT_AT, BUF, BUF_BYTES, STACK_TOP = 0x20030000, 0x20031000, 16 * 1024, 0x20040000
# the watchdog (WATCHDOG_BASE 0x40058000: CTRL, SCRATCH4) and what its reset takes (PSM_BASE 0x40010000: WDSEL), as
# the pico-sdk's watchdog_reboot sets them: everything but ROSC / XOSC, SCRATCH4 0 for a normal boot
WATCHDOG_CTRL, WATCHDOG_SCRATCH4, WATCHDOG_TRIGGER = 0x40058000, 0x4005801C, 1 << 31
PSM_WDSEL, PSM_WDSEL_ALL_BUT_OSCS = 0x40010008, 0x0001FFFF & ~0x3
# TARGETSEL of the multidrop SWD (RP2040 datasheet: core 0, core 1, the rescue DP)
TARGETSEL_CORE0, TARGETSEL_CORE1, TARGETSEL_RESCUE = 0x01002927, 0x11002927, 0xF1002927


def _hword(core: CortexM, at: int) -> int:
    word = core.mem.read32(at & ~3)
    return (word >> (8 * (at & 3))) & 0xFFFF


class Rom:
    def __init__(self, core: CortexM):
        self.core = core
        self._fn: dict[str, int] = {}

    def lookup(self, c: str) -> int:
        if c not in self._fn:
            table, lookup = _hword(self.core, FUNC_TABLE_PTR), _hword(self.core, TABLE_LOOKUP_PTR)
            addr = self.core.call(lookup, (table, rp2350.code(*c)))
            if not 0 < addr < 0x4000:
                raise LookupError(f"ROM has no function {c!r} (lookup returned {addr:#x})")
            self._fn[c] = addr
        return self._fn[c]

    def reboot(self) -> None:
        """The whole chip restarted by the watchdog's trigger, as the pico-sdk's watchdog_reboot(0, 0, 0): WDSEL set to
        everything but the oscillators (left as it comes, the trigger restarted nothing: the core stayed locked up on
        the return breakpoint, 2026-11-10), SCRATCH4 0 (a normal boot), not paused for debug. The core is let go
        first (C_MASKINTS off: it survives every reset but power-on); the USB device re-enumerates."""
        mem = self.core.mem
        mem.write32(WATCHDOG_SCRATCH4, 0)
        mem.write32(PSM_WDSEL, PSM_WDSEL_ALL_BUT_OSCS)
        self.core.release()
        mem.write32(WATCHDOG_CTRL, WATCHDOG_TRIGGER)


def flash(core: CortexM) -> rp2350.Flash:
    return rp2350.Flash(core, Rom(core), BUF, BUF_BYTES)


def core_of(mem) -> CortexM:
    return CortexM(mem, bkpt_at=BKPT_AT, stack_top=STACK_TOP)


def program_and_verify(core: CortexM, image: bytes, offset: int = 0, log=lambda s: None) -> bool:
    return rp2350.program_and_verify(core, image, offset, log, flash(core))
