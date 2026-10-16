"""oep.probe.config revision 1 (oep-spec interfaces/oep-if-probe-config.ja.md): the probe's settings as named items -
labels, idle states, disabled channels, the serial ports' pass-throughs, the network (wifi, ipv4) and the probe's own
items - read and set, removed with unset, saved when the host says so; the storage state and the network's state as
their own lock-free operations (describe is declarations only, core §7.3).

    cfg = config.ProbeConfig(hst)
    cfg.set([config.Label(channel=23, text="nrst"), config.Idle(channel=20, mode="pull-up")])
    cfg.set([config.Wifi(entry=0, ssid="lab", passphrase="password1")])
    cfg.set([config.Own("com.example.ntp-server", "pool.ntp.org")])
    if cfg.needs_save():
        cfg.save()
    cfg.items(), cfg.describe(), cfg.state(), cfg.network()
    cfg.unset([("idle", 20)])          # or cfg.set([config.remove("idle", 20)])

Items are named (probe.config §1): the standard names in the registry, a probe's own as reverse-DNS names. The probe
lists the items it takes in describe (setting); the k-th is item number k on the wire for this boot. An item on the
wire is number(u8) len(u16) key(u16) data; this client looks the numbers up by name (`describe`, cached per boot_id) and
never keeps them across boots. A set replaces the (name, key) it carries and keeps the others; the probe checks the
whole and changes nothing on a refusal. The generation is the probe's change counter within one boot (§2): a host
compares the items themselves (`same_items`, host guide §15) and the generation only for equality.
"""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass, field

from . import catalog, core, message as m, registry as reg
from .core import Interface

_CFG = reg.PROBE_CONFIG
NAMES = tuple(_CFG.names["setting"])                            # the standard items (registry, core §7.7)
# The numbers a probe uses when it lists the standard items in the registry's order (the virtual bench's profiles do):
# a convenience for raw requests in tests and tools only - a host looks every number up by name (`ProbeConfig.wire`).
ITEM = {k.replace("-", "_"): i + 1 for i, k in enumerate(NAMES)}
DESCRIBE = _CFG.tlv["describe"]
IDLE = {k.replace("_", "-"): v for k, v in _CFG.enum["idle_mode"].items()}
STORAGE_STATE = {v: k for k, v in _CFG.enum["storage_state"].items()}
UNREADABLE = {v: k for k, v in _CFG.enum["storage_unreadable"].items()}
FORM = _CFG.enum["setting_form"]
FLAG = _CFG.enum["setting_flag"]
NET_STATE = {v: k for k, v in _CFG.enum["network_state"].items()}
NET_REASON = {v: k for k, v in _CFG.enum["network_reason"].items()}
LABEL_MAX = reg.LIMITS["label_max_bytes"]                       # a label's text: 1 to 32 bytes (probe.config §1)


def _name(table: dict[str, int], value: int) -> str:
    return next((k for k, v in table.items() if v == value), str(value))


@dataclass(kw_only=True)
class Label:
    """A name the settings give a channel (probe.config §1): the probe never moves a line by it; a host may find its
    lines by the label convention (`find_line`, host guide §5.3)."""
    channel: int
    text: str
    NAME = "label"

    def key(self) -> int:
        return self.channel

    def value(self) -> bytes:
        """key(u16) and data: the item's len bytes on the wire (probe.config §1)."""
        return struct.pack("<H", self.key()) + self.data()

    def data(self) -> bytes:
        raw = self.text.encode()
        if not 1 <= len(raw) <= LABEL_MAX or not m.valid_text(raw):
            # probe.config §1: 1 to 32 bytes (the probe refuses other lengths); this host sends no control characters
            raise ValueError(f"label {self.text!r}: 1 to {LABEL_MAX} bytes of text without control characters")
        return raw


