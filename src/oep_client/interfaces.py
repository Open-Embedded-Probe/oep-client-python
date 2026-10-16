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
    """capture describe's mode: mode(u8) max_samples(u32) max_segments(u32: repeat's; 0 and unused otherwise,
    oep-if-capture §3.5)."""
    mode, most, segments = struct.unpack_from("<BII", v)
    name = {1: "one-shot", 2: "repeat", 3: "streaming"}.get(mode, f"mode {mode}")
    return f"{name}, max {most} samples" + (f" x {segments} segments" if mode == 2 else " a segment")


def _rate_range(v: bytes) -> str:
    """capture describe's rate_range: min_hz(u32) max_hz(u32)."""
    lo, hi = struct.unpack_from("<II", v)
    return f"{lo}-{hi} Hz"


_TRIGGERS = {0: "immediate", 1: "level", 2: "edge", 3: "cross up", 4: "cross down"}


def _trigger(v: bytes) -> str:
    """capture describe's trigger: types(u32 bit set) max_pretrigger(u32)."""
    types, pre = struct.unpack_from("<II", v)
    return ", ".join(n for b, n in _TRIGGERS.items() if types >> b & 1) + f"; pretrigger up to {pre}"


def _frontend(v: bytes) -> str:
    """analog describe's frontend: frontend(u8) range_min_mv(i32) range_max_mv(i32), at the input pin."""
    fe, lo, hi = struct.unpack_from("<Bii", v)
    return f"{fe}: {lo}..{hi} mV"


def _u32_list(v: bytes) -> str:
    return ", ".join(str(x) for x in struct.unpack(f"<{len(v) // 4}I", v))


_TRANSPORTS = {1: "UART bridge", 2: "USB CDC", 3: "USB-Serial/JTAG", 4: "vendor bulk", 5: "HID", 6: "TCP"}


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

def _d(name: str, *pairs) -> dict[int, tuple]:
    """describe tags by their registry names -> (label, render)."""
    from . import registry as reg
    table = (reg.CORE if name == CORE_KEY else reg.INTERFACES[name]).tlv["describe"]
    return {table[key]: (label, render) for key, label, render in pairs}


def _roles(name: str, enum: str, labels: dict[str, str]) -> dict[int, str]:
    from . import registry as reg
    return {reg.INTERFACES[name].enum[enum][k]: v for k, v in labels.items()}


def _bits(name: str, labels: dict[str, str]) -> dict[int, str]:
    """features bits (masks in the registry) -> their positions."""
    from . import registry as reg
    return {reg.INTERFACES[name].enum["features"][k].bit_length() - 1: v for k, v in labels.items()}


_WIRE_ROLES = {"swdio": "SWDIO", "swclk": "SWCLK", "reset": "reset"}
_MAXCONN = ("max_connections", "max connections", lambda v: str(v[0]))
_CAPTURE = (("mode", "mode", _capture_mode), ("rate_range", "rate range", _rate_range),
            ("channels", "channels", lambda v: str(v[0])), ("trigger", "trigger", _trigger))

