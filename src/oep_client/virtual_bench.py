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
from .virtual_bench_capture import BLOCK_TOP

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
    inner: tuple[tuple[str, object], ...] = ()   # what the fn keeps to itself, not declared: (key, value) pairs


CORE_NAME = ""                       # fn 0 is the core: it has no name and is never listed (core §0, §7.2)
PLAN_ROLE_INTERFACES = {"oep.fixture.gpio", "oep.fixture.uart", "oep.fixture.i2c-target", "oep.fixture.spi-target",
                        "oep.fixture.logic", "oep.fixture.analog", "oep.fixture.capture-group"}   # the interfaces with plan roles (oep-if-plan)
STAND_IN_OPS = (0x10, 0x11)          # the endpoint's two stand-in ops of an fn it does not simulate (VIRTUAL BENCH ONLY)
CORE_REQUIRED = ("confirm", "list", "describe", "clock", "open", "end", "keepalive", "lock_state")   # fn 0's required ops (core §1.2)


def default_ops(fn: int, name: str) -> set[int]:
    """The ops an fn offers when its profile says nothing (core §1.2): fn 0 the core's required ones; an interface
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
                 own_channels=(), core_contract=False):
        """Every offered fn's describe carries the ops tag (core §7.4): one that gives none gets `default_ops`, first;
        oep.probe.restart carries restart_max_ms (RESTART_MAX_MS when it gives none, oep-if-restart §1) (`fill_ops`
        False: left as given - a probe that does not conform, for tests). A probe offering an interface with plan roles
        and no oep.probe.plan gets one at the next free fn (oep-if-plan: it must list one). fn 0 is the core (no name,
        never listed); every other fn has a name. `own_channels`: the channels the probe uses itself (its own UART,
        USB, strapping pins): never an interface's, never parked, not a settings key (core §8, §13.8) -
        not declared."""
        self.core_contract = core_contract
        self.label = label
        self.max_frame = max_frame
        self.own_channels = tuple(own_channels)
        offered = list(offered)
        if fill_ops and PLAN_ROLE_INTERFACES & {o.name for o in offered} and not any(o.name == PLAN for o in offered):
            # an interface with plan roles: the probe lists oep.probe.plan too (oep-if-plan), at the next free fn
            offered.append(_plan(max(o.fn for o in offered) + 1))
        if core_contract and any(o.fn != 0 for o in offered):
            raise ValueError('core-v1 profile cannot expose legacy interfaces')
        self.offered = sorted((o if not fill_ops else self._filled(o) for o in offered), key=lambda o: o.fn)
        self.requests = 0
        for o in self.offered:
            if o.fn != CORE_FN:
                names.validate(o.name)

    @staticmethod
    def _filled(o: Offered) -> Offered:
        tlvs = o.tlvs
        if not any(t[0] == catalog.OPS for t in tlvs):
            tlvs = (catalog.ops_tlv(default_ops(o.fn, o.name)),) + tlvs
        if o.name == RESTART and not any(t[0] == RESTART_MAX_MS_TAG for t in tlvs):
            tlvs = tlvs + (catalog.u32(RESTART_MAX_MS_TAG, RESTART_MAX_MS),)
        return o if tlvs is o.tlvs else Offered(o.fn, o.instance, o.name, tlvs, o.revision, o.inner)

    def instance_errors(self) -> list[str]:
        """Where the offered instances break core §7.2: an instance is unique within its (name, revision) - 0 may be
        used and gaps are allowed (it is not computed from the fn order or the list position). Empty for every
        profile; a test may make a probe that breaks it on purpose."""
        seen: dict[tuple[str, int, int], int] = {}
        out = []
        for o in self.offered:
            k = (o.name, o.revision, o.instance)
            if k in seen:
                out.append(f"fn {o.fn} {o.name} rev {o.revision} repeats instance {o.instance} of fn {seen[k]}")
            seen.setdefault(k, o.fn)
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
            return (struct.pack("<4sBHIBI", m.CONFIRM_RESULT, REVISION, self.max_frame, WINDOW, MAX_INFLIGHT, BOOT_ID)
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
            size = len(catalog.pack_entry(self._entry(o)))     # fn instance revision name_len name (core §7.2)
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
        return ListEntry(o.fn, o.instance, o.revision, o.name)


# ---- example profiles ----------------------------------------------------
# Probe-wide declarations in fn 0's describe (the core, 0x40..), oep.wire.<link> to scan and attach,
# oep.target.riscv-dm and oep.target.console on the connection, fixtures gpio / uart / capture / i2c / spi targets,
# oep.probe.link, and then oep.probe.plan and oep.probe.restart (appended: the fns before them keep
# their numbers). Probe-wide tags:
_CORE_TAGS = reg.CORE.tlv["describe"]
CORE_FIRMWARE, CORE_MODEL, CORE_UNIT_ID, CORE_CHANNELS = (_CORE_TAGS[k] for k in ("firmware", "model", "unit_id", "channels"))
CORE_TRANSPORT, CORE_MAX_OP_MS = _CORE_TAGS["transport"], _CORE_TAGS["max_op_ms"]
CHANNEL_NAME, NETIF = _CORE_TAGS["channel_name"], _CORE_TAGS["netif"]
MAX_OP_MS = 10000                                 # what the virtual bench declares: the longest one request may take (core §7.5)
PLAN, RESTART, LINK = reg.PROBE_PLAN.name, reg.PROBE_RESTART.name, reg.PROBE_LINK.name
RESTART_MAX_MS_TAG = reg.PROBE_RESTART.tlv["describe"]["restart_max_ms"]
RESTART_MAX_MS = 2000                             # what the virtual bench declares: back on confirm within this (oep-if-restart §1)
PLAN_ROLES_TAG = reg.PROBE_PLAN.tlv["describe"]["plan_roles"]
PLAN_ROLES = 32                                   # role assignments the virtual bench's plan holds at once (oep-if-plan §1)
GPIO_MODES = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
UART_FORMATS = reg.FIXTURE_UART.tlv["describe"]["formats"]
I2C_QUEUE_DEPTH = reg.FIXTURE_I2C_TARGET.tlv["describe"]["queue_depth"]
I2C_MAX_STRETCH_US = reg.FIXTURE_I2C_TARGET.tlv["describe"]["max_stretch_us"]
SPI_QUEUE_DEPTH = reg.FIXTURE_SPI_TARGET.tlv["describe"]["queue_depth"]
SPI_CS_SETUP_NS = reg.FIXTURE_SPI_TARGET.tlv["describe"]["cs_setup_ns"]
SPI_MODES = reg.FIXTURE_SPI_TARGET.tlv["describe"]["modes"]
FORMATS_8 = (0x00, 0x04, 0x08, 0x10, 0x14, 0x18)   # 8N1 8E1 8O1 8N2 8E2 8O2: the formats the virtual bench UARTs take
TRANSPORT = reg.CORE.enum["transport_kind"]
SERIAL_KINDS = {TRANSPORT["uart_bridge"], TRANSPORT["usb_cdc"], TRANSPORT["usb_serial_jtag"]}
NS = "io.github.ch32-riscv-ug"
MECHANISM = reg.TARGET_CONSOLE.tlv["describe"]["mechanism"]
_CAPD = reg.FIXTURE_LOGIC.tlv["describe"]
_ANAD = reg.FIXTURE_ANALOG.tlv["describe"]
_GRPD = reg.FIXTURE_CAPTURE_GROUP.tlv["describe"]
CORE_CHIP = reg.CORE.tlv["describe"]["chip"]
UNIT_ID = re.compile(r"[a-z0-9-]{1,32}")                    # an x- unit_id is not unique (core §7.5, C-24)


def _analog_decl(frontends: list[tuple[int, int, int]], max_samples: int, min_hz: int, max_hz: int) -> tuple[bytes, ...]:
    """oep.fixture.analog's declarations (oep-if-capture §3.5): one-shot / repeat / streaming, rate_range, 4 channels,
    the frontends (number, min_mv, max_mv at the input pin), triggers cross up / down."""
    out = tuple(catalog.tlv(_ANAD["mode"], struct.pack("<BII", _CAPM[k], max_samples, 8 if k == "repeat" else 0))
                for k in ("one_shot", "repeat", "streaming"))
    out += (catalog.tlv(_ANAD["rate_range"], struct.pack("<II", min_hz, max_hz)),)
    out += tuple(catalog.tlv(_ANAD["frontend"], struct.pack("<Bii", *f)) for f in frontends)
    return out + (catalog.u8(_ANAD["channels"], 4),
                  # immediate, cross up / down; every trigger_opts (arm 0, rearm 1, when 1, hysteresis) with 16 records
                  catalog.tlv(_ANAD["trigger"], struct.pack("<IIB", 0b11001 | 1 << _LINKED, max_samples - 1, 0b1111)),
                  catalog.u16(_ANAD["trigger_records"], 16),
                  catalog.tlv(_ANAD["formats"], bytes([_FMT["analog_blocks"], _FMT["test_counter"]])),
                  # format 2: packed, minmax and sum, D up to the longest block (§1.4)
                  catalog.tlv(_ANAD["carriage"], struct.pack("<BII", sum(_ACARRIAGE.values()), 2, BLOCK_TOP)))
    # no features: revision 1 defines no bit (query, force, subscribe / unsubscribe: every op offered, the ops tag)


def _group_decl(tracks: list[int], sourced: bool = False) -> tuple[bytes, ...]:
    """oep.fixture.capture-group's declarations (§4.3): tracks n x fn; with `sourced` the trigger source (§4.4:
    features bit 0, trigger_caps, force in the ops), else the ops tag without force."""
    out = (catalog.tlv(_GRPD["tracks"], struct.pack(f"<B{len(tracks)}H", len(tracks), *tracks)),)
    if not sourced:
        return ops_of("oep.fixture.capture-group", "force") + out
    from . import virtual_bench_trigger
    return out + (catalog.u32(FEATURES, reg.FIXTURE_CAPTURE_GROUP.enum["features"]["trigger_source"]),
                  catalog.tlv(_GRPD["trigger_caps"], virtual_bench_trigger.caps()))
_CAPM = reg.FIXTURE_LOGIC.enum["mode"]
_FMT = reg.FIXTURE_LOGIC.enum["data_format"]
_LINKED = reg.FIXTURE_LOGIC.enum["trigger"]["linked"]
_CARRIAGE = reg.FIXTURE_LOGIC.enum["logic_carriage"]
_ACARRIAGE = reg.FIXTURE_ANALOG.enum["analog_carriage"]


def _capture_decl(modes: list[str], max_channels: int, max_samples: int, ring: int, min_hz: int,
                  max_hz: int, opts: int = 0, records: int = 0, formats: tuple[int, ...] = (),
                  carriage: tuple[int, int, int] | None = None, linked: bool = False) -> tuple[bytes, ...]:
    """oep.fixture.logic's declarations (oep-if-capture §3.5): a mode entry per mode (mode, max_samples, max_segments:
    0 outside repeat), rate_range, channels (max), the trigger types (immediate, level, edge) with the most pretrigger."""
    out = tuple(catalog.tlv(_CAPD["mode"], struct.pack("<BII", _CAPM[k], max_samples, ring if k == "repeat" else 0))
                for k in modes)
    out += (catalog.tlv(_CAPD["rate_range"], struct.pack("<II", min_hz, max_hz)),
            catalog.u8(_CAPD["channels"], max_channels),
            # types u32, max_pretrigger, opts (trigger_opts beyond the defaults: bit 0 arm 0, bit 1 rearm 1, bit 2 when 1)
            catalog.tlv(_CAPD["trigger"], struct.pack("<IIB", 0b111 | (1 << _LINKED if linked else 0), max_samples - 1, opts)))
    out += (catalog.u16(_CAPD["trigger_records"], records),) if records else ()
    out += (catalog.tlv(_CAPD["formats"], bytes(formats)),) if formats else ()
    return out + ((catalog.tlv(_CAPD["carriage"], struct.pack("<BII", *carriage)),) if carriage else ())
    # no features: revision 1 defines no bit (query, force, subscribe / unsubscribe: every op offered, the ops tag)


def _capture_inner(widths: list[int], ring: int, max_read: int) -> tuple[tuple[str, object], ...]:
    """What a capture keeps to itself (oep-if-capture §1.1, §2, §3.2): the sample widths w it lays channels out in,
    the segment records it keeps, and the most one read returns."""
    return (("widths", tuple(widths)), ("ring", ring), ("max_read", max_read))
MAX_CONNECTIONS = reg.WIRE_RVSWD.tlv["describe"]["max_connections"]


BLOCK_HEADERS = 24   # write_block's request (10 + connection 4 + address 4 + count 2) needs 20 and read_block's answer
                     # (5 + done 2 + status 1) 8; the reference firmware keeps 24. A probe may declare more or less: the
                     # host takes min(max_length, max_frame - 8 / - 20) per route (oep-if-debug §4.5)


def block_max_length(max_frame: int) -> int:
    """The max_length (bytes, a multiple of 4) a probe with read_block / write_block declares for its max_frame
    (oep-if-debug §4.5): max_frame - 24 rounded down to a word, as the reference firmware does - a read's answer and a
    write's request both fit one frame. The host's count is at most min(max_length, max_frame - 8 / - 20) / 4."""
    return max(0, (max_frame - BLOCK_HEADERS) // 4 * 4)


def _transports(kinds: list[tuple[int, int]]) -> tuple[bytes, ...]:
    """(kind, interface) per transport, index = position (core §7.5, C-41): a USB CDC names its communication
    interface (the first of the function), built-in USB serial the number the hardware presents or 0xFF, vendor bulk
    and HID their interface; a UART bridge and TCP 0xFF."""
    for k, itf in kinds:
        assert itf == 0xFF or k not in (TRANSPORT["uart_bridge"], TRANSPORT["tcp"]), "core §7.5: 0xFF for a UART bridge / TCP"
    return tuple(catalog.tlv(CORE_TRANSPORT, bytes([i, k, itf])) for i, (k, itf) in enumerate(kinds))


# ---- the settings (core §13) -------------------------------------------------------------------------------
SETTING, SETTING_CHOICE = reg.DESCRIBE_COMMON["setting"], reg.DESCRIBE_COMMON["setting_choice"]
FORM, KEY_KIND, FLAG = reg.CORE.enum["setting_form"], reg.CORE.enum["setting_key_kind"], reg.CORE.enum["setting_flag"]
SETTINGS_OPS = ("get", "set", "unset")             # fn 0's ops with settings; save / state with storage, network with netifs


def choices_of(enum: dict[str, int]) -> dict[int, str]:
    """An enum item's choices (core §13.2): the registry's enum, its keys with '-' for '_'."""
    return {v: k.replace("_", "-") for k, v in enum.items()}


def format_name(fmt: int) -> str:
    """A UART format's choice name (fixture §2.1): data bits, parity (n e o), stop bits - "8n1"."""
    return f"{7 if fmt & 0x03 else 8}{'neo'[(fmt >> 2) & 0x03]}{2 if fmt & 0x10 else 1}"


