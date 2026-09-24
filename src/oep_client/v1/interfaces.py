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

from . import catalog


def _u16(v: bytes) -> str:
    return str(struct.unpack("<H", v)[0])


def _u32(v: bytes) -> str:
    return str(struct.unpack("<I", v)[0])


def _text(v: bytes) -> str:
    return v.decode("ascii", "replace")


def _channels(v: bytes) -> str:
    base = struct.unpack_from("<H", v)[0]
    return ranges(catalog.bitmap_to_channels(base, v[2:]))


def _hex(v: bytes) -> str:
    return v.hex()


def _label(v: bytes) -> str:
    return f"{struct.unpack_from('<H', v)[0]} = {v[2:].decode('ascii', 'replace')}"


def _u32_list(v: bytes) -> str:
    return ", ".join(str(x) for x in struct.unpack(f"<{len(v) // 4}I", v))


@dataclass(frozen=True)
class Known:
    summary: str
    roles: dict[int, str] = field(default_factory=dict)
    features: dict[int, str] = field(default_factory=dict)
    # interface tag -> (label, decoder)
    tags: dict[int, tuple[str, Callable[[bytes], str]]] = field(default_factory=dict)


# Names from oep-spec docs/capability-name-hierarchy.ja.md (provisional, 2026-09-24).
KNOWN: dict[str, Known] = {
    "oep.core": Known(
        "confirm, list, describe, open / end / keepalive, lock state, status, cancel; describe = the probe itself",
        tags={0x40: ("firmware", _text), 0x41: ("model", _text), 0x42: ("unit id", _hex),
              0x43: ("channels", _u16), 0x44: ("reserved", _channels), 0x45: ("profile", _text),
              0x46: ("label", _label), 0x47: ("resets on open", lambda v: "yes"),
              0x48: ("uart rates", _u32_list)}),
    "oep.wire.rvswd": Known("scan, attach, detach over RVSWD (attach returns a connection)",
                            roles={1: "SWDIO", 2: "SWCLK"}),
    "oep.wire.swio": Known("scan, attach, detach over SWIO, one wire (attach returns a connection)",
                           roles={1: "SWIO"}),
    "oep.wire.swd": Known("scan, attach, detach over ARM SWD", roles={1: "SWDIO", 2: "SWCLK"}),
    "oep.target.riscv-dm": Known(
        "RISC-V Debug Module over DMI: step lists, block read/write, run until halt, halt/resume",
        features={0: "block read/write (progbuf + autoexec)", 1: "run until halt", 2: "ndmreset", 3: "reset-halt"}),
    "oep.target.arm-adi": Known("ARM Debug Interface: DP/AP transfer lists, block transfers"),
    "oep.target.console": Known(
        "console streams on a debug connection or a UART assignment (position-addressed, marks)",
        features={0: "SDI", 1: "DMDATA", 2: "dmseq", 3: "UART source"},
        tags={0x40: ("buffer", lambda v: f"{_u32(v)} bytes"), 0x41: ("marks", lambda v: str(v[0])),
              0x43: ("max read", _u16)}),
    "oep.fixture.gpio": Known("drive and read probe pins", roles={1: "line"}),
    "oep.fixture.uart": Known("a UART (USART, asynchronous) on probe pins", roles={1: "RX", 2: "TX"}),
    "oep.fixture.capture": Known("sampled logic capture", roles={k: f"line{k}" for k in range(8)}),
    "io.github.ch32-riscv-ug.esp32.i2c-target": Known(
        "an I2C target the DUT can address (ESP-IDF slave driver)",
        roles={1: "SDA", 2: "SCL"}, features={0: "preloaded tx", 1: "clock stretching"}),
    "io.github.ch32-riscv-ug.esp32.spi-target": Known(
        "an SPI target the DUT can clock (ESP-IDF slave driver)",
        roles={1: "SCK", 2: "MOSI", 3: "MISO", 4: "CS"}, features={0: "LSB first"}),
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