KNOWN: dict[str, Known] = {
    CORE_KEY: Known(
        "the core: confirm, list, describe, clock, open / end / keepalive, lock state; describe = the probe itself",
        tags=_d(CORE_KEY, ("firmware", "firmware", _text), ("model", "model", _text), ("unit_id", "unit id", _text),
                ("channels", "channels", _u16), ("label", "label", _label), ("transport", "transport", _transport),
                ("chip", "chip", _text), ("max_op_ms", "max op ms", _u32))),
    "oep.probe.link": Known("the link test (source, sink) and, when its ops offer it, port_speed on a UART bridge"),
    "oep.probe.plan": Known("the plan: which channel each role of an interface uses (plan_apply, plan_release)",
                            tags=_d("oep.probe.plan", ("plan_roles", "plan roles", _u32))),
    "oep.probe.restart": Known("the probe restarts itself, answering first",
                               tags=_d("oep.probe.restart", ("restart_max_ms", "restart max ms", _u32))),
    "oep.wire.rvswd": Known("scan, attach, detach over RVSWD (attach returns a connection)",
                            roles=_roles("oep.wire.rvswd", "pin_role", _WIRE_ROLES),
                            tags=_d("oep.wire.rvswd", _MAXCONN)),
    "oep.wire.swio": Known("scan, attach, detach over SWIO, one wire (attach returns a connection)",
                           roles=_roles("oep.wire.swio", "pin_role", {"swdio": "SWIO", "reset": "reset"}),
                           tags=_d("oep.wire.swio", _MAXCONN)),
    "oep.wire.swd": Known("scan, attach, detach over ARM SWD", roles=_roles("oep.wire.swd", "pin_role", _WIRE_ROLES),
                          tags=_d("oep.wire.swd", _MAXCONN)),
    "oep.target.riscv-dm": Known(
        "RISC-V Debug Module over DMI: step lists, block read/write, run until halt, halt/resume (optional ops: ops)"),
    "oep.target.arm-adi": Known("ARM Debug Interface: DP/AP transfer lists, block transfers"),
    "oep.target.console": Known(
        "console streams on a debug connection (position-addressed, marks)",
        tags=_d("oep.target.console", ("mechanism", "mechanism", lambda v: v.decode("utf-8", "replace")))),
    "oep.probe.config": Known(
        "the probe's settings (named items: pins, pass-throughs, network, its own) and their storage; the state is op state",
        tags=_d("oep.probe.config", ("storage", "storage", lambda v: f"{_u32(v[:4])} bytes"),
                ("setting", "setting", lambda v: v[5:].decode("utf-8", "replace") if len(v) > 5 else v.hex()),
                ("netif", "netif", lambda v: v.decode("utf-8", "replace")))),
    "oep.fixture.gpio": Known("drive and read probe pins", roles=_roles("oep.fixture.gpio", "role", {"line": "line"}),
                              tags=_d("oep.fixture.gpio", ("modes", "modes", lambda v: ", ".join(str(b) for b in range(32) if _u32v(v) >> b & 1)))),
    "oep.fixture.uart": Known("a UART (USART, asynchronous) on probe pins",
                              roles=_roles("oep.fixture.uart", "role", {"rx": "RX", "tx": "TX"}),
                              tags=_d("oep.fixture.uart", ("formats", "formats", lambda v: ", ".join(f"0x{b:02x}" for b in v[1:1 + v[0]])))),
    "oep.fixture.logic": Known("sampled logic capture", roles={k: f"line{k}" for k in range(8)},
                               tags=_d("oep.fixture.logic", *_CAPTURE)),
    "oep.fixture.analog": Known("sampled analog capture", roles={k: f"ch{k}" for k in range(8)},
                                tags=_d("oep.fixture.analog", *_CAPTURE, ("frontend", "frontend", _frontend))),
    "oep.fixture.capture-group": Known("captures started at once and stopped together",
                                       tags=_d("oep.fixture.capture-group", ("tracks", "tracks", lambda v: ", ".join(
                                           str(f) for f in struct.unpack_from(f"<{v[0]}H", v, 1))))),
    "oep.fixture.i2c-target": Known(
        "an I2C target the DUT can address (open-drain only, fixture §3)",
        roles=_roles("oep.fixture.i2c-target", "role", {"sda": "SDA", "scl": "SCL"}),
        features=_bits("oep.fixture.i2c-target", {"internal_pullups": "internal pull-ups"}),
        tags=_d("oep.fixture.i2c-target", ("queue_depth", "queue depth", lambda v: str(v[0])),
                ("max_stretch_us", "max stretch us", _u32))),
    "oep.fixture.spi-target": Known(
        "an SPI target the DUT can clock (ESP-IDF slave driver)",
        roles=_roles("oep.fixture.spi-target", "role", {"sck": "SCK", "mosi": "MOSI", "miso": "MISO", "cs": "CS"}),
        features=_bits("oep.fixture.spi-target", {"lsb_first": "LSB first"}),
        tags=_d("oep.fixture.spi-target", ("queue_depth", "queue depth", lambda v: str(v[0])),
                ("modes", "modes", lambda v: ", ".join(str(k) for k in range(4) if v[0] >> k & 1)),
                ("cs_setup_ns", "CS setup ns", lambda v: f"{_u32(v)} (SCK sooner after CS: the first bit is not sure)"))),
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