@dataclass(frozen=True)
class Item:
    """One settings item an owner declares (core §13.2): its describe setting and choices."""
    name: str
    form: str
    key_kind: str = "none"
    keys: int = 0
    max_len: int = 0
    flags: int = 0
    choices: tuple[tuple[int, str], ...] = ()

    def tlvs(self, number: int) -> tuple[bytes, ...]:
        out = (catalog.tlv(SETTING, struct.pack("<BBHBB", FORM[self.form], KEY_KIND[self.key_kind], self.keys,
                                                self.max_len, self.flags) + self.name.encode()),)
        return out + tuple(catalog.tlv(SETTING_CHOICE, bytes([number, v]) + n.encode()) for v, n in self.choices)


_CS = reg.CORE.settings
CORE_ITEMS = (Item("channel.{}.label", "text", "channel", 0, reg.LIMITS["label_max_bytes"]),
              Item("channel.{}.idle", "enum", "channel", choices=tuple(choices_of(reg.CORE.enum["channel_idle"]).items())),
              Item("channel.{}.disable", "bool", "channel"))
WIFI_ENTRIES = 4
NETWORK_ITEMS = (Item("wifi.{}.ssid", "text", "index", WIFI_ENTRIES, reg.LIMITS["wifi_ssid_max_bytes"]),
                 Item("wifi.{}.passphrase", "text", "index", WIFI_ENTRIES, 64, FLAG["secret"]),
                 Item("ipv4.{}.mode", "enum", "netif", choices=tuple(choices_of(reg.CORE.enum["ipv4_mode"]).items())),
                 Item("ipv4.{}.address", "ipv4", "netif"), Item("ipv4.{}.prefix", "u32", "netif"),
                 Item("ipv4.{}.gateway", "ipv4", "netif"), Item("ipv4.{}.dns", "ipv4", "netif"))
