"""What the host knows about interfaces, for display: role names, feature bits, specific tags (the ops every describe
carries are named from the registry's op tables: `op_names`).

These entries exist so that `dump` can show what a declaration looks like. An interface missing from here is still
listed and described - with raw role numbers, raw feature bits and raw tag bytes. The core (fn 0) has no name and is
never listed: it is shown under CORE_KEY ("").
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from . import catalog, message as m


def _u16(v: bytes) -> str:
    return str(struct.unpack("<H", v)[0])


def _u32(v: bytes) -> str:
    return str(struct.unpack("<I", v[:4])[0])


def _u32v(v: bytes) -> int:
    return struct.unpack("<I", v[:4])[0]


def _text(v: bytes) -> str:
    return m.shown(v)                                      # control characters and bad UTF-8 replaced (core §2.1)


def _channels(v: bytes) -> str:
    base = struct.unpack_from("<H", v)[0]
    return ranges(catalog.bitmap_to_channels(base, v[2:]))


def _hex(v: bytes) -> str:
    return v.hex()


def _label(v: bytes) -> str:
    return f"{struct.unpack_from('<H', v)[0]} = {m.shown(v[2:])}"


def _capture_mode(v: bytes) -> str:
    """capture describe's mode: mode(u8) max_samples(u32) max_segments(u32) (oep-if-capture §3.5)."""
    mode, most, segments = struct.unpack_from("<BII", v)
    name = {1: "one-shot", 2: "repeat", 3: "streaming"}.get(mode, f"mode {mode}")
    return f"{name}, max {most} samples x {segments} segments"


def _rate_range(v: bytes) -> str:
    """capture describe's rate_range: min_hz(u32) max_hz(u32) exact(u8)."""
    lo, hi, exact = struct.unpack_from("<IIB", v)
    return f"{lo}-{hi} Hz" + (" (any value)" if exact else "")


_TRIGGERS = {0: "immediate", 1: "level", 2: "edge", 3: "cross up", 4: "cross down"}


def _trigger(v: bytes) -> str:
    """capture describe's trigger: types(u32 bit set) max_pretrigger(u32)."""
    types, pre = struct.unpack_from("<II", v)
    return ", ".join(n for b, n in _TRIGGERS.items() if types >> b & 1) + f"; pretrigger up to {pre}"


def _frontend(v: bytes) -> str:
    """analog describe's frontend: frontend(u8) range_min_mv(i32) range_max_mv(i32) attenuation_mdb(u32)."""
    fe, lo, hi, mdb = struct.unpack_from("<BiiI", v)
    return f"{fe}: {lo}..{hi} mV" + (f", {mdb / 1000:g} dB" if mdb not in (0, 0xFFFFFFFF) else "")


def _u32_list(v: bytes) -> str:
    return ", ".join(str(x) for x in struct.unpack(f"<{len(v) // 4}I", v))


_TRANSPORTS = {1: "UART bridge", 2: "USB CDC", 3: "USB-Serial/JTAG", 4: "vendor bulk", 5: "HID", 6: "TCP"}
_MECHANISMS = {0: "SDI", 1: "DMDATA", 2: "dmseq"}


def _transport(v: bytes) -> str:
    itf = "" if len(v) < 3 or v[2] == 0xFF else f" (interface {v[2]})"
    return f"{v[0]} = {_TRANSPORTS.get(v[1], f'kind {v[1]}')}{itf}"


@dataclass(frozen=True)
class Known:
    summary: str
    roles: dict[int, str] = field(default_factory=dict)
    features: dict[int, str] = field(default_factory=dict)
    # interface tag -> (label, decoder)
    tags: dict[int, tuple[str, Callable[[bytes], str]]] = field(default_factory=dict)


CORE_KEY = ""                                   # the core (fn 0): no name (core §0)