@dataclass(kw_only=True)
class Idle:
    """The state of a free channel (probe.config §1, core §8): at boot and after every release. mode: hi-z, pull-up,
    pull-down, output-low, output-high (output_low / output_high too). An output mode keeps driving that level while the
    channel is free - a target's power switch kept on - even after the host has gone; a probe that cannot drive the
    channel refuses it unsupported. The output strength is the probe's own."""
    channel: int
    mode: str = "pull-up"          # hi-z, pull-up, pull-down, output-low, output-high
    NAME = "idle"

    def __post_init__(self):
        self.mode = self.mode.replace("_", "-")

    def key(self) -> int:
        return self.channel

    def value(self) -> bytes:
        """key(u16) and data: the item's len bytes on the wire (probe.config §1)."""
        return struct.pack("<H", self.key()) + self.data()

    def data(self) -> bytes:
        if self.mode not in IDLE:
            raise ValueError(f"idle mode {self.mode!r}: one of {', '.join(IDLE)}")
        return bytes([IDLE[self.mode]])


@dataclass(kw_only=True)
class Disable:
    """A channel the probe never drives or sets up (probe.config §1): not on this board, or wired to another part. Any
    request naming it is refused unavailable (cause 5, held by settings); describe still declares it."""
    channel: int
    NAME = "disable"

    def key(self) -> int:
        return self.channel

    def value(self) -> bytes:
        """key(u16) and data: the item's len bytes on the wire (probe.config §1)."""
        return struct.pack("<H", self.key()) + self.data()

    def data(self) -> bytes:
        return b""


@dataclass(kw_only=True)
class Passthrough:
    """Serial port `port` (fn 0's describe transport index) carries what fixture UART `fn` receives on `rx`, outside the
    OEP frames, while no session uses the port, the fn or the pin (probe.config §1.1; transports §4): a plain serial
    monitor on a probe whose one port is a UART bridge sees the target. A host on that port reads those bytes past as
    noise; its session's request there stops it for the session. Key: the port."""
    port: int
    fn: int
    rx: int
    baud: int = 115200
    format: int = 0
    NAME = "passthrough"

    def key(self) -> int:
        return self.port

    def value(self) -> bytes:
        """key(u16) and data: the item's len bytes on the wire (probe.config §1)."""
        return struct.pack("<H", self.key()) + self.data()

    def data(self) -> bytes:
        return struct.pack("<HHIB", self.fn, self.rx, self.baud, self.format)


@dataclass(kw_only=True)
class ConsolePassthrough:
    """Serial port `port` carries the target's debug console - read by the probe itself over `wire_fn` on (swdio, swclk)
    with method 0 (no halt, no reset), through console `console_fn`'s `mechanism` - outside the OEP frames, while no
    session uses the port or those pins (probe.config §1.2): a target that prints through the debug mailbox, watched
    with a plain serial monitor. Not an OEP resource (no connection, no stream). Key: the port."""
    port: int
    wire_fn: int
    swdio: int
    swclk: int = 0xFFFF
    max_speed: int = 1_000_000
    console_fn: int
    mechanism: int
    idle_clock: int = 0          # debug §3's idle_clock: 0 high, 1 low (rvswd only) - how the passthrough's attach rests the line
    NAME = "console-passthrough"

    def key(self) -> int:
        return self.port

    def value(self) -> bytes:
        """key(u16) and data: the item's len bytes on the wire (probe.config §1)."""
        return struct.pack("<H", self.key()) + self.data()

    def data(self) -> bytes:
        return struct.pack("<HHHIHBB", self.wire_fn, self.swdio, self.swclk, self.max_speed, self.console_fn,
                           self.mechanism, self.idle_clock)