assert {i.name for i in CORE_ITEMS + NETWORK_ITEMS} == set(_CS), "the core's items as the registry names them"
HOLD = Item("hold.{}", "bool", "channel")
REST = Item("rest.{}", "enum", "channel", choices=tuple(choices_of(reg.WIRE_RVSWD.enum["rest"]).items()))


def uart_items(formats) -> tuple[Item, ...]:
    """oep.fixture.uart's pass-through items (fixture §2.1)."""
    return (Item("passthrough.{}.rx", "channel", "transport"), Item("passthrough.{}.baud", "u32", "transport"),
            Item("passthrough.{}.format", "enum", "transport", choices=tuple((f, format_name(f)) for f in formats)))


CONSOLE_ITEMS = (Item("passthrough.{}.wire", "fn", "transport"), Item("passthrough.{}.swdio", "channel", "transport"),
                 Item("passthrough.{}.swclk", "channel", "transport"), Item("passthrough.{}.max-speed", "u32", "transport"),
                 Item("passthrough.{}.mechanism", "text", "transport", max_len=32))
assert {i.name for i in uart_items(())} == set(reg.FIXTURE_UART.settings)
assert {i.name for i in CONSOLE_ITEMS} == set(reg.TARGET_CONSOLE.settings)


def items_tlvs(items) -> tuple[bytes, ...]:
    return tuple(t for k, it in enumerate(items, 1) for t in it.tlvs(k))


def channel_name(channel: int) -> str:
    """The virtual bench's channels are named as an RP2's / ESP32's (core §7.5)."""
    return f"GPIO{channel}"