KNOWN: dict[str, Known] = {
    CORE_KEY: Known(
        "the core: confirm, list, describe, clock, open / end / keepalive, lock state; describe = the probe itself",
        tags={0x40: ("firmware", _text), 0x41: ("model", _text), 0x42: ("unit id", _text),
              0x43: ("channels", _u16), 0x46: ("label", _label), 0x49: ("transport", _transport),
              0x4C: ("chip", _text), 0x4D: ("max op ms", _u32)}),
    "oep.probe.link": Known("the link test (source, sink) and, when its ops offer it, port_speed on a UART bridge"),
    "oep.probe.plan": Known("the plan: which channel each role of an interface uses (plan_apply, plan_release)",
                            tags={0x40: ("plan roles", _u32)}),
    "oep.probe.restart": Known("the probe restarts itself, answering first",
                               tags={0x40: ("restart max ms", _u32)}),
    "oep.wire.rvswd": Known("scan, attach, detach over RVSWD (attach returns a connection)",
                            roles={1: "SWDIO", 2: "SWCLK", 3: "reset"},
                            tags={0x40: ("max connections", lambda v: str(v[0]))}),
    "oep.wire.swio": Known("scan, attach, detach over SWIO, one wire (attach returns a connection)",
                           roles={1: "SWIO", 3: "reset"},
                           tags={0x40: ("max connections", lambda v: str(v[0]))}),
    "oep.wire.swd": Known("scan, attach, detach over ARM SWD", roles={1: "SWDIO", 2: "SWCLK", 3: "reset"},
                          tags={0x40: ("max connections", lambda v: str(v[0]))}),
    "oep.target.riscv-dm": Known(
        "RISC-V Debug Module over DMI: step lists, block read/write, run until halt, halt/resume (optional ops: ops)"),
    "oep.target.arm-adi": Known("ARM Debug Interface: DP/AP transfer lists, block transfers"),
    "oep.target.console": Known(
        "console streams on a debug connection (position-addressed, marks)",
        tags={0x40: ("mechanisms", lambda v: ", ".join(_MECHANISMS.get(b, str(b)) for b in v))}),
    "oep.probe.config": Known(
        "the probe's configuration (plan, labels, idle pins, slots, binds, uart) and its storage; the state is op state",
        tags={0x40: ("storage", lambda v: f"{_u32(v[:4])} bytes"),
              0x41: ("items", lambda v: ", ".join(str(b) for b in v)), 0x42: ("slots", lambda v: str(v[0]))}),
    "oep.fixture.gpio": Known("drive and read probe pins", roles={1: "line"},
                              tags={0x40: ("modes", lambda v: ", ".join(str(b) for b in range(32) if _u32v(v) >> b & 1)),
                                    0x41: ("drive levels", lambda v: (
                                        ", ".join(f"{i}: ~{ma} mA" for i, ma in enumerate(
                                            struct.unpack_from(f"<{v[1]}H", v, 2))) + f" (default {v[0]})"))}),
    "oep.fixture.uart": Known("a UART (USART, asynchronous) on probe pins", roles={1: "RX", 2: "TX"},
                              tags={0x40: ("formats", lambda v: ", ".join(f"0x{b:02x}" for b in v[1:1 + v[0]]))}),
    "oep.fixture.logic": Known("sampled logic capture", roles={k: f"line{k}" for k in range(8)},
                               tags={0x40: ("mode", _capture_mode), 0x41: ("rate range", _rate_range),
                                     0x44: ("channels", lambda v: str(v[0])), 0x45: ("trigger", _trigger)}),
    "oep.fixture.analog": Known("sampled analog capture", roles={k: f"ch{k}" for k in range(8)},
                                tags={0x40: ("mode", _capture_mode), 0x41: ("rate range", _rate_range),
                                      0x44: ("channels", lambda v: str(v[0])), 0x45: ("trigger", _trigger),
                                      0x46: ("frontend", _frontend)}),
    "oep.fixture.capture-group": Known("captures started and stopped together",
                                       tags={0x40: ("tracks", lambda v: ", ".join(
                                           str(f) for f in struct.unpack_from(f"<{v[0]}H", v, 1)))}),
    "oep.fixture.i2c-target": Known(
        "an I2C target the DUT can address (open-drain only, fixture §3)",
        roles={1: "SDA", 2: "SCL"}, features={2: "internal pull-ups"},
        tags={0x40: ("queue depth", lambda v: str(v[0])), 0x41: ("max stretch us", _u32)}),
    "oep.fixture.spi-target": Known(
        "an SPI target the DUT can clock (ESP-IDF slave driver)",
        roles={1: "SCK", 2: "MOSI", 3: "MISO", 4: "CS"}, features={0: "LSB first"},
        tags={0x40: ("queue depth", lambda v: str(v[0])),
              0x43: ("CS setup ns", lambda v: f"{_u32(v)} (SCK sooner after CS: the first bit is not sure)")}),
}


def op_names(name: str, ops) -> list[str]:
    """The ops of an ops tag by the registry's names for interface `name` (the core's for CORE_KEY; an op it does not
    name: 0x.. hex)."""
    from . import registry as reg
    i = reg.CORE if name == CORE_KEY else reg.INTERFACES.get(name)
    known = {v: k for k, v in getattr(i, "op", {}).items()}
    return [known.get(op, f"0x{op:02x}") for op in sorted(ops)]


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
