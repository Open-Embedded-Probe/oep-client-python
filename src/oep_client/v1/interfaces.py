"""What the host knows about interfaces, for display: role names, feature bits, specific tags.

EXAMPLES ONLY. Which capabilities become the BASIC standard under `oep.` is not decided
(oep-spec docs/capability-declaration-model.ja.md); these entries exist so that `dump` can show
what a declaration looks like. An interface missing from here is still listed and described -
with raw role numbers, raw feature bits and raw tag bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from . import wire


def _u16(v: bytes) -> str:
    return str(struct.unpack("<H", v)[0])


def _u32(v: bytes) -> str:
    return str(struct.unpack("<I", v)[0])


def _mv(v: bytes) -> str:
    return f"{struct.unpack('<H', v)[0]} mV"


def _text(v: bytes) -> str:
    return v.decode("ascii", "replace")


def _channels(v: bytes) -> str:
    base = struct.unpack_from("<H", v)[0]
    return ranges(wire.bitmap_to_channels(base, v[2:]))


def _enum(names: dict[int, str]) -> Callable[[bytes], str]:
    return lambda v: names.get(v[0], f"0x{v[0]:02x}")


TRANSPORTS = {1: "RVSWD", 2: "SWIO (1-wire)", 3: "ARM SWD", 4: "JTAG"}
PROGRAM_PATHS = {1: "debug module (DMI)", 2: "SPI", 3: "PIO"}


@dataclass(frozen=True)
class Known:
    summary: str
    roles: dict[int, str] = field(default_factory=dict)
    features: dict[int, str] = field(default_factory=dict)
    # interface tag -> (label, decoder)
    tags: dict[int, tuple[str, Callable[[bytes], str]]] = field(default_factory=dict)


KNOWN: dict[str, Known] = {
    "oep.core": Known("confirmation, list, describe, plan, stop, ping"),
    "oep.probe.identity": Known(
        "the probe itself",
        tags={0x40: ("channels", _u16), 0x41: ("reserved", _channels), 0x42: ("profile", _text)}),
    "oep.target.control": Known(
        "attach, halt, resume, reset",
        features={0: "system reset", 1: "pin reset (NRST wired)", 2: "reset-halt"},
        tags={0x40: ("debug transport", _enum(TRANSPORTS)), 0x41: ("target I/O", _mv)}),
    "oep.target.debug.riscv": Known("RISC-V debug module registers (DMI, abstract commands)"),
    "oep.target.memory": Known("target memory read / write"),
    "oep.target.flash": Known(
        "geometry, erase, program, verify",
        tags={0x40: ("program path", _enum(PROGRAM_PATHS))}),
    "oep.target.console": Known(
        "the target's console over the debug module's data registers",
        features={0: "framing 0 SerialSDI", 1: "framing 1 SerialDMDATA", 2: "framing 2 dmseq"}),
    "oep.fixture.gpio": Known("drive and read probe pins", roles={1: "line"}),
    "oep.fixture.uart": Known("a UART on probe pins", roles={1: "RX", 2: "TX"}),
    "oep.fixture.capture": Known("sampled logic capture", roles={k: f"line{k}" for k in range(8)}),
    "oep.fixture.i2c-target": Known(
        "an I2C target the DUT can address",
        roles={1: "SDA", 2: "SCL"}, features={0: "preloaded tx", 1: "clock stretching"}),
    "oep.fixture.spi-target": Known(
        "an SPI target the DUT can clock",
        roles={1: "SCK", 2: "MOSI", 3: "MISO", 4: "CS"}, features={0: "LSB first"}),
    "io.github.ch32-riscv-ug.p4.i2c-target": Known(
        "ESP32-P4 I2C target extras",
        features={0: "hardware register view"}, tags={0x40: ("max stretch", lambda v: f"{_u32(v)} us")}),
    "io.github.ch32-riscv-ug.p4.spi-target": Known("ESP32-P4 SPI target extras"),
    "io.github.ch32-riscv-ug.ch32.uiapduino-boot": Known(
        "UIAPduino bootloader entry through a RAM payload (QingKe V2)",
        features={0: "enter bootloader", 1: "normalise to user mode"}),
}


def ranges(channels: list[int]) -> str:
    """[0,1,2,5,7,8] -> '0-2,5,7-8'."""
    out, start, prev = [], None, None
    for c in channels:
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = c
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(out) or "-"