@dataclass(kw_only=True)
class Wifi:
    """A Wi-Fi network the probe tries, in entry order (probe.config §1.4). passphrase None: keep the entry's
    passphrase (pass_len 0xFF - what get answers, so an item read back and sent again changes nothing); "" an open
    network. The passphrase is write-only: this client never shows or logs it (repr hides it)."""
    entry: int
    ssid: str
    passphrase: str | None = None
    NAME = "wifi"

    def __repr__(self) -> str:
        return f"Wifi(entry={self.entry}, ssid={self.ssid!r}, passphrase={'kept' if self.passphrase is None else '***'})"

    def key(self) -> int:
        return self.entry

    def data(self) -> bytes:
        ssid = self.ssid.encode()
        if not 1 <= len(ssid) <= reg.LIMITS["wifi_ssid_max_bytes"]:
            raise ValueError("ssid: 1 to 32 bytes")
        if self.passphrase is None:
            return bytes([len(ssid)]) + ssid + b"\xff"
        pw = self.passphrase.encode()
        if pw and not (8 <= len(pw) <= 63 or (len(pw) == 64 and all(c in "0123456789abcdefABCDEF" for c in self.passphrase))):
            raise ValueError("passphrase: none, 8 to 63 printable bytes, or 64 hex digits")
        return bytes([len(ssid)]) + ssid + bytes([len(pw)]) + pw


@dataclass(kw_only=True)
class Ipv4:
    """How netif `netif` (by name: "wifi", "ethernet" or a probe's own; looked up in describe's netif list) gets its
    IPv4 address (probe.config §1.5): DHCP (address None) or static `address` "a.b.c.d/prefix" with an optional gateway
    and dns. A netif without this item uses DHCP (§1.3)."""
    netif: str = "wifi"
    address: str | None = None
    gateway: str | None = None
    dns: str | None = None
    NAME = "ipv4"

    number = 0                     # the netif's number (from 1), filled in from describe by ProbeConfig

    def key(self) -> int:
        return self.number

    def data(self) -> bytes:
        if self.address is None:
            return bytes(14)
        net = ipaddress.IPv4Interface(self.address)
        four = lambda a: ipaddress.IPv4Address(a).packed if a else bytes(4)
        return bytes([1]) + net.ip.packed + bytes([net.network.prefixlen]) + four(self.gateway) + four(self.dns)


@dataclass
class Own:
    """A probe's own item (probe.config §1.6) by its reverse-DNS name: `value` str (text), int (u32), bool, bytes
    (bytes), or an ipaddress.IPv4Address (ipv4); None for a secret kept as it is. Encoded by the form the probe
    declares (`ProbeConfig` does it)."""
    name: str
    value: object = None
    key_: int = 0

    @property
    def NAME(self) -> str:
        return self.name

    def key(self) -> int:
        return self.key_

    def encode(self, form: int) -> bytes:
        v = self.value
        if v is None:
            return b""
        if form == FORM["text"]:
            return str(v).encode()
        if form == FORM["u32"]:
            return struct.pack("<I", int(v))
        if form == FORM["bool"]:
            return bytes([1 if v else 0])
        if form == FORM["ipv4"]:
            return ipaddress.IPv4Address(v).packed
        return bytes(v)


def item(it) -> bytes:
    """One standard item on the wire as a probe that lists the standard items in the registry's order numbers it (`ITEM`;
    tests and tools only): number(u8) len(u16) key(u16) data (probe.config §1)."""
    data = it.data()
    return struct.pack("<BHH", ITEM[it.NAME.replace("-", "_")], 2 + len(data), it.key()) + data


@dataclass(frozen=True)
class Removal:
    """What `remove()` makes: one key for unset. `set()` sends these as an unset after its set."""
    kind: str
    channel: int


def remove(kind: str, key: int) -> Removal:
    """The removal of the item of this key (for `unset`, or in a `set` list): a standard name or a probe's own."""
    return Removal(kind, key)


@dataclass(frozen=True)
class Setting:
    """One describe setting (probe.config §4): its item number is its place (from 1)."""
    number: int
    name: str
    form: int
    keys: int
    max_len: int
    flags: int

    @property
    def secret(self) -> bool:
        return bool(self.flags & FLAG["secret"])

    @property
    def restart(self) -> bool:
        return bool(self.flags & FLAG["restart"])