def with_settings(offered: list[Offered], *, storage: bool = True, netifs=(), labels: dict[int, str] | None = None,
                  initial=(), changeable: bool = True) -> list[Offered]:
    """A profile's fns with the settings (core §13): fn 0 offers get / set / unset (save / state with `storage`, network
    with `netifs`) and declares its channel items (the network's with `netifs`, a named list of netifs); each debug wire
    its hold (rvswd its rest too), each fixture UART and console its pass-through. `labels`: channel labels as initial
    values; `initial`: more initial values as (fn, item, key, data) (core §13.4: in on a boot without a save);
    `changeable` False: a probe of initial values only, get alone (§13.5)."""
    out = []
    for o in offered:
        extra: tuple[Item, ...] = ()
        inner = o.inner
        if o.fn == CORE_FN:
            ops = [reg.CORE.op[k] for k in CORE_REQUIRED + (SETTINGS_OPS if changeable else ("get",))]
            ops += [reg.CORE.op[k] for k in ("save", "state")] if storage and changeable else []
            ops += [reg.CORE.op["network"]] if netifs else []
            items = CORE_ITEMS + (NETWORK_ITEMS if netifs else ())
            firm = tuple((0, "channel.{}.label", ch, n.encode()) for ch, n in (labels or {}).items()) + tuple(initial)
            out.append(Offered(o.fn, o.instance, o.name, (catalog.ops_tlv(ops),) + tuple(t for t in o.tlvs if t[0] != catalog.OPS)
                               + tuple(catalog.tlv(NETIF, n.encode()) for n in netifs) + items_tlvs(items), o.revision,
                               inner + (("initial_values", firm),)))
            continue
        if o.name == "oep.wire.rvswd":
            extra = (REST, HOLD)
        elif o.name in ("oep.wire.swio", "oep.wire.swd"):
            extra = (HOLD,)
        elif o.name == "oep.fixture.uart":
            formats = next((t[4:4 + t[3]] for t in o.tlvs if t[0] == UART_FORMATS), b"\0")
            extra = uart_items(formats)
        elif o.name == "oep.target.console":
            extra = CONSOLE_ITEMS
        out.append(Offered(o.fn, o.instance, o.name, o.tlvs + items_tlvs(extra), o.revision, inner) if extra else o)
    return out


def _gpio(fn: int, channels: list[int], modes: int = 0x7F) -> Offered:
    """oep.fixture.gpio: role 1 on `channels` and the modes it drives (u32 bit set, fixture §1); the output strength is
    the probe's own."""
    return Offered(fn, 0, "oep.fixture.gpio", _roles({1: channels}) + (catalog.u32(GPIO_MODES, modes),))


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
              cs_setup_ns: int = 0, modes: int = 0x0F) -> tuple[bytes, ...]:
    """oep.fixture.spi-target's declarations besides its pins (fixture §4): max_length (bytes a transaction),
    max_clock_hz, features (bit0 LSB first), queue_depth (tag 0x40 u8), modes (tag 0x41 u8, required: bit k = SPI mode
    k), and cs_setup_ns (tag 0x43 u32: the worst CS-active-to-first-SCK time for which MISO carries the first bit -
    declared by a probe that drives MISO in software once it sees CS; 0 = left out, MISO driven at once)."""
    return ((catalog.u16(MAX_LENGTH, max_length), catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.u32(FEATURES, features),
             catalog.u8(SPI_QUEUE_DEPTH, queue_depth), catalog.u8(SPI_MODES, modes))
            + ((catalog.u32(SPI_CS_SETUP_NS, cs_setup_ns),) if cs_setup_ns else ()))


_I2CC_D, _SPIC_D = reg.FIXTURE_I2C_CONTROLLER.tlv["describe"], reg.FIXTURE_SPI_CONTROLLER.tlv["describe"]
_PWM_D, _CNT_D, _DAC_D = (reg.FIXTURE_PWM.tlv["describe"], reg.FIXTURE_COUNTER.tlv["describe"],
                          reg.FIXTURE_DAC.tlv["describe"])


def i2c_controller_max_length(max_frame: int) -> int:
    """The largest max_length whose write-only request (header 10 + flags 1 + n 1 + segment 4 + data) and read-only
    answer (header 5 + result 1 + at 1 + index 2 + count 2 + data) fit max_frame (fixture §5)."""
    request, result = reg.LIMITS["request_header_bytes"], reg.LIMITS["result_header_bytes"]
    return min(max_frame - (request + 1 + 1 + 4), max_frame - (result + 1 + 1 + 2 + 2))


def spi_controller_max_length(max_frame: int) -> int:
    """The largest max_length whose one-item request (header 10 + n 1 + item 14 + tx) and answer (header 5 + 14 + rx)
    fit max_frame (fixture §6)."""
    request, result = reg.LIMITS["request_header_bytes"], reg.LIMITS["result_header_bytes"]
    return min(max_frame - (request + 1 + 14), max_frame - (result + 14))


def _i2c_controller(fn: int, channels: list[int], max_length: int, max_hz: int, max_stretch_us: int,
                    pullups: bool = True) -> Offered:
    """oep.fixture.i2c-controller (fixture §5): SDA / SCL on `channels`, max_length (written and read bytes a transfer),
    max_clock_hz, features bit0 internal pull-ups, max_stretch_timeout_us (tag 0x40, required), min_clock_hz."""
    return Offered(fn, 0, "oep.fixture.i2c-controller", _roles({1: channels, 2: channels}) + (
        catalog.u16(MAX_LENGTH, max_length), catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.u32(MIN_CLOCK_HZ, 1_000),
        catalog.u32(FEATURES, reg.FIXTURE_I2C_CONTROLLER.enum["features"]["internal_pullups"] if pullups else 0),
        catalog.u32(_I2CC_D["max_stretch_timeout_us"], max_stretch_us)))


def _spi_controller(fn: int, channels: list[int], max_length: int, max_hz: int, time_step_ns: int,
                    max_delay_ns: int, modes: int = 0x0F) -> Offered:
    """oep.fixture.spi-controller (fixture §6): SCK / MOSI / MISO / CS on `channels`, max_length (bytes a transfer),
    max_clock_hz, min_clock_hz, features bit0 LSB first, time_step_ns, modes, max_delay_ns (tags 0x40-0x42, required)."""
    return Offered(fn, 0, "oep.fixture.spi-controller", _roles({r: channels for r in (1, 2, 3, 4)}) + (
        catalog.u16(MAX_LENGTH, max_length), catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.u32(MIN_CLOCK_HZ, 1_000),
        catalog.u32(FEATURES, reg.FIXTURE_SPI_CONTROLLER.enum["features"]["lsb_first"]),
        catalog.u32(_SPIC_D["time_step_ns"], time_step_ns), catalog.u8(_SPIC_D["modes"], modes),
        catalog.u32(_SPIC_D["max_delay_ns"], max_delay_ns)))


