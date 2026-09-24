"""UIAPduino (CH32V003) bootloader entry and return to user mode, from the host with v1 draft parts only.

The knowledge that used to sit in the v0 probe (target.control reset modes 1 and 2) lives here now: two RAM
payloads (wch-protocols E129 / E130, assembled for the V003) that the V003's own CPU runs, because the same
registers written from a halted debug session do not take (E127). The probe only moves a gpio line, writes RAM and
walks DMI steps (oep-spec capability-name-hierarchy.ja.md: NRST is a labelled gpio channel, not a capability).

  enter_bootloader: the bootloader stays only when RCC_RSTSCKR.PINRSTF is set (E161). Read it first: if a pin
                    reset's flag is still there (nobody wrote RMVF since), go in over SWIO alone; otherwise pulse
                    NRST when the jig has it wired, or say what is needed. Then attach halted, place PREPARE_BOOT and
                    resume into it with mstatus = 0 -> HID 1209:b803 in about a second.
                    Only an external NRST pulse sets PINRSTF on the V003: not the IWDG, the WWDG, a software reset,
                    nor the target driving its own PD7 low while the reset function owns the pad (measured
                    2026-09-24, oep-spec experiments/v003-reset-flags); power-on leaves it 0 per the RM.
  normalize_user:   the same with NORMALIZE_USER (clears BOOT_MODE) and no pin pulse
"""

from __future__ import annotations

import struct
import time

from . import host as h, target
from .fixture import Gpio

GPIO_OPEN_DRAIN_LOW, GPIO_OPEN_DRAIN_RELEASE = Gpio.OPEN_DRAIN_LOW, Gpio.OPEN_DRAIN_RELEASE   # for older scripts

PAYLOAD_BASE = 0x20000000
RSTSCKR, PINRSTF = 0x40021024, 1 << 26
DMCONTROL, ABSTRACTCS, COMMAND, DATA0 = 0x10, 0x16, 0x17, 0x04
MSTATUS, DPC = 0x0300, 0x07B1

# kNormalizeUserReset: unlock FLASH, clear BOOT_MODE, PFIC SYSRST
NORMALIZE_USER = [
    0x400222b7, 0x00428293, 0x45670337, 0x12330313, 0x0062a023, 0xcdef9337,
    0x9ab30313, 0x0062a023, 0x400222b7, 0x02428293, 0x45670337, 0x12330313,
    0x0062a023, 0xcdef9337, 0x9ab30313, 0x0062a023, 0x400222b7, 0x02828293,
    0x45670337, 0x12330313, 0x0062a023, 0xcdef9337, 0x9ab30313, 0x0062a023,
    0x400222b7, 0x00c28293, 0x0002a303, 0xffffc3b7, 0xfff38393, 0x00737333,
    0x0062a023, 0xe000e2b7, 0x04828293, 0xbeef0337, 0x08030313, 0x0062a023,
    0x0000006f,
]
# kPrepareBootAndReset: unlock, set BOOT_MODE, PD4 (software USB D-) low for a detach window, PFIC SYSRST
PREPARE_BOOT = [
    0x400222b7, 0x00428293, 0x45670337, 0x12330313, 0x0062a023, 0xcdef9337,
    0x9ab30313, 0x0062a023, 0x400222b7, 0x02428293, 0x45670337, 0x12330313,
    0x0062a023, 0xcdef9337, 0x9ab30313, 0x0062a023, 0x400222b7, 0x02828293,
    0x45670337, 0x12330313, 0x0062a023, 0xcdef9337, 0x9ab30313, 0x0062a023,
    0x400222b7, 0x00c28293, 0x0002a303, 0xffffc3b7, 0xfff38393, 0x00737333,
    0x000043b7, 0x00736333, 0x0062a023, 0x400212b7, 0x01828293, 0x0002a303,
    0x02036313, 0x0062a023, 0x400112b7, 0x40028293, 0x0002a303, 0xfff103b7,
    0xfff38393, 0x00737333, 0x000303b7, 0x00736333, 0x0062a023, 0x400112b7,
    0x41428293, 0x01000313, 0x0062a023, 0x004c52b7, 0xb4028293, 0xfff28293,
    0xfe029ee3, 0xe000e2b7, 0x04828293, 0xbeef0337, 0x08030313, 0x0062a023,
    0x0000006f,
]


def _write_register(dm: target.RiscvDm, regno: int, value: int) -> bytes:
    """DMI steps of one abstract-command register write (aarsize 32, transfer, write), then wait for it."""
    return (dm.step_write(DATA0, value) + dm.step_write(COMMAND, 0x00230000 | regno)
            + dm.step_poll(ABSTRACTCS, 1 << 12, 0, 100))


def run_payload(hst: h.Host, wire: target.Wire, payload: list[int]) -> None:
    """Attach halted, place the payload, resume into it with interrupts off, and let go of the target."""
    conn, _ = wire.attach(halt=True)
    try:
        dm = target.RiscvDm(hst, conn)
        data = struct.pack(f"<{len(payload)}I", *payload)
        dm.write_block(PAYLOAD_BASE, data)
        if dm.read_block(PAYLOAD_BASE, len(payload)) != data:
            raise RuntimeError("payload did not read back")
        # mstatus = 0 first: with MIE set the halted application's SysTick ran over the payload (2026-09-22).
        # resumereq twice, then drop haltreq so the payload's own system reset is not halted again (E129).
        # dmi() raises if an abstract-command poll gave up, so a register write that did not land stops here.
        steps = (_write_register(dm, MSTATUS, 0) + _write_register(dm, DPC, PAYLOAD_BASE)
                 + dm.step_write(DMCONTROL, 0x40000001) + dm.step_write(DMCONTROL, 0x40000001)
                 + dm.step_write(DMCONTROL, 0x00000001))
        dm.dmi(steps)
        time.sleep(0.02)
    finally:
        wire.detach(conn)


def pulse_nrst(hst: h.Host, gpio_fn: int, channel: int, low_s: float = 0.02) -> None:
    """Open-drain low, then released to Hi-Z (never driven high). This drops any debug connection."""
    Gpio(hst, gpio_fn).pulse_low(channel, low_s)


class NeedsPinReset(RuntimeError):
    pass


def pin_reset_flag(hst: h.Host, wire: target.Wire) -> bool:
    conn, _ = wire.attach(halt=True)
    dm = target.RiscvDm(hst, conn)
    try:
        return bool(dm.read32(RSTSCKR) & PINRSTF)
    finally:
        dm.resume()
        wire.detach(conn)


def enter_bootloader(hst: h.Host, wire: target.Wire, gpio_fn: int | None = None,
                     nrst_channel: int | None = None) -> str:
    """-> "swio" (a pin reset's flag was still set) or "nrst" (pulsed). Raises NeedsPinReset otherwise."""
    how = "swio"
    if not pin_reset_flag(hst, wire):
        if gpio_fn is None or nrst_channel is None:
            raise NeedsPinReset("PINRSTF is clear and no NRST line was given: press the board's reset or "
                                "power-cycle it (without the sketch clearing the flags), then try again")
        pulse_nrst(hst, gpio_fn, nrst_channel)
        time.sleep(0.3)
        how = "nrst"
    run_payload(hst, wire, PREPARE_BOOT)
    return how


def normalize_user(hst: h.Host, wire: target.Wire) -> None:
    run_payload(hst, wire, NORMALIZE_USER)