def decode(name: str, key: int, d: bytes, setting: Setting | None = None, netifs=()):
    """One item as one of the classes above (another: (name, key, data))."""
    try:
        if name == "label":
            return Label(channel=key, text=d.decode("utf-8", "replace"))
        if name == "idle" and len(d) == 1:
            return Idle(channel=key, mode=_name(IDLE, d[0]))
        if name == "disable" and not d:
            return Disable(channel=key)
        if name == "passthrough" and len(d) == 9:
            fn, rx, baud, fmt = struct.unpack("<HHIB", d)
            return Passthrough(port=key, fn=fn, rx=rx, baud=baud, format=fmt)
        if name == "console-passthrough" and len(d) == 14:
            wfn, swdio, swclk, speed, cfn, mech, idle = struct.unpack("<HHHIHBB", d)
            return ConsolePassthrough(port=key, wire_fn=wfn, swdio=swdio, swclk=swclk, max_speed=speed, console_fn=cfn,
                                      mechanism=mech, idle_clock=idle)
        if name == "wifi" and len(d) >= 2 and len(d) == 2 + d[0]:
            return Wifi(entry=key, ssid=d[1:1 + d[0]].decode("utf-8", "replace"),
                        passphrase=None if d[1 + d[0]] == 0xFF else "")
        if name == "ipv4" and len(d) == 14:
            netif = netifs[key - 1] if 1 <= key <= len(netifs) else str(key)
            if d[0] == 0:
                return Ipv4(netif=netif)
            ip = lambda b: str(ipaddress.IPv4Address(b)) if any(b) else None
            return Ipv4(netif=netif, address=f"{ip(d[1:5])}/{d[5]}", gateway=ip(d[6:10]), dns=ip(d[10:14]))
        if setting is not None and setting.form != FORM["named"]:
            f = setting.form
            value = (None if setting.secret and not d else d.decode("utf-8", "replace") if f == FORM["text"]
                     else struct.unpack("<I", d)[0] if f == FORM["u32"] else bool(d[0]) if f == FORM["bool"]
                     else ipaddress.IPv4Address(d) if f == FORM["ipv4"] else d)
            return Own(name, value, key)
    except (ValueError, struct.error, IndexError):
        pass
    return (name, key, d)


def split_items(data: bytes, count: int) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """`count` items from `data` -> (the (number, key, data) triples, what follows them: the answer's TLV tail)."""
    out, at = [], 0
    for _ in range(count):
        if at + 5 > len(data):
            raise m.ShortPayload(f"item {len(out)} of {count}: truncated")
        number, n, key = struct.unpack_from("<BHH", data, at)
        if n < 2 or at + 3 + n > len(data):
            raise m.ShortPayload(f"item {number}: truncated value")
        out.append((number, key, bytes(data[at + 5:at + 3 + n])))
        at += 3 + n
    return out, data[at:]


def _triple(it) -> tuple:
    if isinstance(it, tuple):
        return it
    if isinstance(it, Own):
        return (it.name, it.key(), repr(it.value))
    return (it.NAME, it.key(), it.data())


def same_items(a, b) -> bool:
    """Whether two configurations hold the same items (objects of the classes above or (name, key, data) triples),
    item by item (host guide §15)."""
    return sorted(map(_triple, a), key=repr) == sorted(map(_triple, b), key=repr)


@dataclass
class Declared:
    """What the probe's describe declares (fixed for one boot): the storage's size (0: no save / erase), the settings
    in their order (item number = place) and the netifs."""
    storage_bytes: int = 0
    settings: list[Setting] = field(default_factory=list)
    netifs: list[str] = field(default_factory=list)

    def netif_number(self, name: str) -> int:
        if name not in self.netifs:
            raise KeyError(f"the probe has no netif {name!r}")
        return self.netifs.index(name) + 1

    @property
    def items(self) -> list[str]:
        """The names of the items the probe takes, in its order."""
        return [s.name for s in self.settings]

    def by_name(self, name: str) -> Setting:
        for s in self.settings:
            if s.name == name:
                return s
        raise KeyError(f"the probe takes no item {name!r}")

    def by_number(self, number: int) -> Setting | None:
        return self.settings[number - 1] if 1 <= number <= len(self.settings) else None