def _pwm(fn: int, channels: list[int], outputs: int, min_hz: int, max_hz: int, duty_bits: int) -> Offered:
    """oep.fixture.pwm (fixture §7): output k (role k) on `channels`, min / max_clock_hz, channels (0x40), duty_bits (0x41)."""
    return Offered(fn, 0, "oep.fixture.pwm", _roles({k: channels for k in range(outputs)}) + (
        catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.u32(MIN_CLOCK_HZ, min_hz),
        catalog.u8(_PWM_D["channels"], outputs), catalog.u8(_PWM_D["duty_bits"], duty_bits)))


def _counter(fn: int, channels: list[int], inputs: int, max_hz: int, max_gate_us: int) -> Offered:
    """oep.fixture.counter (fixture §8): input k (role k) on `channels`, max_clock_hz, channels (0x40), max_gate_us (0x41)."""
    return Offered(fn, 0, "oep.fixture.counter", _roles({k: channels for k in range(inputs)}) + (
        catalog.u32(MAX_CLOCK_HZ, max_hz), catalog.u8(_CNT_D["channels"], inputs),
        catalog.u32(_CNT_D["max_gate_us"], max_gate_us)))


def _dac(fn: int, channels: list[int], bits: int, min_mv: int, max_mv: int) -> Offered:
    """oep.fixture.dac (fixture §9): output k on the k-th of `channels`, channels (0x40), bits (0x41), range (0x42)."""
    return Offered(fn, 0, "oep.fixture.dac", _roles({k: [c] for k, c in enumerate(channels)}) + (
        catalog.u8(_DAC_D["channels"], len(channels)), catalog.u8(_DAC_D["bits"], bits),
        catalog.tlv(_DAC_D["range"], struct.pack("<ii", min_mv, max_mv))))


CONSOLE_SEND_QUEUE = 256      # each console stream's send queue, bytes: the probe's own size (console §2, not declared)


def _console(fn: int, mechanisms=("sdi", "dmdata", "dmseq"), send_queue: int = CONSOLE_SEND_QUEUE) -> Offered:
    """oep.target.console: the mechanisms it opens, a named list (console §1, core §7.7: the place from 1 is the number),
    and - kept to itself - the send queue a stream of dmdata / dmseq has (§2)."""
    return Offered(fn, 0, "oep.target.console", tuple(catalog.tlv(MECHANISM, m.encode()) for m in mechanisms),
                   inner=(("send_queue", send_queue),))


def _link(fn: int, port_speed: bool = False) -> Offered:
    """oep.probe.link (oep-if-link): source and sink, and port_speed with port_state when `port_speed` (a probe on a
    UART bridge; port_state is required with port_speed, oep-if-link §1)."""
    return Offered(fn, 0, LINK, ops_of(LINK) if port_speed else ops_of(LINK, "port_speed", "port_state"))


def _plan(fn: int, plan_roles: int = PLAN_ROLES) -> Offered:
    """oep.probe.plan (oep-if-plan): plan_apply, plan_release, and plan_roles - the most role assignments at once."""
    return Offered(fn, 0, PLAN, (catalog.u32(PLAN_ROLES_TAG, plan_roles),))


def _restart(fn: int, restart_max_ms: int = RESTART_MAX_MS) -> Offered:
    """oep.probe.restart (oep-if-restart): restart, and restart_max_ms - back on confirm within this after the answer."""
    return Offered(fn, 0, RESTART, (catalog.u32(RESTART_MAX_MS_TAG, restart_max_ms),))


def _roles(assign: dict[int, list[int]]) -> tuple[bytes, ...]:
    return tuple(catalog.role_channels(r, ch) for r, ch in assign.items())


def _core(firmware: str, model: str, unit_id: str, channels: int, extra: tuple[bytes, ...] = ()) -> Offered:
    """fn 0's declarations (core §7.5): firmware and model (free text), the required unit_id, transport (in `extra`)
    and max_op_ms, the channel count and every channel's name. fn 0 is the core: no name, never listed."""
    assert UNIT_ID.fullmatch(unit_id) and unit_id != "any", "core §7.5: unit_id grammar, never any (a host's wildcard)"
    return Offered(0, 0, CORE_NAME, (
        catalog.text(CORE_FIRMWARE, firmware), catalog.text(CORE_MODEL, model), catalog.text(CORE_UNIT_ID, unit_id),
        catalog.u16(CORE_CHANNELS, channels), catalog.u32(CORE_MAX_OP_MS, MAX_OP_MS),
        ) + tuple(catalog.tlv(CHANNEL_NAME, struct.pack("<H", c) + channel_name(c).encode()) for c in range(channels))
        + extra)


