"""In-process virtual bench probes that declare capabilities in the draft wire forms (no hardware).

Each profile is a list of offered interfaces with their describe TLVs. The virtual bench answers the core
operations - confirm, list, describe - by encoding real payloads and paging them to its max_frame,
so what `dump` shows is what a host would decode from a probe of that shape.

The profiles are EXAMPLES of declarations.
Wire forms: oep-core §7 (confirm with a revision range, list first / total u16 - fn 0, the core, has no name and is
never an entry - describe first u16). Every profile lists oep.probe.plan (its interfaces have plan roles) and
oep.probe.restart after the fns it had before them. What a probe keeps to itself and does not declare (the channels it
uses for itself, its console's send queue, a capture's sample widths and segment records, a group's budgets) is in
`Offered.inner` and `VirtualProbe.own_channels`.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from . import catalog, message as m, names, registry as reg
from .catalog import CHANNEL_GROUP, FEATURES, MAX_CLOCK_HZ, MAX_LENGTH, MIN_CLOCK_HZ, ListEntry

CORE_FN = 0
OP_CONFIRM, OP_LIST, OP_DESCRIBE = 0x01, 0x02, 0x03
RESULT_HEADER = 5            # role(1) correlation(2) resolution(1) detail(1) in front of every payload
REVISION = 1                 # the protocol revision confirm reports
WINDOW, MAX_INFLIGHT = 4096, 4
BOOT_ID = 0x0EB00001          # the bare VirtualProbe's boot_id (endpoint.Endpoint keeps its own)


@dataclass(frozen=True)
class Offered:
    fn: int
    instance: int
    name: str
    tlvs: tuple[bytes, ...] = ()
    revision: int = 1
    flags: int = 0
    inner: tuple[tuple[str, object], ...] = ()   # what the fn keeps to itself, not declared: (key, value) pairs


CORE_NAME = ""                       # fn 0 is the core: it has no name and is never listed (core §0, §7.2)
WIFI_MAX_TAG = reg.PROBE_CONFIG.tlv["describe"]["wifi_max"]
WIFI_MIN_MAX_FRAME = reg.LIMITS["wifi_min_max_frame"]   # a probe with the wifi item answers at least this (probe.config §1.4)
PLAN_ROLE_INTERFACES = {"oep.fixture.gpio", "oep.fixture.uart", "oep.fixture.i2c-target", "oep.fixture.spi-target",
                        "oep.fixture.logic", "oep.fixture.analog"}   # the interfaces with plan roles (oep-if-plan)
STAND_IN_OPS = (0x01, 0x02)          # the endpoint's two stand-in ops of an fn it does not simulate (VIRTUAL BENCH ONLY)
CORE_REQUIRED = tuple(reg.CORE.op)   # fn 0's ops: every one mandatory, none optional (core §1.2, §12)


def default_ops(fn: int, name: str) -> set[int]:
    """The ops an fn offers when its profile says nothing (core §1.2): fn 0 the core's (all mandatory); an interface
    every op of its table, the optional ones included; one the registry does not know the stand-in ops."""
    if fn == CORE_FN:
        return {reg.CORE.op[k] for k in CORE_REQUIRED}
    i = reg.INTERFACES.get(name)
    return set(i.op.values()) if i is not None else set(STAND_IN_OPS)


def ops_of(name: str, *without: str) -> tuple[bytes, ...]:
    """The ops tag of an fn of interface `name` with every op of its table but the optional ones named in `without`
    (core §1.2, §7.4)."""
    i = reg.INTERFACES[name]
    return (catalog.ops_tlv(v for k, v in i.op.items() if k not in without),)


class VirtualProbe:
    def __init__(self, label: str, max_frame: int, offered: list[Offered], fill_ops: bool = True,
                 own_channels=()):
        """Every offered fn's describe carries the ops tag (core §7.4): one that gives none gets `default_ops`, first;
        oep.probe.restart carries restart_max_ms (RESTART_MAX_MS when it gives none, oep-if-restart §1) (`fill_ops`
        False: left as given - a probe that does not conform, for tests). A probe offering an interface with plan roles
        and no oep.probe.plan gets one at the next free fn (oep-if-plan: it must list one). fn 0 is the core (no name,
        never listed); every other fn has a name. `own_channels`: the channels the probe uses itself (its own UART,
        USB, strapping pins): never an interface's, never parked, not an item's channel (core §8, probe.config §1) -
        not declared."""
        self.label = label
        self.max_frame = max_frame
        self.own_channels = tuple(own_channels)
        offered = list(offered)
        if fill_ops and PLAN_ROLE_INTERFACES & {o.name for o in offered} and not any(o.name == PLAN for o in offered):
            # an interface with plan roles: the probe lists oep.probe.plan too (oep-if-plan), at the next free fn
            offered.append(_plan(max(o.fn for o in offered) + 1))
        self.offered = sorted((o if not fill_ops else self._filled(o) for o in offered), key=lambda o: o.fn)
        self.requests = 0
        for o in self.offered:
            if o.fn != CORE_FN:
                names.validate(o.name)
        if fill_ops and max_frame < WIFI_MIN_MAX_FRAME and any(self.has_wifi(o) for o in self.offered):
            raise ValueError(f"{label}: a probe with the wifi item answers max_frame {WIFI_MIN_MAX_FRAME} or more on every "
                             f"transport (probe.config §1.4), not {max_frame}")

    @staticmethod
    def has_wifi(o: Offered) -> bool:
        """Whether this fn is oep.probe.config with the wifi item (its describe's wifi_max)."""
        return o.name == "oep.probe.config" and any(t[0] & 0x7F == WIFI_MAX_TAG for t in o.tlvs)

    @staticmethod
    def _filled(o: Offered) -> Offered:
        tlvs = o.tlvs
        if not any(t[0] == catalog.OPS for t in tlvs):
            tlvs = (catalog.ops_tlv(default_ops(o.fn, o.name)),) + tlvs
        if o.name == RESTART and not any(t[0] == RESTART_MAX_MS_TAG for t in tlvs):
            tlvs = tlvs + (catalog.u32(RESTART_MAX_MS_TAG, RESTART_MAX_MS),)
        return o if tlvs is o.tlvs else Offered(o.fn, o.instance, o.name, tlvs, o.revision, o.flags, o.inner)

    def instance_errors(self) -> list[str]:
        """Where the offered instances break core §7.2 (C-30): numbered from 0 per (name, revision) in ascending fn
        order. Empty for every profile; a test may make a probe that breaks it on purpose."""
        count: dict[tuple[str, int], int] = {}
        out = []
        for o in self.offered:
            k = (o.name, o.revision)
            if o.instance != count.get(k, 0):
                out.append(f"fn {o.fn} {o.name} rev {o.revision} is instance {o.instance}, not {count.get(k, 0)}")
            count[k] = o.instance + 1
        return out

    # The one entry point a transport would call: (fn, op, payload) -> result payload.
    def call(self, fn: int, op: int, payload: bytes = b"") -> bytes:
        self.requests += 1
        if fn != CORE_FN:
            raise ValueError(f"virtual bench: fn {fn} has no operations here")
        if op == OP_CONFIRM:
            if len(payload) < 6 or payload[:4] != m.CONFIRM_REQUEST:
                raise ValueError("virtual bench: confirm needs \"OEP?\" min_rev max_rev")
            if not payload[4] <= REVISION <= payload[5]:
                raise LookupError(f"virtual bench: no revision in {payload[4]}..{payload[5]}")
            # boot_id, then TLV transport: the index it came on (core §7.1; this bare VirtualProbe: transport 0)
            return (struct.pack("<4sBBHIBI", m.CONFIRM_RESULT, REVISION, 0, self.max_frame, WINDOW, MAX_INFLIGHT, BOOT_ID)
                    + catalog.tlv(reg.CORE.tlv["confirm_answer"]["transport"], b"\x00"))
        if op == OP_LIST:
            return self._list(catalog.unpack_list_request(payload)[0])
        if op == OP_DESCRIBE:
            if len(payload) < 4:
                raise ValueError("virtual bench: describe needs fn(u16) first(u16)")
            target, first = struct.unpack_from("<HH", payload)
            return self._describe(target, first)
        raise ValueError(f"virtual bench: core op 0x{op:02x} unknown")

    def _list(self, first: int) -> bytes:
        """Every interface from the first-th, as many as one frame holds (core §7.2): total is every interface."""
        hits = [o for o in self.offered if o.fn != CORE_FN]
        budget = self.max_frame - RESULT_HEADER - 3
        page, used = [], 0
        for o in hits[first:]:
            size = 1 + len(catalog.pack_entry(self._entry(o)))
            if page and used + size > budget:
                break
            page.append(self._entry(o))
            used += size
        return catalog.pack_list_result(len(hits), page)

    def _describe(self, fn: int, first: int) -> bytes:
        o = next((x for x in self.offered if x.fn == fn), None)
        if o is None:
            raise ValueError(f"virtual bench: fn {fn} not offered")
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
# Probe-wide declarations in fn 0's describe (the core, 0x40..), oep.wire.<link> to scan and attach,
# oep.target.riscv-dm and oep.target.console on the connection, fixtures gpio / uart / capture / i2c / spi targets,
# oep.probe.config, oep.probe.link, and then oep.probe.plan and oep.probe.restart (appended: the fns before them keep
# their numbers). Probe-wide tags:
_CORE_TAGS = reg.CORE.tlv["describe"]
CORE_FIRMWARE, CORE_MODEL, CORE_UNIT_ID, CORE_CHANNELS = (_CORE_TAGS[k] for k in ("firmware", "model", "unit_id", "channels"))
CORE_LABEL, CORE_TRANSPORT, CORE_MAX_OP_MS = _CORE_TAGS["label"], _CORE_TAGS["transport"], _CORE_TAGS["max_op_ms"]
MAX_OP_MS = 10000                                 # what the virtual bench declares: the longest one request may take (core §7.5)
PLAN, RESTART, LINK = reg.PROBE_PLAN.name, reg.PROBE_RESTART.name, reg.PROBE_LINK.name
RESTART_MAX_MS_TAG = reg.PROBE_RESTART.tlv["describe"]["restart_max_ms"]
RESTART_MAX_MS = 2000                             # what the virtual bench declares: back on confirm within this (oep-if-restart §1)
PLAN_ROLES_TAG = reg.PROBE_PLAN.tlv["describe"]["plan_roles"]
PLAN_ROLES = 32                                   # role assignments the virtual bench's plan holds at once (oep-if-plan §1)
GPIO_MODES = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
GPIO_DRIVE_LEVELS = reg.FIXTURE_GPIO.tlv["describe"]["drive_levels"]
DRIVE_LEVELS_MA = (5, 10, 20, 40)     # the virtual bench's selectable output strengths (fixture §1.1), approximate mA, ascending
DRIVE_DEFAULT = 2                     # ... and the default level (about 20 mA)
UART_FORMATS = reg.FIXTURE_UART.tlv["describe"]["formats"]
I2C_QUEUE_DEPTH = reg.FIXTURE_I2C_TARGET.tlv["describe"]["queue_depth"]
I2C_MAX_STRETCH_US = reg.FIXTURE_I2C_TARGET.tlv["describe"]["max_stretch_us"]
SPI_QUEUE_DEPTH = reg.FIXTURE_SPI_TARGET.tlv["describe"]["queue_depth"]
SPI_CS_SETUP_NS = reg.FIXTURE_SPI_TARGET.tlv["describe"]["cs_setup_ns"]
FORMATS_8 = (0x00, 0x04, 0x08, 0x10, 0x14, 0x18)   # 8N1 8E1 8O1 8N2 8E2 8O2: the formats the virtual bench UARTs take
TRANSPORT = reg.CORE.enum["transport_kind"]
SERIAL_KINDS = {TRANSPORT["uart_bridge"], TRANSPORT["usb_cdc"], TRANSPORT["usb_serial_jtag"]}
NS = "io.github.ch32-riscv-ug"
MECHANISMS = reg.TARGET_CONSOLE.tlv["describe"]["mechanisms"]
_CAPD = reg.FIXTURE_LOGIC.tlv["describe"]
_ANAD = reg.FIXTURE_ANALOG.tlv["describe"]
_GRPD = reg.FIXTURE_CAPTURE_GROUP.tlv["describe"]
CORE_CHIP = reg.CORE.tlv["describe"]["chip"]
UNIT_ID = re.compile(r"[a-z0-9-]{1,32}")                    # an x- unit_id is not unique (core §7.5, C-24)


def _analog_decl(frontends: list[tuple[int, int, int, int]], max_samples: int) -> tuple[bytes, ...]:
    """oep.fixture.analog's declarations (oep-if-capture §3.5): one-shot / repeat / streaming, 4 channels, the
    frontends (number, min_mv, max_mv, attenuation_mdb), triggers cross up / down."""
    out = tuple(catalog.tlv(_ANAD["mode"], struct.pack("<BII", _CAPM[k], max_samples, 8 if k == "repeat" else 1))
                for k in ("one_shot", "repeat", "streaming"))
    out += tuple(catalog.tlv(_ANAD["frontend"], struct.pack("<BiiI", *f)) for f in frontends)
    return out + (catalog.u8(_ANAD["channels"], 4),
                  catalog.tlv(_ANAD["trigger"], struct.pack("<II", 0b11001, max_samples - 1)))   # immediate, cross up / down
    # no features: revision 1 defines no bit (query, force, subscribe / unsubscribe: every op offered, the ops tag)


def _group_decl(tracks: list[int]) -> tuple[bytes, ...]:
    """oep.fixture.capture-group's declarations (§4.3): tracks n x fn (force, subscribe / unsubscribe: the ops tag)."""
    return (catalog.tlv(_GRPD["tracks"], struct.pack(f"<B{len(tracks)}H", len(tracks), *tracks)),)
_CAPM = reg.FIXTURE_LOGIC.enum["mode"]


def _capture_decl(modes: list[str], max_channels: int, max_samples: int, ring: int) -> tuple[bytes, ...]:
    """oep.fixture.logic's declarations (oep-if-capture §3.5): a mode entry per mode (mode, max_samples, max_segments),
    channels (max), the trigger types (immediate, level, edge) with the most pretrigger."""
    out = tuple(catalog.tlv(_CAPD["mode"], struct.pack("<BII", _CAPM[k], max_samples, ring if k == "repeat" else 1))
                for k in modes)
    return out + (catalog.u8(_CAPD["channels"], max_channels),
                  catalog.tlv(_CAPD["trigger"], struct.pack("<II", 0b111, max_samples - 1)))   # types u32, max_pretrigger
    # no features: revision 1 defines no bit (query, force, subscribe / unsubscribe: every op offered, the ops tag)


def _multirate_decl(policies: int = 0b111, min_d: int = 2, max_d: int = 1024, pow2: bool = False) -> tuple[bytes, ...]:
    """oep.fixture.logic's multirate (oep-if-capture §5.1): the policies (bit p: policy p), d 2 .. max_d (d 1 always),
    every integer or powers of 2 only."""
    from .multirate import Declared
    return (catalog.tlv(_CAPD["multirate"], Declared(policies, min_d, max_d, pow2).value()),)


def _capture_inner(widths: list[int], ring: int, max_read: int) -> tuple[tuple[str, object], ...]:
    """What a capture keeps to itself (oep-if-capture §1.1, §2, §3.2): the sample widths w it lays channels out in,
    the segment records it keeps, and the most one read returns."""
    return (("widths", tuple(widths)), ("ring", ring), ("max_read", max_read))
MAX_CONNECTIONS = reg.WIRE_RVSWD.tlv["describe"]["max_connections"]
_CFG = reg.PROBE_CONFIG.tlv["describe"]


BLOCK_HEADERS = 24   # write_block's request (10 + connection 2 + address 4 + count 2) and read_block's answer (5 + done 2
                     # + status 1) need 18; the reference firmware keeps 24 (a probe may declare less, oep-if-debug §4.5)


def block_max_length(max_frame: int) -> int:
    """The max_length (bytes, a multiple of 4) a probe with read_block / write_block declares for its max_frame
    (oep-if-debug §4.5): max_frame - 24 rounded down to a word, as the reference firmware does - a read's answer and a
    write's request both fit one frame. The host takes this value; it never computes a block size from max_frame."""
    return max(0, (max_frame - BLOCK_HEADERS) // 4 * 4)


def _transports(kinds: list[tuple[int, int]]) -> tuple[bytes, ...]:
    """(kind, interface) per transport, index = position (core §7.5, C-41): a USB CDC names its communication
    interface (the first of the function), built-in USB serial the number the hardware presents or 0xFF, vendor bulk
    and HID their interface; a UART bridge and TCP 0xFF."""
    for k, itf in kinds:
        assert itf == 0xFF or k not in (TRANSPORT["uart_bridge"], TRANSPORT["tcp"]), "core §7.5: 0xFF for a UART bridge / TCP"
    return tuple(catalog.tlv(CORE_TRANSPORT, bytes([i, k, itf])) for i, (k, itf) in enumerate(kinds))


def _config(fn: int, instance: int, slots_max: int, storage: int = 4096, wifi_max: int = 0) -> Offered:
    """oep.probe.config's declarations (probe.config §4: declarations only; the state is the endpoint's op state):
    storage 0 = none - no storage tag, and save / erase not in its ops (probe.config §2). wifi_max > 0: the wifi item
    too, with up to that many entries (describe wifi_max), and state's wifi TLV."""
    from . import config as cfgmod
    items = [v for k, v in cfgmod.ITEM.items() if k != "wifi" or wifi_max]
    return Offered(fn, instance, "oep.probe.config", (
        ops_of("oep.probe.config") if storage else ops_of("oep.probe.config", "save", "erase"))
        + ((catalog.u32(_CFG["storage"], storage),) if storage else ()) + (
        catalog.tlv(_CFG["items"], bytes(items)),
        catalog.u8(_CFG["slots_max"], slots_max))
        + ((catalog.u8(cfgmod.DESCRIBE["wifi_max"], wifi_max),) if wifi_max else ()))


def drive_levels_tlv(default: int = DRIVE_DEFAULT, ma=DRIVE_LEVELS_MA) -> bytes:
    """oep.fixture.gpio's drive_levels (fixture §1.1, tag 0x41): default(u8) n(u8) n x ma(u16), ascending."""
    return catalog.tlv(GPIO_DRIVE_LEVELS, struct.pack(f"<BB{len(ma)}H", default, len(ma), *ma))


def _gpio(fn: int, channels: list[int], modes: int = 0x7F, drive: bool = True) -> Offered:
    """oep.fixture.gpio: role 1 on `channels`, the modes it drives (u32 bit set, fixture §1), and the output strengths
    it can select (drive_levels, fixture §1.1: DRIVE_LEVELS_MA, default DRIVE_DEFAULT) unless `drive` is False."""
    return Offered(fn, 0, "oep.fixture.gpio", _roles({1: channels}) + (catalog.u32(GPIO_MODES, modes),)
                   + ((drive_levels_tlv(),) if drive else ()))


def _uart(fn: int, instance: int, channels: list[int], max_hz: int, formats=FORMATS_8) -> Offered:
    """oep.fixture.uart: RX / TX on `channels`, the formats it takes (n(u8) n x u8; 8N1 always, fixture §2)."""
    return Offered(fn, instance, "oep.fixture.uart", _roles({1: channels, 2: channels}) + (
        catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.tlv(UART_FORMATS, bytes([len(formats)]) + bytes(formats))))


def _i2c_target(fn: int, channels: list[int], max_length: int, max_hz: int, features: int, queue_depth: int,
                max_stretch_us: int = 0) -> Offered:
    """oep.fixture.i2c-target (fixture §3): SDA / SCL on `channels`, max_length (bytes a frame), max_clock_hz, features
    (bit2 internal pull-ups), queue_depth (frames it keeps and preload slots, tag 0x40 u8), max_stretch_us (tag 0x41
    u32: the most stretch accepts) - declared exactly when the optional op stretch is offered (its ops tag)."""
    assert not features & 0b11, "features bit0 / bit1 are reserved (stretch is an op, declared by ops)"
    ops = ops_of("oep.fixture.i2c-target") if max_stretch_us else ops_of("oep.fixture.i2c-target", "stretch")
    return Offered(fn, 0, "oep.fixture.i2c-target", ops + _roles({1: channels, 2: channels}) + (
        catalog.u16(MAX_LENGTH, max_length), catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.u32(FEATURES, features),
        catalog.u8(I2C_QUEUE_DEPTH, queue_depth))
        + ((catalog.u32(I2C_MAX_STRETCH_US, max_stretch_us),) if max_stretch_us else ()))


def _spi_decl(max_length: int, max_hz: int, features: int, queue_depth: int,
              cs_setup_ns: int = 0) -> tuple[bytes, ...]:
    """oep.fixture.spi-target's declarations besides its pins (fixture §4): max_length (bytes a transaction),
    max_clock_hz, features (bit0 LSB first), queue_depth (tag 0x40 u8), and cs_setup_ns (tag 0x43 u32: the worst
    CS-active-to-first-SCK time for which MISO carries the first bit - declared by a probe that drives MISO in software
    once it sees CS; 0 = left out, MISO driven at once)."""
    return ((catalog.u16(MAX_LENGTH, max_length), catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.u32(FEATURES, features),
             catalog.u8(SPI_QUEUE_DEPTH, queue_depth))
            + ((catalog.u32(SPI_CS_SETUP_NS, cs_setup_ns),) if cs_setup_ns else ()))


CONSOLE_SEND_QUEUE = 256      # each console stream's send queue, bytes: the probe's own size (console §2, not declared)


def _console(fn: int, mechanisms=(0, 1, 2), send_queue: int = CONSOLE_SEND_QUEUE) -> Offered:
    """oep.target.console: the mechanisms it opens (console §1), and - kept to itself - the send queue a stream of
    DMDATA / dmseq has (§2)."""
    return Offered(fn, 0, "oep.target.console", (catalog.tlv(MECHANISMS, bytes(mechanisms)),),
                   inner=(("send_queue", send_queue),))


def _link(fn: int, port_speed: bool = False) -> Offered:
    """oep.probe.link (oep-if-link): source and sink, and port_speed when `port_speed` (a probe on a UART bridge)."""
    return Offered(fn, 0, LINK, ops_of(LINK) if port_speed else ops_of(LINK, "port_speed"))


def _plan(fn: int, plan_roles: int = PLAN_ROLES) -> Offered:
    """oep.probe.plan (oep-if-plan): plan_apply, plan_release, and plan_roles - the most role assignments at once."""
    return Offered(fn, 0, PLAN, (catalog.u32(PLAN_ROLES_TAG, plan_roles),))


def _restart(fn: int, restart_max_ms: int = RESTART_MAX_MS) -> Offered:
    """oep.probe.restart (oep-if-restart): restart, and restart_max_ms - back on confirm within this after the answer."""
    return Offered(fn, 0, RESTART, (catalog.u32(RESTART_MAX_MS_TAG, restart_max_ms),))


def _roles(assign: dict[int, list[int]]) -> tuple[bytes, ...]:
    return tuple(catalog.role_channels(r, ch) for r, ch in assign.items())


def _label(channel: int, name: str) -> bytes:
    return catalog.tlv(CORE_LABEL, struct.pack("<H", channel) + name.encode("ascii"))


def _core(firmware: str, model: str, unit_id: str, channels: int, labels: dict[int, str],
          extra: tuple[bytes, ...] = ()) -> Offered:
    """fn 0's declarations (core §7.5): firmware and model (free text), the required unit_id, transport (in `extra`)
    and max_op_ms, the channel count, the firmware's fixed labels. fn 0 is the core: no name, never listed."""
    assert UNIT_ID.fullmatch(unit_id), "core §7.5: unit_id grammar"
    return Offered(0, 0, CORE_NAME, (
        catalog.text(CORE_FIRMWARE, firmware), catalog.text(CORE_MODEL, model), catalog.text(CORE_UNIT_ID, unit_id),
        catalog.u16(CORE_CHANNELS, channels), catalog.u32(CORE_MAX_OP_MS, MAX_OP_MS),
        ) + tuple(_label(c, n) for c, n in labels.items()) + extra)


def p4_x035() -> VirtualProbe:
    """ESP32-P4 development probe on the CH32X035F8U6 jig (as wired on 2026-09-24), in the recommended USB shape
    (probe guide §8): USB-Serial/JTAG (serial port 0), and on the HS port vendor bulk, HID and a CDC (serial port 3)
    on the project's VID:PID."""
    own = [24, 25]                                  # USB-Serial/JTAG (the probe's own: never an interface's)
    pins = [p for p in range(55) if p not in own + [2, 54]]   # the fixtures' pins: all but the RVSWD pair
    return VirtualProbe("p4-x035", 1024, [
        _core("3.0.0", "esp32p4", "fafe00000035", 55, {2: "SWDIO", 54: "SWCLK", 51: "LED"},
              _transports([(TRANSPORT["usb_serial_jtag"], 0xFF), (TRANSPORT["vendor_bulk"], 0),
                           (TRANSPORT["hid"], 1), (TRANSPORT["usb_cdc"], 2)])
              + (catalog.text(CORE_CHIP, "esp32p4 v1.0"),)),
        Offered(1, 0, "oep.wire.rvswd", (
            catalog.channel_group(1, [(1, 2), (2, 54)]), catalog.u32(MAX_CLOCK_HZ, 5_000_000),
            catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(MAX_LENGTH, block_max_length(1024)),)),   # every op
        _console(3),
        _gpio(4, pins),
        _uart(5, 0, pins, 3_000_000),
        _uart(6, 1, pins, 3_000_000),
        # the P4's PARLIO: w 1-16, one-shot / repeat / streaming
        Offered(7, 0, "oep.fixture.logic", _roles({k: pins for k in range(16)}) + (
            catalog.u32(MAX_CLOCK_HZ, 20_000_000), catalog.u32(MIN_CLOCK_HZ, 1_000),
            catalog.u16(MAX_LENGTH, 65000))
            + _capture_decl(["one_shot", "repeat", "streaming"], 16, 1 << 20, 8) + _multirate_decl(),
            inner=_capture_inner([1, 2, 4, 8, 16], 8, 4096)),
        _i2c_target(8, pins, max_length=128, max_hz=1_000_000, features=0, queue_depth=8,
                    max_stretch_us=100_000),                                                       # stretch
        Offered(9, 0, "oep.fixture.spi-target", _roles({1: pins, 2: pins, 3: pins, 4: pins})
                + _spi_decl(max_length=64, max_hz=3_000_000, features=0b1, queue_depth=8)),             # LSB first
        _config(10, 0, slots_max=1),
        # the P4's ADC1 (GPIO16-23): one ADC for all its channels, 611 Hz - 83.3 kHz in all; ESP32-style attenuations
        Offered(11, 0, "oep.fixture.analog", _roles({k: list(range(16, 24)) for k in range(4)}) + (
            catalog.u32(MAX_CLOCK_HZ, 83_333), catalog.u32(MIN_CLOCK_HZ, 611))
            + _analog_decl([(0, 0, 950, 0), (1, 0, 1250, 2500), (2, 0, 1750, 6000), (3, 0, 3100, 12000)], 65536),
            inner=_capture_inner([16], 8, 4096)),
        # the logic (fn 7) and the analog (fn 11) together; the ADC's 83.3 kHz is shared by its channels (the probe's
        # own budget: a bind past it is refused unavailable, capture §4.2)
        Offered(12, 0, "oep.fixture.capture-group", _group_decl([7, 11]),
                inner=(("max_tracks", 2), ("budgets", ((83_333, (11,)),)))),
        _link(13),
        _plan(14),
        _restart(15),
    ], own_channels=own)


def esp32_v003(max_frame: int = 512) -> VirtualProbe:
    """A classic ESP32 on a CH32V003 (SWIO) jig over a UART bridge (its only transport, serial port 0), with the reference
    classic ESP32 firmware's frame limits: max_frame 512 and oep.target.riscv-dm's max_length 488 (max_frame - 24 rounded
    down to a word, oep-if-debug §4.5), so a host splits its block ops as it does on that probe (a 9168-byte image: 19
    write_blocks of up to 122 words, not 230 of 10). oep.probe.link with port_speed (oep-if-link §3), as that firmware.
    Its channels and the other interfaces' limits are this profile's own. `max_frame`: the same probe at another frame
    size (esp32_v003_64: the smallest a probe may declare). oep.probe.config has the wifi item (wifi_max 4), as the
    reference firmware on a classic ESP32 (its TCP transport is not part of this profile: `virtual_bench_serve --tcp`),
    when max_frame is wifi_min_max_frame (112) or more (probe.config §1.4); below that, no wifi item."""
    own = [0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 15]   # UART0, strapping, flash: the probe's own (SWIO 16 is the wire's)
    wired = [4, 5, 13, 14, 17, 18, 19, 21, 22, 25, 26, 27, 32, 33]
    return VirtualProbe("esp32-v003" if max_frame == 512 else f"esp32-v003-{max_frame}", max_frame, [
        _core("3.0.0", "esp32", "fafe00000003", 40, {16: "SWIO", 23: "NRST", 22: "DUT TX", 21: "DUT RX"},
              _transports([(TRANSPORT["uart_bridge"], 0xFF)])),
        Offered(1, 0, "oep.wire.swio", (catalog.channel_group(1, [(1, 16)]), catalog.role_channels(3, [23]),
                                        catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 0, "oep.target.riscv-dm", ops_of("oep.target.riscv-dm", "step") + (    # no step
            catalog.u16(MAX_LENGTH, block_max_length(max_frame)),)),
        _console(3),
        _gpio(4, wired + [23]),
        _uart(5, 0, wired, 115_200),
        # the classic ESP32's GPIO sampler: a byte a sample (w 8), one-shot
        Offered(6, 0, "oep.fixture.logic", _roles({k: wired for k in range(4)}) + (
            catalog.u32(MAX_CLOCK_HZ, 2_000_000), catalog.u32(MIN_CLOCK_HZ, 400_000))
            + _capture_decl(["one_shot"], 8, 65536, 1), inner=_capture_inner([8], 1, 480)),
        _i2c_target(7, wired, max_length=16, max_hz=100_000, features=0, queue_depth=4),       # no stretch
        # Two fixed pin sets (an example of channel_group; GPIO23 is the DUT's NRST on this jig); MSB first only.
        Offered(8, 0, "oep.fixture.spi-target", (
            catalog.channel_group(1, [(1, 18), (2, 19), (3, 5), (4, 4)]),
            catalog.channel_group(2, [(1, 14), (2, 13), (3, 27), (4, 26)]))
            + _spi_decl(max_length=32, max_hz=3_000_000, features=0, queue_depth=4, cs_setup_ns=4000)),   # MISO in software
        # the classic ESP32 joins Wi-Fi from its settings - at a max_frame that carries the longest wifi set (112,
        # probe.config §1.4); below that (esp32-v003-64) the same probe without the wifi item
        _config(9, 0, slots_max=1, storage=1024, wifi_max=4 if max_frame >= WIFI_MIN_MAX_FRAME else 0),
        _link(10, port_speed=True),
        _plan(11),
        _restart(12),
    ], own_channels=own)


def esp32_v003_64() -> VirtualProbe:
    """esp32-v003 at the smallest max_frame a probe may declare (64, reg.MIN_MAX_FRAME): list and describe paged, a
    riscv-dm max_length of 40, every answer cut to 64 bytes - what a host must still work with. No wifi item: a probe
    with it answers max_frame 112 or more (probe.config §1.4)."""
    return esp32_v003(reg.MIN_MAX_FRAME)


def p4_bench() -> VirtualProbe:
    """A made-up bench probe with three RVSWD places and two seats (slots, the seat rule and the binds can be
    exercised): USB-Serial/JTAG (serial port 0), vendor bulk, HID and a CDC (serial port 3) on the project's VID:PID."""
    own = [24, 25]                                  # USB-Serial/JTAG (the wires' pins 2-7 are interfaces')
    pins = [p for p in range(55) if p not in own + list(range(2, 8))]
    return VirtualProbe("p4-bench", 1024, [
        _core("3.0.0", "esp32p4", "30eda0e3b001", 55,
              {2: "A SWDIO", 3: "A SWCLK", 4: "B SWDIO", 5: "B SWCLK", 6: "C SWDIO", 7: "C SWCLK"},
              _transports([(TRANSPORT["usb_serial_jtag"], 0xFF), (TRANSPORT["vendor_bulk"], 0),
                           (TRANSPORT["hid"], 1), (TRANSPORT["usb_cdc"], 2)])),
        Offered(1, 0, "oep.wire.rvswd", (
            catalog.channel_group(1, [(1, 2), (2, 3)]), catalog.channel_group(2, [(1, 4), (2, 5)]),
            catalog.channel_group(3, [(1, 6), (2, 7)]), catalog.u32(MAX_CLOCK_HZ, 5_000_000),
            catalog.u8(MAX_CONNECTIONS, 2))),
        Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(MAX_LENGTH, block_max_length(1024)),)),
        _console(3),
        _gpio(4, pins),
        _uart(5, 0, pins, 3_000_000),
        _config(6, 0, slots_max=4),
        _link(7),
        _plan(8),
        _restart(9),
    ], own_channels=own)


def rp2350_pins() -> VirtualProbe:
    """A board firmware whose wire takes its pins from the host (oep-if-debug §1, role_channels): any two of GP0-GP29
    but GP19 (the Pro Micro RP2350's PSRAM CS) are an RVSWD pair, a reset line or fixture pins. USB CDC (serial port 0)
    on the project's VID:PID.
    The virtual target answers on (0, 1), the first pair."""
    own = [19]
    pins = [p for p in range(30) if p not in own]
    return VirtualProbe("rp2350-pins", 1024, [
        _core("3.0.0", "rp2350", "e66138935f2b1f2c", 30, {}, _transports([(TRANSPORT["usb_cdc"], 0)])),
        Offered(1, 0, "oep.wire.rvswd", _roles({1: pins, 2: pins, 3: pins}) + (
            catalog.u32(MAX_CLOCK_HZ, 5_000_000), catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(MAX_LENGTH, block_max_length(1024)),)),
        _console(3),
        _gpio(4, pins),
        _uart(5, 0, pins, 3_000_000),
        _link(6),
        _plan(7),
        _restart(8),
    ], own_channels=own)


STAND_IN = f"{NS}.stand-in"


def _again(probe: VirtualProbe, offered: list[Offered]) -> VirtualProbe:
    """The same probe (label, max_frame, own channels) with other fns."""
    return VirtualProbe(probe.label, probe.max_frame, offered, own_channels=probe.own_channels)


def _with(o: Offered, tlvs) -> Offered:
    return Offered(o.fn, o.instance, o.name, tuple(tlvs), o.revision, o.flags, o.inner)


def without(probe: VirtualProbe, name: str) -> VirtualProbe:
    """The profile without its fns of interface `name` (`virtual_bench_serve --no-restart`: a probe without the optional
    oep.probe.restart, which then lists none and answers unknown_function on its old fn). The other fns keep their
    numbers. VIRTUAL BENCH ONLY."""
    return _again(probe, [o for o in probe.offered if o.name != name])


def with_unit_id(probe: VirtualProbe, unit_id: str) -> VirtualProbe:
    """The profile with another unit_id in fn 0's describe - an `x-` one is a probe with neither a unique number nor
    storage (core §7.5, C-24: hosts key nothing kept across sessions by it). VIRTUAL BENCH ONLY."""
    return _again(probe, [_with(o, (catalog.text(CORE_UNIT_ID, unit_id) if t[0] == CORE_UNIT_ID else t for t in o.tlvs))
                          if o.fn == CORE_FN else o for o in probe.offered])


def unit_id_of(probe: VirtualProbe) -> str:
    """The unit_id fn 0's describe declares (core §7.5)."""
    for o in probe.offered:
        if o.fn == CORE_FN:
            for t in o.tlvs:
                if t[0] == CORE_UNIT_ID:
                    return t[3:].decode("ascii")
    raise LookupError("no unit_id in fn 0's describe")


def with_stand_in(probe: VirtualProbe) -> VirtualProbe:
    """The profile plus one fn (the next number) the endpoint does not simulate: it answers only the two stand-in
    operations (endpoint.TOY_WRITE / TOY_READ), for tests of the session rules. VIRTUAL BENCH ONLY."""
    fn = max(o.fn for o in probe.offered) + 1
    return _again(probe, list(probe.offered) + [Offered(fn, 0, STAND_IN)])


I2C_INTERNAL_PULLUPS = reg.FIXTURE_I2C_TARGET.enum["features"]["internal_pullups"]


def with_i2c_pullups(probe: VirtualProbe) -> VirtualProbe:
    """The profile with its oep.fixture.i2c-target enabling pull-ups of its own on SDA / SCL while configured,
    declared as fixture §3 says: features bit2. VIRTUAL BENCH ONLY (the profiles declare none)."""
    def decl(o: Offered) -> Offered:
        return _with(o, (catalog.u32(FEATURES, struct.unpack_from("<I", t, 3)[0] | I2C_INTERNAL_PULLUPS)
                         if t[0] == FEATURES else t for t in o.tlvs))
    return _again(probe, [decl(o) if o.name == "oep.fixture.i2c-target" else o for o in probe.offered])


def without_drive_levels(probe: VirtualProbe) -> VirtualProbe:
    """The profile with no drive_levels on its oep.fixture.gpio fns: a probe that cannot switch the output strength
    (fixture §1.1). VIRTUAL BENCH ONLY."""
    return _again(probe, [_with(o, (t for t in o.tlvs if t[0] != GPIO_DRIVE_LEVELS)) if o.name == "oep.fixture.gpio"
                          else o for o in probe.offered])


PROFILES = {"p4-x035": p4_x035, "esp32-v003": esp32_v003, "esp32-v003-64": esp32_v003_64, "p4-bench": p4_bench,
            "rp2350-pins": rp2350_pins}
