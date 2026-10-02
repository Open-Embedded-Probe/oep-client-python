"""What the host knows about target families, in one table: which wire, the reset vector, how to read whether the reset
line (NRST) exists, the line settings to attach with. The probe knows none of it (oep-if-debug: the host knows the
target). `oep pins` and anything else that meets a target by its target_id read it from here, and oep-spec
docs/target-scan-notes.ja.md records where each fact comes from.

A family is matched by the target_id an attach reports (scheme 1 = WCH DMI 0x7F, a u32 that equals the chip's ESIG ID):
`ids` are (mask, value) pairs on that u32. A family without ids is known by name only (its target_id is not recorded
yet). Reading the option bytes is read-only here: nothing in this module writes them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

SCHEME_WCH_DMI_7F = 1


@dataclass(frozen=True)
class NrstState:
    """The reset line as the option bytes set it. enabled False: the pin is a GPIO and there is no reset line to find."""
    enabled: bool
    detail: str
    raw: int                        # the option word read


@dataclass(frozen=True)
class Family:
    name: str
    wire: str                       # swio, rvswd, swd
    ids: tuple[tuple[int, int], ...] = ()          # (mask, value) on the scheme-1 target_id; () = not recorded
    reset_vector: int = 0           # where an attach under reset stops (dpc)
    max_speed: int | None = None    # Hz to attach / scan with (None: the wire's own)
    idle_clock: str | None = None   # rvswd: how SWCLK rests ("high" / "low"); None: the wire's default
    nrst: Callable | None = field(default=None, compare=False)   # nrst(dm) -> NrstState, the hart halted; None: unknown
    notes: str = ""

    def matches(self, target_id: tuple[int, bytes] | None) -> bool:
        if not target_id or target_id[0] != SCHEME_WCH_DMI_7F or len(target_id[1]) != 4:
            return False
        value = struct.unpack("<I", target_id[1])[0]
        return any(value & mask == want for mask, want in self.ids)


V00X_OPTION = 0x1FFFF800            # word0 = RDPR, nRDPR, USER, nUSER (bytes, little-endian)
V00X_RST_MODE = {0: "128 us", 1: "1 ms", 2: "12 ms"}   # USER bits[4:3]: the NRST ignore window; 3 = PD7 is a GPIO


def v00x_nrst_from_word(word: int) -> NrstState:
    """CH32V00x: USER (byte 2 of the option word) bits[4:3] = RST_MODE: 11 = PD7 is a GPIO, no NRST (as shipped);
    00 / 01 / 10 = NRST on, ignored for 128 us / 1 ms / 12 ms. USER and nUSER must be complements."""
    user, nuser = (word >> 16) & 0xFF, (word >> 24) & 0xFF
    if user ^ nuser != 0xFF:
        return NrstState(False, f"option bytes unreadable as such (USER {user:#04x}, nUSER {nuser:#04x})", word)
    mode = (user >> 3) & 3
    if mode == 3:
        return NrstState(False, f"RST_MODE 11 (USER {user:#04x}): PD7 is a GPIO, no NRST", word)
    return NrstState(True, f"RST_MODE {mode:02b} (USER {user:#04x}): NRST on PD7, {V00X_RST_MODE[mode]} ignore window", word)


def v00x_nrst(dm) -> NrstState:
    """Read (never write) the CH32V00x option word through a halted hart's riscv-dm."""
    return v00x_nrst_from_word(dm.read32(V00X_OPTION))


# The families met on the OEP benches (oep-spec docs/target-scan-notes.ja.md §2). ids: the top 12 bits of the ESIG ID
# name the series (CH32V003 0x003..., CH32X035 0x035...).
FAMILIES: tuple[Family, ...] = (
    Family("ch32v00x", "swio", ids=tuple((0xFFF00000, s << 20) for s in (0x002, 0x003, 0x004, 0x005, 0x006, 0x007)),
           nrst=v00x_nrst,
           notes="NRST is PD7 and off as shipped (RST_MODE 11); the DM freezes DMSTATUS halt / running until "
                 "havereset is acknowledged; a power cycle then attach(halt) stops at dpc 0 ~15 ms after power-on"),
    Family("ch32x035", "rvswd", ids=((0xFFF00000, 0x03500000),), idle_clock="high",
           notes="SWCLK resting low does not connect; reset is ndmreset only (no NRST on the benches)"),
    Family("ch32l103", "rvswd", max_speed=1_000_000, idle_clock="low",
           notes="SWCLK resting high resets the debug link; over 1 MHz fails right after a reset; target_id not recorded"),
    Family("ch32v203", "rvswd", idle_clock="low", notes="no OEP bench yet; target_id not recorded"),
)


def identify(target_id: tuple[int, bytes] | None) -> Family | None:
    """The family whose ids match this target_id (scheme, value), or None."""
    return next((f for f in FAMILIES if f.matches(target_id)), None)


def by_name(name: str) -> Family | None:
    return next((f for f in FAMILIES if f.name == name), None)


def describe_id(target_id: tuple[int, bytes] | None) -> str:
    if not target_id:
        return "no target_id"
    scheme, value = target_id
    if scheme == SCHEME_WCH_DMI_7F and len(value) == 4:
        return f"{struct.unpack('<I', value)[0]:08x} (WCH DMI 0x7F)"
    return f"scheme {scheme} {value.hex()}"