def p4_x035() -> VirtualProbe:
    """ESP32-P4 development probe on the CH32X035F8U6 jig (as wired on 2026-09-24), in the recommended USB shape
    (probe guide §8): USB-Serial/JTAG (serial port 0), and on the HS port vendor bulk, HID and a CDC (serial port 3)
    on the project's VID:PID."""
    own = [24, 25]                                  # USB-Serial/JTAG (the probe's own: never an interface's)
    pins = [p for p in range(55) if p not in own + [2, 54]]   # the fixtures' pins: all but the RVSWD pair
    return VirtualProbe("p4-x035", 1024, with_settings([
        _core("3.0.0", "esp32p4", "fafe00000035", 55,
              _transports([(TRANSPORT["usb_serial_jtag"], 0xFF), (TRANSPORT["vendor_bulk"], 0),
                           (TRANSPORT["hid"], 1), (TRANSPORT["usb_cdc"], 2)])
              + (catalog.text(CORE_CHIP, "esp32p4 v1.0"),)),
        Offered(1, 0, "oep.wire.rvswd", (catalog.u32(FEATURES, ATTACH_HALT),
            catalog.channel_group(1, [(1, 2), (2, 54)]), catalog.u32(MAX_CLOCK_HZ, 5_000_000),
            catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(MAX_LENGTH, block_max_length(1024)),)),   # every op
        _console(3),
        _gpio(4, pins),
        _uart(5, 0, pins, 3_000_000),
        _uart(6, 1, pins, 3_000_000),
        # the P4's PARLIO: w 1-16, one-shot / repeat / streaming
        Offered(7, 0, "oep.fixture.logic", _roles({k: pins for k in range(16)})
            + _capture_decl(["one_shot", "repeat", "streaming"], 16, 1 << 20, 8, 1_000, 20_000_000, 0b0111, 16,
                            (_FMT["logic_blocks"], _FMT["test_counter"]),
                            (sum(1 << v for v in _CARRIAGE.values()), 2, 4096), linked=True),   # raw / events D 2-4096
            inner=_capture_inner([1, 2, 4, 8, 16], 8, 4096)),
        _i2c_target(8, pins, max_length=128, max_hz=1_000_000, features=0, queue_depth=8,
                    max_stretch_us=100_000),                                                       # stretch
        Offered(9, 0, "oep.fixture.spi-target", _roles({1: pins, 2: pins, 3: pins, 4: pins})
                + _spi_decl(max_length=64, max_hz=3_000_000, features=0b1, queue_depth=8)),             # LSB first
        # fn 10: the settings' interface before they moved into the core (core §13); its number left unused
        # the P4's ADC1 (GPIO16-23): one ADC for all its channels, 611 Hz - 83.3 kHz in all; ESP32-style attenuations
        Offered(11, 0, "oep.fixture.analog", _roles({k: list(range(16, 24)) for k in range(4)})
            + _analog_decl([(0, 0, 950), (1, 0, 1250), (2, 0, 1750), (3, 0, 3100)], 65536, 611, 83_333),
            inner=_capture_inner([16], 8, 4096)),
        # the logic (fn 7) and the analog (fn 11) together; the ADC's 83.3 kHz is shared by its channels (the probe's
        # own budget: a bind past it is refused unavailable, capture §4.2)
        Offered(12, 0, "oep.fixture.capture-group", _group_decl([7, 11], sourced=True),
                inner=(("max_tracks", 2), ("budgets", ((83_333, (11,)),)))),
        # the drive fixtures of the P4 firmware: controllers bit-banged on GPIOs, LEDC PWM, an edge counter (no DAC)
        _i2c_controller(13, pins, max_length=256, max_hz=400_000, max_stretch_us=100_000),
        _spi_controller(14, pins, max_length=256, max_hz=2_000_000, time_step_ns=100, max_delay_ns=1_000_000),
        _pwm(15, pins, 4, 10, 10_000_000, 3),
        _counter(16, pins, 4, 100_000, 5_000_000),
        _link(17),
        _plan(18),
        _restart(19),
    ], netifs=("ethernet",), labels={2: "x035.swdio", 54: "x035.swclk"}), own_channels=own)


def esp32_v003(max_frame: int = 512) -> VirtualProbe:
    """A classic ESP32 on a CH32V003 (SWIO) jig over a UART bridge (its only transport, serial port 0), with the reference
    classic ESP32 firmware's frame limits: max_frame 512 and oep.target.riscv-dm's max_length 488 (max_frame - 24 rounded
    down to a word, oep-if-debug §4.5), so a host splits its block ops as it does on that probe (a 9168-byte image: 19
    write_blocks of up to 122 words, not 230 of 10). oep.probe.link with port_speed (oep-if-link §3), as that firmware.
    Its channels and the other interfaces' limits are this profile's own. `max_frame`: the same probe at another frame
    size (esp32_v003_64: the smallest a probe may declare). The core's settings with storage and the Wi-Fi netif (not
    below the network items' max_frame floor, transports §7)."""
    own = [0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 15]   # UART0, strapping, flash: the probe's own (SWIO 16 is the wire's)
    wired = [4, 5, 13, 14, 17, 18, 19, 21, 22, 25, 26, 27, 32, 33]
    return VirtualProbe("esp32-v003" if max_frame == 512 else f"esp32-v003-{max_frame}", max_frame, with_settings([
        _core("3.0.0", "esp32", "fafe00000003", 40, _transports([(TRANSPORT["uart_bridge"], 0xFF)])),
        Offered(1, 0, "oep.wire.swio", (catalog.u32(FEATURES, ATTACH_HALT), catalog.channel_group(1, [(1, 16)]), catalog.role_channels(3, [23]),
                                        catalog.u8(MAX_CONNECTIONS, 1),
                                        # the fixed speed of a 0 slot, as the reference firmware (oep-if-debug §3.2)
                                        catalog.u32(MIN_CLOCK_HZ, SWIO_HZ))),
        Offered(2, 0, "oep.target.riscv-dm", ops_of("oep.target.riscv-dm", "step") + (    # no step
            catalog.u16(MAX_LENGTH, block_max_length(max_frame)),)),
        _console(3),
        _gpio(4, wired + [23]),
        _uart(5, 0, wired, 115_200),
        # the classic ESP32's GPIO sampler: a byte a sample (w 8), one-shot
        Offered(6, 0, "oep.fixture.logic", _roles({k: wired for k in range(4)})
            + _capture_decl(["one_shot"], 8, 65536, 1, 400_000, 2_000_000), inner=_capture_inner([8], 1, 480)),
        _i2c_target(7, wired, max_length=16, max_hz=100_000, features=0, queue_depth=4),       # no stretch
        # Two fixed pin sets (an example of channel_group; GPIO23 is the DUT's NRST on this jig); MSB first only.
        Offered(8, 0, "oep.fixture.spi-target", (
            catalog.channel_group(1, [(1, 18), (2, 19), (3, 5), (4, 4)]),
            catalog.channel_group(2, [(1, 14), (2, 13), (3, 27), (4, 26)]))
            + _spi_decl(max_length=32, max_hz=3_000_000, features=0, queue_depth=4, cs_setup_ns=4000)),   # MISO in software
        # fn 9: the settings' interface before they moved into the core (core §13); its number left unused
        # the classic ESP32 firmware's drive fixtures: controllers on GPIOs, LEDC PWM, an edge counter, the DAC on 25 / 26
        _i2c_controller(10, wired, max_length=min(256, i2c_controller_max_length(max_frame)), max_hz=400_000,
                        max_stretch_us=100_000),
        _spi_controller(11, wired, max_length=min(256, spi_controller_max_length(max_frame)), max_hz=2_000_000,
                        time_step_ns=100, max_delay_ns=1_000_000),
        _pwm(12, wired, 4, 10, 10_000_000, 3),
        _counter(13, wired, 4, 100_000, 5_000_000),
        _dac(14, [25, 26], 8, 0, 3300),
        _link(15, port_speed=True),
        _plan(16),
        _restart(17),
    ], netifs=("wifi",) if max_frame >= reg.LIMITS["wifi_min_max_frame"] else (),   # transports §7: max_frame >= 82
        labels={16: "v003.swio", 23: "v003.nrst", 22: "v003.rx", 21: "v003.tx"}), own_channels=own)


