"""In-process fake probes that declare capabilities in the draft wire forms (no hardware).

Each profile is a list of offered interfaces with their describe TLVs. The fake answers the core
operations - confirm, list, describe - by encoding real payloads and paging them to its max_frame,
so what `dump` shows is what a host would decode from a probe of that shape.

The profiles are EXAMPLES of declarations, not a decision about which capabilities are standard.
Wire forms: oep-core §7 (confirm with a revision range, list first / total u16 with oep.core as the first
entry, describe first u16).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import catalog, message as m, names, registry as reg
from .catalog import (CHANNEL_GROUP, FEATURES, IMPLEMENTATION, MAX_CLOCK_HZ, MAX_LENGTH,
                   MIN_CLOCK_HZ, ListEntry)

CORE_FN = 0
OP_CONFIRM, OP_LIST, OP_DESCRIBE = 0x01, 0x02, 0x03
RESULT_HEADER = 5            # role(1) correlation(2) resolution(1) detail(1) in front of every payload
REVISION = 1                 # the protocol revision confirm reports
WINDOW, MAX_INFLIGHT = 4096, 4


@dataclass(frozen=True)
class Offered:
    fn: int
    instance: int
    name: str
    tlvs: tuple[bytes, ...] = ()
    revision: int = 1
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
            if len(payload) < 6 or payload[:4] != m.CONFIRM_REQUEST:
                raise ValueError("fake: confirm needs \"OEP?\" min_rev max_rev")
            if not payload[4] <= REVISION <= payload[5]:
                raise LookupError(f"fake: no revision in {payload[4]}..{payload[5]}")
            return struct.pack("<4sBBHIB", m.CONFIRM_RESULT, REVISION, 0, self.max_frame, WINDOW, MAX_INFLIGHT)
        if op == OP_LIST:
            return self._list(*catalog.unpack_list_request(payload)[:3])
        if op == OP_DESCRIBE:
            if len(payload) < 4:
                raise ValueError("fake: describe needs fn(u16) first(u16)")
            target, first = struct.unpack_from("<HH", payload)
            return self._describe(target, first)
        raise ValueError(f"fake: core op 0x{op:02x} unknown")

    def _list(self, prefix: str, exact: bool, first: int) -> bytes:
        hits = [o for o in self.offered if names.matches(o.name, prefix, exact)]
        budget = self.max_frame - RESULT_HEADER - 3
        page, used = [], 0
        for o in hits[first:]:
            size = len(catalog.pack_entry(self._entry(o)))
            if page and used + size > budget:
                break
            page.append(self._entry(o))
            used += size
        return catalog.pack_list_result(len(hits), page)

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
# Names follow oep-spec docs/capability-name-hierarchy.ja.md (provisional, 2026-09-24): probe-wide
# declarations in oep.core's describe, oep.wire.<link> to scan and attach, oep.target.riscv-dm and
# oep.target.console on the connection, fixtures gpio / uart / capture, the ESP-IDF I2C and SPI
# targets under the project's own name. Probe-wide tags (oep.core, interface-specific 0x40..):
_CORE_TAGS = reg.CORE.tlv["describe"]
CORE_FIRMWARE, CORE_MODEL, CORE_UNIT_ID, CORE_CHANNELS = (_CORE_TAGS[k] for k in ("firmware", "model", "unit_id", "channels"))
CORE_RESERVED, CORE_PROFILE, CORE_LABEL = _CORE_TAGS["reserved"], _CORE_TAGS["profile"], _CORE_TAGS["label"]
CORE_RESETS_ON_OPEN, CORE_TRANSPORT, CORE_OEP_PID = _CORE_TAGS["resets_on_open"], _CORE_TAGS["transport"], _CORE_TAGS["oep_pid"]
TRANSPORT = reg.CORE.enum["transport_kind"]
SERIAL_KINDS = {TRANSPORT["uart_bridge"], TRANSPORT["usb_cdc"], TRANSPORT["usb_serial_jtag"]}
NS = "io.github.ch32-riscv-ug"
MECHANISMS = reg.TARGET_CONSOLE.tlv["describe"]["mechanisms"]
MAX_CONNECTIONS = reg.WIRE_RVSWD.tlv["describe"]["max_connections"]
_CFG = reg.PROBE_CONFIG.tlv["describe"]


def _transports(kinds: list[tuple[int, int]]) -> tuple[bytes, ...]:
    """(kind, USB interface or 0xFF) per transport, index = position (core §7.5)."""
    return tuple(catalog.tlv(CORE_TRANSPORT, bytes([i, k, itf])) for i, (k, itf) in enumerate(kinds))


def _config(fn: int, instance: int, slots_max: int, modes: int = 0b111, storage: int = 4096) -> Offered:
    """oep.probe.config's static declarations; slot_state / bind_state are added live by the endpoint."""
    return Offered(fn, instance, "oep.probe.config", (
        catalog.tlv(_CFG["storage"], struct.pack("<IBII", storage, 0, 0, 20)),
        catalog.tlv(_CFG["items"], bytes(reg.PROBE_CONFIG.tlv["item"].values())),
        catalog.u8(_CFG["slots_max"], slots_max), catalog.u8(_CFG["bind_modes"], modes)))