@dataclass
class State:
    """The storage's state (op state, lock-free; probe.config §3): none, applied, unreadable or partial; the generation
    the saved settings were saved or applied at (0: none, unreadable or partial); why it is not applied whole."""
    storage: str = "none"          # none, applied, unreadable, partial
    saved_generation: int = 0
    reason: str | None = None      # form, item, capacity (None: none)


@dataclass
class NetifState:
    """One netif (op network, probe.config §2): state off / connecting / up / waiting, the wifi entry, the last
    failure's reason, rssi, and the address in use."""
    netif: str
    state: str
    entry: int | None
    reason: str
    rssi: int
    address: str | None
    gateway: str | None
    dns: str | None


@dataclass
class Network:
    tcp_port: int
    netifs: list[NetifState]


class ProbeConfig(Interface):
    NAME = "oep.probe.config"
    REVISION = 1
    GET, SET, SAVE, ERASE, UNSET, STATE, NETWORK = (_CFG.op[k] for k in ("get", "set", "save", "erase", "unset", "state",
                                                                         "network"))

    def _declared(self) -> Declared:
        """describe (core.describe keeps it while the probe's boot_id holds: the numbers are for that boot only)."""
        return self.describe()

    def wire(self, it) -> bytes:
        """One item on the wire: number(u8) len(u16) key(u16) data (probe.config §1); the number by name."""
        decl = self._declared()
        s = decl.by_name(it.NAME)
        if isinstance(it, Ipv4):
            it.number = decl.netif_number(it.netif)
        data = it.encode(s.form) if isinstance(it, Own) else it.data()
        return struct.pack("<BHH", s.number, 2 + len(data), it.key()) + data

    def get(self) -> tuple[int, list[tuple[str, int, bytes]]]:
        """-> (generation, the items as (name, key, data) in get's order), paged. No lock. Every page carries the
        generation; when it changes between pages the settings moved and the read starts over."""
        decl = self._declared()
        while True:
            out, first_gen = [], None
            while True:
                rd = m.Reader(self._call(self.GET, struct.pack("<H", len(out)), locked=False).payload)
                more, gen, count = rd.take("BIB")
                page, tail = split_items(rd.rest(), count)
                m.Tail.parse(tail)                                 # the answer's own TLV tail, not items
                if first_gen is None:
                    first_gen = gen
                elif gen != first_gen:
                    break                                          # changed under us: again from the start
                out += [((decl.by_number(n) or Setting(n, str(n), 0, 0, 0, 0)).name, k, d) for n, k, d in page]
                if not more or not page:
                    return gen, out

    def items(self) -> list:
        """The current settings, decoded (Label, Idle, Disable, Passthrough, ConsolePassthrough, Wifi, Ipv4, Own)."""
        decl = self._declared()
        named = {s.name: s for s in decl.settings}
        return [decode(n, k, d, named.get(n), decl.netifs) for n, k, d in self.get()[1]]

    def set(self, items: list) -> int:
        """Items (objects of the classes above, or item bytes) -> the new generation. Removals (`remove()`) in the list
        go as an unset after the set (two requests; each is atomic by itself). Needs the lock."""
        removals = [it for it in items if isinstance(it, Removal)]
        rest = [it for it in items if not isinstance(it, Removal)]
        gen = None
        if rest or not removals:
            body = b"".join(bytes(it) if isinstance(it, (bytes, bytearray)) else self.wire(it) for it in rest)
            self._check_fits(1 + len(body))
            gen = self._generation(self._call(self.SET, bytes([len(rest)]) + body))
        if removals:
            gen = self.unset([(r.kind, r.channel) for r in removals])
        return gen

    def _check_fits(self, size: int) -> None:
        """A set request longer than the probe's max_frame is refused here, before anything is sent (ValueError)."""
        size += m.REQUEST_HEADER
        limit = core.confirm(self.host)["max_frame"]
        if size > limit:
            raise ValueError(f"probe.config set of {size} bytes exceeds this transport's max_frame {limit}: "
                             "send fewer items per set")

    def unset(self, keys: list[tuple[str, int]]) -> int:
        """Remove the items of these (name, key) (probe.config §2 unset): a key that is not there is nothing; the
        whole must still be consistent or nothing changes. -> the new generation. Needs the lock."""
        decl = self._declared()
        body = bytes([len(keys)]) + b"".join(struct.pack("<BH", decl.by_name(n).number, k) for n, k in keys)
        return self._generation(self._call(self.UNSET, body))

    @staticmethod
    def _generation(r: m.Result) -> int:
        rd = m.Reader(r.payload)
        gen = rd.u32()
        rd.tail()
        return gen

    def save(self) -> int:
        """Save the current settings (a probe with storage; the whole is replaced). -> the current generation (which by
        itself does not say the save succeeded: completed failed does, and `state`). Its argument time is max_op_ms
        (probe.config §3, core §4.4)."""
        return self._generation(self._call(self.SAVE, expect_ms=core.max_op_ms(self.host)))

    def needs_save(self) -> bool:
        """Whether a save would change what is stored (host guide §15 step 4): not when the storage is applied and its
        saved_generation is the current generation (get's)."""
        st = self.state()
        return not (st.storage == "applied" and st.saved_generation == self.get()[0])

    def _triple(self, it) -> tuple[str, int, bytes]:
        if isinstance(it, tuple):
            return it
        decl = self._declared()
        s = decl.by_name(it.NAME)
        if isinstance(it, Ipv4):
            it.number = decl.netif_number(it.netif)
        return (it.NAME, it.key(), it.encode(s.form) if isinstance(it, Own) else it.data())

    def apply(self, wanted: list, save: bool = False) -> bool:
        """Make the probe's settings `wanted` (host guide §15): get, compared item by item (`same_items`); when they
        differ, set what is wanted and unset the keys get has and `wanted` does not; with `save`, save when
        `needs_save`. -> whether anything was sent. Needs the lock when something changes."""
        _, have = self.get()
        want = [self._triple(it) for it in wanted]
        changed = False
        if not same_items(have, want):
            keys = {(n, k) for n, k, _ in want}
            gone = sorted({(n, k) for n, k, _ in have} - keys)
            if gone:                                               # first, so a disable can replace an idle (§1)
                self.unset(gone)
            decl = self._declared()
            self.set([struct.pack("<BHH", decl.by_name(n).number, 2 + len(d), k) + d for n, k, d in want])
            changed = True
        if save and self.needs_save():
            self.save()
            changed = True
        return changed

    def erase(self) -> None:
        self._call(self.ERASE, expect_ms=core.max_op_ms(self.host))

    def describe(self) -> Declared:
        """The declarations (describe, cached by the host while the probe's boot_id holds)."""
        d = Declared()
        tlvs = core.describe(self.host, self.fn)
        storage = catalog.first_fixed(tlvs, DESCRIBE["storage"], 4)
        if storage is not None:
            d.storage_bytes = struct.unpack("<I", storage)[0]
        for tag, v in tlvs:
            if tag == DESCRIBE["setting"] and len(v) >= 6:
                form, keys, max_len, flags = struct.unpack_from("<BHBB", v)
                d.settings.append(Setting(len(d.settings) + 1, v[5:].decode("ascii", "replace"), form, keys, max_len, flags))
        d.netifs = [v.decode("utf-8", "replace") for tag, v in tlvs if tag == DESCRIBE["netif"]]
        return d

    def state(self) -> State:
        """The storage's state (op state, lock-free; probe.config §3): storage_state, saved_generation, reason."""
        rd = m.Reader(self._call(self.STATE, locked=False).payload)
        storage, saved, why = rd.take("BIB")
        rd.tail()
        return State(STORAGE_STATE.get(storage, str(storage)), saved, UNREADABLE.get(why, str(why)) if why else None)

    def network(self) -> Network:
        """The network now (op network, lock-free; probe.config §2): the TCP port and each netif's state."""
        netifs = self._declared().netifs
        rd = m.Reader(self._call(self.NETWORK, locked=False).payload)
        port, count = rd.take("HB")
        out = []
        ip = lambda b: str(ipaddress.IPv4Address(b)) if any(b) else None
        for _ in range(count):
            netif, st, entry, reason, rssi = rd.take("BBHBb")
            addr, pfx, gw, dns = rd.bytes(4), rd.u8(), rd.bytes(4), rd.bytes(4)
            out.append(NetifState(netifs[netif - 1] if 1 <= netif <= len(netifs) else str(netif), NET_STATE.get(st, str(st)), None if entry == 0xFFFF else entry,
                                  NET_REASON.get(reason, str(reason)), rssi,
                                  f"{ip(addr)}/{pfx}" if any(addr) else None, ip(gw), ip(dns)))
        rd.tail()
        return Network(port, out)