def esp32_v003_64() -> VirtualProbe:
    """esp32-v003 at the smallest max_frame a probe may declare (64, reg.MIN_MAX_FRAME): list and describe paged, a
    riscv-dm max_length of 40, every answer cut to 64 bytes - what a host must still work with."""
    return esp32_v003(reg.MIN_MAX_FRAME)


def p4_bench() -> VirtualProbe:
    """A made-up bench probe with three RVSWD places and two seats (several connections at once): USB-Serial/JTAG (serial
    port 0), vendor bulk, HID and a CDC (serial port 3) on the project's VID:PID."""
    own = [24, 25]                                  # USB-Serial/JTAG (the wires' pins 2-7 are interfaces')
    pins = [p for p in range(55) if p not in own + list(range(2, 8))]
    return VirtualProbe("p4-bench", 1024, with_settings([
        _core("3.0.0", "esp32p4", "30eda0e3b001", 55,
              _transports([(TRANSPORT["usb_serial_jtag"], 0xFF), (TRANSPORT["vendor_bulk"], 0),
                           (TRANSPORT["hid"], 1), (TRANSPORT["usb_cdc"], 2)])),
        Offered(1, 0, "oep.wire.rvswd", (catalog.u32(FEATURES, ATTACH_HALT),
            catalog.channel_group(1, [(1, 2), (2, 3)]), catalog.channel_group(2, [(1, 4), (2, 5)]),
            catalog.channel_group(3, [(1, 6), (2, 7)]), catalog.u32(MAX_CLOCK_HZ, 5_000_000),
            catalog.u8(MAX_CONNECTIONS, 2))),
        Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(MAX_LENGTH, block_max_length(1024)),)),
        _console(3),
        _gpio(4, pins),
        _uart(5, 0, pins, 3_000_000),
        # fn 6: the settings' interface before they moved into the core (core §13); its number left unused
        _link(7),
        _plan(8),
        _restart(9),
    ], labels={2: "a.swdio", 3: "a.swclk", 4: "b.swdio", 5: "b.swclk", 6: "c.swdio", 7: "c.swclk"}), own_channels=own)


def rp2350_pins() -> VirtualProbe:
    """A board firmware whose wire takes its pins from the host (oep-if-debug §1, role_channels): any two of GP0-GP29
    but GP19 (the Pro Micro RP2350's PSRAM CS) are an RVSWD pair, a reset line or fixture pins. USB CDC (serial port 0)
    on the project's VID:PID.
    The virtual target answers on (0, 1), the first pair."""
    own = [19]
    pins = [p for p in range(30) if p not in own]
    return VirtualProbe("rp2350-pins", 1024, with_settings([
        _core("3.0.0", "rp2350", "e66138935f2b1f2c", 30, _transports([(TRANSPORT["usb_cdc"], 0)])),
        Offered(1, 0, "oep.wire.rvswd", _roles({1: pins, 2: pins, 3: pins}) + (catalog.u32(FEATURES, ATTACH_HALT),
            catalog.u32(MAX_CLOCK_HZ, 5_000_000), catalog.u8(MAX_CONNECTIONS, 1))),
        Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(MAX_LENGTH, block_max_length(1024)),)),
        _console(3),
        _gpio(4, pins),
        _uart(5, 0, pins, 3_000_000),
        _link(6),
        _plan(7),
        _restart(8),
    ]), own_channels=own)


STAND_IN = f"{NS}.stand-in"


def _again(probe: VirtualProbe, offered: list[Offered]) -> VirtualProbe:
    """The same probe (label, max_frame, own channels) with other fns."""
    return VirtualProbe(probe.label, probe.max_frame, offered, fill_ops=not probe.core_contract,
                        own_channels=probe.own_channels, core_contract=probe.core_contract)


def _with(o: Offered, tlvs) -> Offered:
    return Offered(o.fn, o.instance, o.name, tuple(tlvs), o.revision, o.inner)


def without(probe: VirtualProbe, name: str) -> VirtualProbe:
    """The profile without its fns of interface `name` (`virtual_bench_serve --no-restart`: a probe without the optional
    oep.probe.restart, which then lists none and answers unknown_function on its old fn). The other fns keep their
    numbers. When no interface with plan roles is left, oep.probe.plan goes too (oep-if-plan: a probe without plan
    roles lists none). VIRTUAL BENCH ONLY."""
    kept = [o for o in probe.offered if o.name != name]
    if not PLAN_ROLE_INTERFACES & {o.name for o in kept}:
        kept = [o for o in kept if o.name != PLAN]
    return _again(probe, kept)


STREAM_OPTIONAL_OPS = ("marks", "clear", "mark", "write")   # common §1: what a position stream may leave out


def minimal_ops(probe: VirtualProbe) -> VirtualProbe:
    """The profile with oep.target.console and oep.fixture.uart at their smallest (console §1: open / read / close /
    streams; fixture §2: configure / read / status): marks, clear, mark and write left out of their ops tags, so they
    answer unknown_operation and read's from 3 is unsupported - for a host's test that it sends an optional op only
    when describe's ops set it (conformance §2). VIRTUAL BENCH ONLY."""
    def small(o: Offered) -> Offered:
        if o.name not in ("oep.target.console", "oep.fixture.uart"):
            return o
        return _with(o, ops_of(o.name, *STREAM_OPTIONAL_OPS) + tuple(t for t in o.tlvs if t[0] != catalog.OPS))
    return _again(probe, [small(o) for o in probe.offered])


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


ATTACH_HALT = reg.WIRE_RVSWD.enum["features"]["attach_halt"]   # attach method 1 (debug §3)
SWIO_HZ = 888_888                     # SWIO's 0 slot, 862.5 ns low + 262.5 ns high (oep-probe-arduino's SwioPhy)
I2C_INTERNAL_PULLUPS = reg.FIXTURE_I2C_TARGET.enum["features"]["internal_pullups"]


def with_i2c_pullups(probe: VirtualProbe) -> VirtualProbe:
    """The profile with its oep.fixture.i2c-target enabling pull-ups of its own on SDA / SCL while configured,
    declared as fixture §3 says: features bit2. VIRTUAL BENCH ONLY (the profiles declare none)."""
    def decl(o: Offered) -> Offered:
        return _with(o, (catalog.u32(FEATURES, struct.unpack_from("<I", t, 3)[0] | I2C_INTERNAL_PULLUPS)
                         if t[0] == FEATURES else t for t in o.tlvs))
    return _again(probe, [decl(o) if o.name == "oep.fixture.i2c-target" else o for o in probe.offered])


