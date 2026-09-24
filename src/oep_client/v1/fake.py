"""In-process fake probes that declare capabilities in the draft wire forms (no hardware).

Each profile is a list of offered interfaces with their describe TLVs. The fake answers the core
operations - confirm, list, describe - by encoding real payloads and paging them to its max_frame,
so what `dump` shows is what a host would decode from a probe of that shape.

The profiles are EXAMPLES of declarations, not a decision about which capabilities are standard.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import names, wire
from .wire import (CHANNEL_GROUP, EXCLUSIVE_GROUP, FEATURES, IMPLEMENTATION, MAX_CLOCK_HZ, MAX_LENGTH,
                   MIN_CLOCK_HZ, ListEntry)

CORE_FN = 0
OP_CONFIRM, OP_LIST, OP_DESCRIBE = 0x01, 0x02, 0x03
RESULT_HEADER = 5            # role(1) correlation(2) resolution(1) detail(1) in front of every payload
REVISION = 1                 # the draft's own revision, reported by confirm


@dataclass(frozen=True)
class Offered:
    fn: int
    instance: int
    name: str
    tlvs: tuple[bytes, ...] = ()
    revision: int = 0
    flags: int = 0


class FakeProbe:
    def __init__(self, label: str, max_frame: int, offered: list[Offered]):
        self.label = label
        self.max_frame = max_frame
        self.offered = sorted(offered, key=lambda o: o.fn)
        self.requests = 0
        for o in self.offered:
            names.validate(o.name)

    # The one entry point a transport would call: (fn, op, payload) -> result payload.
    def call(self, fn: int, op: int, payload: bytes = b"") -> bytes:
        self.requests += 1
        if fn != CORE_FN:
            raise ValueError(f"fake: fn {fn} has no operations here")
        if op == OP_CONFIRM:
            return struct.pack("<4sBH", b"OEP!", REVISION, self.max_frame)
        if op == OP_LIST:
            return self._list(*wire.unpack_list_request(payload))
        if op == OP_DESCRIBE:
            target, first = struct.unpack("<HB", payload)
            return self._describe(target, first)
        raise ValueError(f"fake: core op 0x{op:02x} unknown")

    def _list(self, prefix: str, exact: bool, first: int) -> bytes:
        hits = [o for o in self.offered if names.matches(o.name, prefix, exact)]
        budget = self.max_frame - RESULT_HEADER - 2
        page, used = [], 0
        for o in hits[first:]:
            size = len(wire.pack_entry(self._entry(o)))
            if page and used + size > budget:
                break
            page.append(self._entry(o))
            used += size
        return wire.pack_list_result(len(hits), page)

    def _describe(self, fn: int, first: int) -> bytes:
        o = next((x for x in self.offered if x.fn == fn), None)
        if o is None:
            raise ValueError(f"fake: fn {fn} not offered")
        budget = self.max_frame - RESULT_HEADER - 1
        out, sent = b"", 0
        for t in o.tlvs[first:]:
            if out and len(out) + len(t) > budget:
                break
            out += t
            sent += 1
        more = 1 if first + sent < len(o.tlvs) else 0
        return bytes([more]) + out

    @staticmethod
    def _entry(o: Offered) -> ListEntry:
        return ListEntry(o.fn, o.instance, o.revision, o.flags, o.name)


# ---- example profiles ----------------------------------------------------

def _roles(assign: dict[int, list[int]]) -> tuple[bytes, ...]:
    return tuple(wire.role_channels(r, ch) for r, ch in assign.items())


def p4_x035() -> FakeProbe:
    """ESP32-P4 development probe on the CH32X035F8U6 jig (as flashed on 2026-09-24)."""
    reserved = [2, 24, 25, 54]                      # RVSWD SWDIO/SWCLK, USB-Serial/JTAG
    pins = [p for p in range(55) if p not in reserved]
    base, bits = wire.channels_to_bitmap(reserved)
    ns = "io.github.ch32-riscv-ug"
    return FakeProbe("p4-x035", 1024, [
        Offered(0, 0, "oep.core"),
        Offered(1, 1, "oep.probe.identity", (
            wire.u16(0x40, 55), wire.tlv(0x41, struct.pack("<H", base) + bits),
            wire.text(0x42, f"{ns}.p4-devkit"))),
        Offered(2, 2, "oep.target.control", (
            wire.u32(FEATURES, 0b001), wire.u8(IMPLEMENTATION, 1),
            wire.u8(0x40, 1), wire.u16(0x41, 3300))),
        Offered(3, 2, "oep.target.debug.riscv", (wire.u8(IMPLEMENTATION, 1),)),
        Offered(4, 2, "oep.target.memory", (wire.u16(MAX_LENGTH, 1024),)),
        Offered(5, 2, "oep.target.flash", (wire.u8(IMPLEMENTATION, 1), wire.u8(0x40, 1))),
        Offered(6, 2, "oep.target.console", (wire.u32(FEATURES, 0b111),)),
        Offered(7, 3, "oep.fixture.gpio", _roles({1: pins})),
        Offered(8, 4, "oep.fixture.uart", _roles({1: pins, 2: pins}) + (
            wire.u32(MAX_CLOCK_HZ, 3_000_000), wire.u8(IMPLEMENTATION, 2))),
        Offered(9, 5, "oep.fixture.capture", _roles({k: pins for k in range(8)}) + (
            wire.u32(MAX_CLOCK_HZ, 20_000_000), wire.u32(MIN_CLOCK_HZ, 1_000),
            wire.u16(MAX_LENGTH, 65000), wire.u8(IMPLEMENTATION, 3))),
        Offered(10, 6, "oep.fixture.i2c-target", _roles({1: pins, 2: pins}) + (
            wire.u16(MAX_LENGTH, 128), wire.u32(MAX_CLOCK_HZ, 1_000_000),
            wire.u32(FEATURES, 0b11), wire.u8(IMPLEMENTATION, 2), wire.u16(EXCLUSIVE_GROUP, 1))),
        Offered(11, 6, f"{ns}.p4.i2c-target", (wire.u32(FEATURES, 0b1), wire.u32(0x40, 100_000))),
        Offered(12, 7, "oep.fixture.spi-target", _roles({1: pins, 2: pins, 3: pins, 4: pins}) + (
            wire.u16(MAX_LENGTH, 64), wire.u32(MAX_CLOCK_HZ, 3_000_000), wire.u8(IMPLEMENTATION, 2))),
        Offered(13, 7, f"{ns}.p4.spi-target"),
    ])


def esp32_v003() -> FakeProbe:
    """A small bit-bang probe with 64-byte frames: classic ESP32 on a CH32V003 (SWIO) jig."""
    wired = [4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 25, 26, 27, 32, 33]
    ns = "io.github.ch32-riscv-ug"
    return FakeProbe("esp32-v003", 64, [
        Offered(0, 0, "oep.core"),
        Offered(1, 1, "oep.probe.identity", (
            wire.u16(0x40, 40), wire.text(0x42, f"{ns}.esp32-v003"))),
        Offered(2, 2, "oep.target.control", (
            wire.u32(FEATURES, 0b011), wire.u8(IMPLEMENTATION, 1),
            wire.u8(0x40, 2), wire.u16(0x41, 3300))),
        Offered(3, 2, "oep.target.debug.riscv"),
        Offered(4, 2, "oep.target.memory", (wire.u16(MAX_LENGTH, 48),)),
        Offered(5, 2, "oep.target.flash", (wire.u8(IMPLEMENTATION, 1), wire.u8(0x40, 1))),
        Offered(6, 2, "oep.target.console", (wire.u32(FEATURES, 0b111),)),
        Offered(7, 2, f"{ns}.ch32.uiapduino-boot", (wire.u32(FEATURES, 0b11),)),
        Offered(8, 3, "oep.fixture.gpio", _roles({1: wired})),
        Offered(9, 4, "oep.fixture.uart", _roles({1: wired, 2: wired}) + (
            wire.u32(MAX_CLOCK_HZ, 115_200), wire.u8(IMPLEMENTATION, 1))),
        Offered(10, 5, "oep.fixture.i2c-target", _roles({1: wired, 2: wired}) + (
            wire.u16(MAX_LENGTH, 16), wire.u32(MAX_CLOCK_HZ, 100_000), wire.u8(IMPLEMENTATION, 1))),
        # The fast SPI target only on the IO_MUX-native pins: two fixed pin sets.
        Offered(11, 6, "oep.fixture.spi-target", (
            wire.channel_group(1, [(1, 18), (2, 23), (3, 19), (4, 5)]),
            wire.channel_group(2, [(1, 14), (2, 13), (3, 12), (4, 15)]),
            wire.u16(MAX_LENGTH, 32), wire.u32(MAX_CLOCK_HZ, 10_000_000), wire.u8(IMPLEMENTATION, 2))),
    ])


PROFILES = {"p4-x035": p4_x035, "esp32-v003": esp32_v003}