# The line names of the label convention (host guide §5.3): `nrst` a target's reset, `power_hi` high powers it,
# `power_lo` low powers it. With several targets, `<profile>.<name>` (the host's connection profile name).
LINE_NAMES = tuple(_CFG.line_names)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold_name(text: str) -> str:
    """ASCII case folded (only A-Z: the convention compares ignoring ASCII case)."""
    return text.translate(_ASCII_LOWER)


def ports_named(labels) -> list[str]:
    """The ports the settings' labels name (host guide §5.3): the distinct S of the "S.N" labels, folded, in order."""
    out = []
    for _, text in labels:
        port, dot, rest = text.partition(".")
        if dot and port and rest and fold_name(port) not in out:
            out.append(fold_name(port))
    return out


def line_from_labels(labels, name: str, profile: str | None = None, firmware=(), single: bool | None = None) -> int | None:
    """Host guide §5.3 on bare data: labels as (channel, text) - the settings' label items - firmware the firmware's
    fixed labels (fn 0 describe) as (channel, text). In order, stopping at the first step that finds exactly one
    channel: (a) with a port name (`profile`), a settings label equal to `<port>.<name>`; (b) only while at most one
    port is named (`single`; by default from the labels: the distinct S of "S.N"), a settings label equal to `name`;
    (c) the same, a firmware label equal to `name`. A step that finds two or more ends the search with none
    (ambiguous). Texts compare ignoring ASCII case."""
    if single is None:
        single = len(ports_named(labels)) <= 1
    steps = ([(labels, f"{profile}.{name}")] if profile is not None else []) + (
        [(labels, name), (firmware, name)] if single else [])
    for source, text in steps:
        found = {ch for ch, t in source if fold_name(t) == fold_name(text)}
        if len(found) > 1:
            return None                                            # ambiguous at this step: no such line
        if found:
            return found.pop()
    return None


def find_line(config, name: str, profile: str | None = None, firmware=None, single: bool | None = None) -> int | None:
    """The channel of the line `name` (nrst, power_hi, power_lo: LINE_NAMES) by the label convention (host guide §5.3),
    or None. config: a Host (its settings are read with `ProbeConfig.items()`, no lock) or the decoded items. profile:
    the port's name (the S of its "S.N" labels); `single`: whether at most one port is named (by default from the
    labels); firmware: the fixed
    labels as (channel, text) - read from the probe's describe when `config` is a Host, none when it is a list of items
    and this is not given."""
    if isinstance(config, (list, tuple)):
        items, fixed = config, list(firmware or ())
    else:
        items = ProbeConfig(config).items()
        fixed = list(firmware) if firmware is not None else core.firmware_labels(config)
    labels = [(it.channel, it.text) for it in items if isinstance(it, Label)]
    return line_from_labels(labels, name, profile, fixed, single)