EXTERNAL_TCP_PORT = 7891              # with_capture_options' external data paths (capture §3.9): a TCP port and a UDP one
EXTERNAL_UDP_PORT = 7892
NAMED_FORMAT = "org.example.virtual-bench.counter"   # its named format: format 4's bytes under a name of its own


def capture_option_decls(analog: bool, channels: list[int]) -> tuple[bytes, ...]:
    """The optional capture declarations of capture §1 (format 0xFF, format_names), §3.8 (front end and external clock)
    and §3.9 (external data paths) as with_capture_options adds them; `channels` the track's pins (the logic's
    threshold group)."""
    d = (_ANAD if analog else _CAPD)
    ext = reg.FIXTURE_LOGIC.enum["external_kind"]
    out = (catalog.text(d["format_names"], NAMED_FORMAT),
           catalog.tlv(d["external_data"], struct.pack("<BBI", ext["tcp"], 0b11, 4_000) + struct.pack("<H", EXTERNAL_TCP_PORT)),
           catalog.tlv(d["external_data"], struct.pack("<BBI", ext["udp"], 0b01, 1_000) + struct.pack("<H", EXTERNAL_UDP_PORT)))
    if not analog:
        # one threshold group for every channel: 0-3300 mV in 50 mV steps, presets 1650 (3.3 V CMOS) and 1400 (TTL),
        # a hysteresis up to 200 mV in 50 mV steps; a glitch filter 10 ns - 10 us in 10 ns steps; an external clock up to
        # 50 MHz on either or both edges, with a qualifier
        presets = (1650, 1400)
        return out + (
            catalog.tlv(_CAPD["threshold"], struct.pack("<BiiIII", 0, 0, 3300, 50, 200, 50)
                        + struct.pack(f"<B{len(presets)}i", len(presets), *presets)
                        + struct.pack(f"<B{len(channels)}H", len(channels), *channels)),
            catalog.tlv(_CAPD["glitch_filter"], struct.pack("<BIII", 0, 10, 10_000, 10)),
            catalog.tlv(_CAPD["external_clock"], struct.pack("<IBB", 50_000_000, 0b111, 1)))
    out += tuple(catalog.tlv(_ANAD["coupling"], bytes([fe, 0b111])) for fe in range(4))   # DC, AC, GND
    out += tuple(catalog.tlv(_ANAD["offset"], struct.pack("<BiiI", fe, -1000, 1000, 10)) for fe in range(4))
    out += tuple(catalog.tlv(_ANAD["bandwidth"], struct.pack("<BB3I", fe, 3, 0, 20_000, 2_000)) for fe in range(4))
    out += tuple(catalog.tlv(_ANAD["impedance"], struct.pack("<BII", fe, 1_000_000, 0xFFFFFFFF)) for fe in range(4))
    # 8 / 12 bits at the ADC's full rate, 16 bits at a quarter of it; hardware averaging 2, 4, 16
    return out + (catalog.tlv(_ANAD["resolution"], struct.pack("<BBIBIBI", 3, 8, 83_333, 12, 83_333, 16, 20_833)),
                  catalog.tlv(_ANAD["oversample"], struct.pack("<B3H", 3, 2, 4, 16)))


def with_capture_options(probe: VirtualProbe) -> VirtualProbe:
    """The profile with every optional capture feature on its logic and analog tracks: named formats (format 0xFF),
    format 3 and the external clock, threshold and glitch filter (logic), coupling, offset, bandwidth, impedance,
    resolution and oversample (analog), the external data paths (TCP, UDP), and a capture-group trigger source with every
    condition, UART, trigger_out and the trig_in / trig_out roles. VIRTUAL BENCH ONLY (no profile's probe has them
    all)."""
    def decl(o: Offered) -> Offered:
        if o.name not in ("oep.fixture.logic", "oep.fixture.analog"):
            return o
        analog = o.name == "oep.fixture.analog"
        d = _ANAD if analog else _CAPD
        more = (_FMT["named"],) + (() if analog else (_FMT["state_timed"],))
        pins = sorted({ch for t in o.tlvs if t[0] == catalog.ROLE_CHANNELS
                       for ch in catalog.bitmap_to_channels(struct.unpack_from("<H", t, 4)[0], t[6:])})
        kept = tuple(catalog.tlv(d["formats"], t[3:] + bytes(more)) if t[0] == d["formats"] else t for t in o.tlvs)
        return _with(o, kept + capture_option_decls(analog, pins))

    def group(o: Offered, pins: list[int]) -> Offered:
        """Its trigger source with every condition, UART, trigger_out, and the trig_in / trig_out roles (§4.4)."""
        from . import virtual_bench_trigger
        if o.name != "oep.fixture.capture-group" or not any(t[0] == _GRPD["trigger_caps"] for t in o.tlvs):
            return o
        kept = tuple(catalog.tlv(_GRPD["trigger_caps"], virtual_bench_trigger.caps(full=True))
                     if t[0] == _GRPD["trigger_caps"] else t for t in o.tlvs)
        return _with(o, kept + tuple(catalog.role_channels(r, pins)
                                     for r in reg.FIXTURE_CAPTURE_GROUP.enum["pin_role"].values()))
    logic_pins = sorted({ch for o in probe.offered if o.name == "oep.fixture.logic" for t in o.tlvs
                         if t[0] == catalog.ROLE_CHANNELS
                         for ch in catalog.bitmap_to_channels(struct.unpack_from("<H", t, 4)[0], t[6:])})
    return _again(probe, [group(decl(o), logic_pins) for o in probe.offered])


def core_v1() -> VirtualProbe:
    """Current core only; no target/fixture interfaces."""
    def field(tag, value):
        return bytes((tag,)) + struct.pack('<H', len(value)) + value
    return VirtualProbe('core-v1', 64, [Offered(0, 0, '', (
        field(7, b'\0\x1e\0\x0f'),
        field(64, b'virtual-core-1'), field(65, b'virtual-core'),
        field(66, b'virtual-core-1'), field(73, b'\0\x02\0'),
        field(77, struct.pack('<I', 10000)),
    ))], fill_ops=False, core_contract=True)


CORE_PROFILES = {"core-v1": core_v1}

PROFILES = {"p4-x035": p4_x035, "esp32-v003": esp32_v003, "esp32-v003-64": esp32_v003_64, "p4-bench": p4_bench,
            "rp2350-pins": rp2350_pins}