def _roles(assign: dict[int, list[int]]) -> tuple[bytes, ...]:
    return tuple(catalog.role_channels(r, ch) for r, ch in assign.items())


def _label(channel: int, name: str) -> bytes:
    return catalog.tlv(CORE_LABEL, struct.pack("<H", channel) + name.encode("ascii"))


def _core(firmware: str, model: str, unit_id: bytes, channels: int, reserved: list[int], profile: str,
          labels: dict[int, str], extra: tuple[bytes, ...] = ()) -> Offered:
    base, bits = catalog.channels_to_bitmap(reserved)
    return Offered(0, 0, "oep.core", (
        catalog.text(CORE_FIRMWARE, firmware), catalog.text(CORE_MODEL, model), catalog.tlv(CORE_UNIT_ID, unit_id),
        catalog.u16(CORE_CHANNELS, channels), catalog.tlv(CORE_RESERVED, struct.pack("<H", base) + bits),
        ) + ((catalog.text(CORE_PROFILE, profile),) if profile else ()) + tuple(_label(c, n) for c, n in labels.items())
        + extra)


def p4_x035() -> FakeProbe:
    """ESP32-P4 development probe on the CH32X035F8U6 jig (as wired on 2026-09-24), in the recommended USB shape
    (probe guide §3.8): USB-Serial/JTAG (serial port 0), and on the HS port vendor bulk, HID and a CDC (serial port 3)
    under the OEP VID:PID."""
    reserved = [2, 24, 25, 54]                      # RVSWD SWDIO/SWCLK, USB-Serial/JTAG
    pins = [p for p in range(55) if p not in reserved]
    return FakeProbe("p4-x035", 1024, [
        _core("3.0.0", "esp32-p4-devkit", bytes.fromhex("30eda0e31108"), 55, reserved, f"{NS}.p4-x035",
              {2: "SWDIO", 54: "SWCLK", 51: "LED"},
              _transports([(TRANSPORT["usb_serial_jtag"], 0xFF), (TRANSPORT["vendor_bulk"], 0),
                           (TRANSPORT["hid"], 1), (TRANSPORT["usb_cdc"], 2)]) + (catalog.u8(CORE_OEP_PID, 1),)),
        Offered(1, 1, "oep.wire.rvswd", (
            catalog.channel_group(1, [(1, 2), (2, 54)]), catalog.u32(MAX_CLOCK_HZ, 5_000_000), catalog.u8(IMPLEMENTATION, 1),
            catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 1, "oep.target.riscv-dm", (catalog.u32(FEATURES, 0b1111), catalog.u8(IMPLEMENTATION, 1),
                                              catalog.u16(MAX_LENGTH, 1000))),
        Offered(3, 1, "oep.target.console", (catalog.tlv(MECHANISMS, bytes([0, 1, 2])),)),
        Offered(4, 2, "oep.fixture.gpio", _roles({1: pins})),
        Offered(5, 3, "oep.fixture.uart", _roles({1: pins, 2: pins}) + (
            catalog.u32(MAX_CLOCK_HZ, 3_000_000), catalog.u8(IMPLEMENTATION, 2))),
        Offered(6, 4, "oep.fixture.uart", _roles({1: pins, 2: pins}) + (
            catalog.u32(MAX_CLOCK_HZ, 3_000_000), catalog.u8(IMPLEMENTATION, 2))),
        Offered(7, 5, "oep.fixture.capture", _roles({k: pins for k in range(8)}) + (
            catalog.u32(MAX_CLOCK_HZ, 20_000_000), catalog.u32(MIN_CLOCK_HZ, 1_000),
            catalog.u16(MAX_LENGTH, 65000), catalog.u8(IMPLEMENTATION, 3))),
        Offered(8, 6, f"{NS}.esp32.i2c-target", _roles({1: pins, 2: pins}) + (
            catalog.u16(MAX_LENGTH, 128), catalog.u32(MAX_CLOCK_HZ, 1_000_000),
            catalog.u32(FEATURES, 0b11), catalog.u8(IMPLEMENTATION, 2))),
        Offered(9, 7, f"{NS}.esp32.spi-target", _roles({1: pins, 2: pins, 3: pins, 4: pins}) + (
            catalog.u16(MAX_LENGTH, 64), catalog.u32(MAX_CLOCK_HZ, 3_000_000), catalog.u8(IMPLEMENTATION, 2))),
        _config(10, 8, slots_max=1),
    ])


def esp32_v003() -> FakeProbe:
    """A small probe with 64-byte frames over a 115200 bps UART bridge (its only transport, serial port 0):
    classic ESP32 on a CH32V003 (SWIO) jig."""
    reserved = [0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 15, 16]
    wired = [4, 5, 13, 14, 17, 18, 19, 21, 22, 25, 26, 27, 32, 33]
    return FakeProbe("esp32-v003", 64, [
        _core("3.0.0", "esp32-d0wd", bytes.fromhex("0070070d9394"), 40, reserved, f"{NS}.esp32-v003",
              {16: "SWIO", 23: "NRST", 22: "DUT TX", 21: "DUT RX"},
              _transports([(TRANSPORT["uart_bridge"], 0xFF)])),
        Offered(1, 1, "oep.wire.swio", (catalog.channel_group(1, [(1, 16)]), catalog.role_channels(3, [23]),
                                        catalog.u8(IMPLEMENTATION, 1), catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 1, "oep.target.riscv-dm", (catalog.u32(FEATURES, 0b0111), catalog.u8(IMPLEMENTATION, 1),
                                              catalog.u16(MAX_LENGTH, 40))),
        Offered(3, 1, "oep.target.console", (catalog.tlv(MECHANISMS, bytes([0, 1, 2])),)),
        Offered(4, 2, "oep.fixture.gpio", _roles({1: wired + [23]})),
        Offered(5, 3, "oep.fixture.uart", _roles({1: wired, 2: wired}) + (
            catalog.u32(MAX_CLOCK_HZ, 115_200), catalog.u8(IMPLEMENTATION, 2))),
        Offered(6, 4, "oep.fixture.capture", _roles({k: wired for k in range(4)}) + (
            catalog.u32(MAX_CLOCK_HZ, 2_000_000), catalog.u32(MIN_CLOCK_HZ, 400_000), catalog.u8(IMPLEMENTATION, 1))),
        Offered(7, 5, f"{NS}.esp32.i2c-target", _roles({1: wired, 2: wired}) + (
            catalog.u16(MAX_LENGTH, 16), catalog.u32(MAX_CLOCK_HZ, 100_000), catalog.u8(IMPLEMENTATION, 2))),
        # Two fixed pin sets (an example of channel_group; GPIO23 is the DUT's NRST on this jig).
        Offered(8, 6, f"{NS}.esp32.spi-target", (
            catalog.channel_group(1, [(1, 18), (2, 19), (3, 5), (4, 4)]),
            catalog.channel_group(2, [(1, 14), (2, 13), (3, 27), (4, 26)]),
            catalog.u16(MAX_LENGTH, 32), catalog.u32(MAX_CLOCK_HZ, 3_000_000), catalog.u8(IMPLEMENTATION, 2))),
        _config(9, 7, slots_max=1, modes=0b011, storage=1024),
    ])


def p4_bench() -> FakeProbe:
    """A made-up bench probe with three RVSWD places and two seats (slots, the seat rule and the bind modes can be
    exercised): USB-Serial/JTAG (serial port 0), vendor bulk, HID and a CDC (serial port 3) under the OEP VID:PID."""
    reserved = [2, 3, 4, 5, 6, 7, 24, 25]
    pins = [p for p in range(55) if p not in reserved]
    return FakeProbe("p4-bench", 1024, [
        _core("3.0.0", "esp32-p4-devkit", bytes.fromhex("30eda0e3b001"), 55, reserved, f"{NS}.p4-bench",
              {2: "A SWDIO", 3: "A SWCLK", 4: "B SWDIO", 5: "B SWCLK", 6: "C SWDIO", 7: "C SWCLK"},
              _transports([(TRANSPORT["usb_serial_jtag"], 0xFF), (TRANSPORT["vendor_bulk"], 0),
                           (TRANSPORT["hid"], 1), (TRANSPORT["usb_cdc"], 2)]) + (catalog.u8(CORE_OEP_PID, 1),)),
        Offered(1, 1, "oep.wire.rvswd", (
            catalog.channel_group(1, [(1, 2), (2, 3)]), catalog.channel_group(2, [(1, 4), (2, 5)]),
            catalog.channel_group(3, [(1, 6), (2, 7)]), catalog.u32(MAX_CLOCK_HZ, 5_000_000),
            catalog.u8(IMPLEMENTATION, 1), catalog.u8(MAX_CONNECTIONS, 2))),
        Offered(2, 1, "oep.target.riscv-dm", (catalog.u32(FEATURES, 0b1111), catalog.u8(IMPLEMENTATION, 1),
                                              catalog.u16(MAX_LENGTH, 1000))),
        Offered(3, 1, "oep.target.console", (catalog.tlv(MECHANISMS, bytes([0, 1, 2])),)),
        Offered(4, 2, "oep.fixture.gpio", _roles({1: pins})),
        Offered(5, 3, "oep.fixture.uart", _roles({1: pins, 2: pins}) + (
            catalog.u32(MAX_CLOCK_HZ, 3_000_000), catalog.u8(IMPLEMENTATION, 2))),
        _config(6, 4, slots_max=4),
    ])


def rp2350_pins() -> FakeProbe:
    """A board firmware whose wire takes its pins from the host (oep-if-debug §1, role_channels): any two of GP0-GP29
    but GP19 (the Pro Micro RP2350's PSRAM CS) are an RVSWD pair, a reset line or fixture pins. USB CDC (serial port 0).
    The fake target answers on (0, 1), the first pair."""
    reserved = [19]
    pins = [p for p in range(30) if p not in reserved]
    return FakeProbe("rp2350-pins", 1024, [
        _core("3.0.0", "sparkfun-promicro-rp2350", bytes.fromhex("e66138935f2b1f2c"), 30, reserved, "",
              {}, _transports([(TRANSPORT["usb_cdc"], 0)])),
        Offered(1, 1, "oep.wire.rvswd", _roles({1: pins, 2: pins, 3: pins}) + (
            catalog.u32(MAX_CLOCK_HZ, 5_000_000), catalog.u8(IMPLEMENTATION, 1), catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 1, "oep.target.riscv-dm", (catalog.u32(FEATURES, 0b1111), catalog.u8(IMPLEMENTATION, 1),
                                              catalog.u16(MAX_LENGTH, 1000))),
        Offered(3, 1, "oep.target.console", (catalog.tlv(MECHANISMS, bytes([0, 1, 2])),)),
        Offered(4, 2, "oep.fixture.gpio", _roles({1: pins})),
        Offered(5, 3, "oep.fixture.uart", _roles({1: pins, 2: pins}) + (
            catalog.u32(MAX_CLOCK_HZ, 3_000_000), catalog.u8(IMPLEMENTATION, 2))),
    ])


PROFILES = {"p4-x035": p4_x035, "esp32-v003": esp32_v003, "p4-bench": p4_bench, "rp2350-pins": rp2350_pins}
