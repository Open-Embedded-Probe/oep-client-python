"""oep.probe.config revision 1 (oep-spec docs/oep-if-probe-config.ja.md): the probe's settings - plan, labels, idle
pins, slots, binds, fixture UART settings, disabled channels, Wi-Fi networks (the passphrase write-only) - read and set
as items, removed with unset, saved when the host says so, and
the live slot / bind / storage state as its own lock-free operation (describe is declarations only, core §7.3).

    cfg = config.ProbeConfig(hst)
    cfg.set([config.Slot(slot=0, wire_fn=wire_fn, pins=(2, 54), name="x035", attach="at-boot", retry_s=1),
             config.Bind(port=1, stream=("slot", 0))])
    if cfg.needs_save():
        cfg.save()
    cfg.items(), cfg.describe(), cfg.state()
    cfg.unset([("bind", 1)])          # or cfg.set([config.remove("bind", 1)])

An item goes as its TLV; a set replaces the keys it carries and keeps the others; the probe checks the whole and changes
nothing on a refusal. Every item has one form per tag (probe.config §1). The hash is the probe's own u32 that changes
with the settings (probe.config §2): a host never computes it - it compares the items themselves (`same_items`, host
guide §15) and uses the hash to see whether the settings moved (get's pages, storage_hash against get's hash).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import catalog, core, message as m, registry as reg
from .core import Interface
from .fixture import Drive

_CFG = reg.PROBE_CONFIG
ITEM = _CFG.tlv["item"]
DESCRIBE = _CFG.tlv["describe"]
STATE_TLV = _CFG.tlv["state_answer"]                            # the state answer's TLVs: wifi (probe.config §3.3)
ATTACH = {k.replace("_", "-"): v for k, v in _CFG.enum["slot_attach"].items()}
STREAM = {"slot": _CFG.enum["bind_stream"]["slot_console"], "uart": _CFG.enum["bind_stream"]["fixture_uart"]}
MECHANISM = {k: v for k, v in reg.TARGET_CONSOLE.enum["mechanism"].items()}      # includes "none" = 0xFF: no console
SLOT_STATE = {v: k.replace("_", "-") for k, v in _CFG.enum["slot_state"].items()}
BIND_FLOW = {v: k for k, v in _CFG.enum["bind_flow"].items()}
IDLE = {k.replace("_", "-"): v for k, v in _CFG.enum["idle_mode"].items()}
IDLE_CLOCK = dict(reg.WIRE_RVSWD.enum["idle_clock"])            # a slot's idle_clock (oep-if-debug §3)
STORAGE_STATE = {v: k for k, v in _CFG.enum["storage_state"].items()}
UNREADABLE = {1: "unreadable form", 2: "an interface it names is gone or of another revision", 3: "refused when applied"}
NEVER_NS = 0xFFFFFFFFFFFFFFFF                                   # last_try_at_ns: never tried
LABEL_MAX = reg.LIMITS["label_max_bytes"]                       # a label's text: 1 to 32 bytes (probe.config §1)
# slot wire_fn swdio swclk attach retry_ms max_speed_hz idle_clock mechanism name_len, then the name (probe.config §1.1)
SLOT_HEAD = struct.Struct("<BHHHBIIBBB")
BYTE_KEYED = (ITEM["slot"], ITEM["bind"], ITEM["wifi"])        # items keyed by their first byte (slot, port, index)
SSID_MAX = reg.LIMITS["wifi_ssid_max_bytes"]                    # a wifi item's ssid: 1 to 32 bytes (probe.config §1.4)
PASS_MIN, PASS_MAX = reg.LIMITS["wifi_passphrase_min_bytes"], reg.LIMITS["wifi_passphrase_max_bytes"]
PSK_HEX = reg.LIMITS["wifi_psk_hex_digits"]
WIFI_MIN_MAX_FRAME = reg.LIMITS["wifi_min_max_frame"]  # a probe with the wifi item answers max_frame >= this (§1.4)
PASS_SET = _CFG.enum["wifi_pass_len"]["hidden"]  # pass_len in get: a passphrase is set (none follows); in a set: keep it
WIFI_STATE = dict(_CFG.enum["wifi_state"])                      # off, connecting, connected, waiting
WIFI_REASON = {k.replace("_", "-"): v for k, v in _CFG.enum["wifi_reason"].items()}
NO_ENTRY = _CFG.enum["wifi_entry"]["none"]                      # the wifi state's entry: none


def _name(table: dict[str, int], value: int) -> str:
    return next((k for k, v in table.items() if v == value), str(value))


@dataclass(kw_only=True)
class Plan:
    fn: int
    role: int
    channel: int
    TAG = ITEM["plan"]

    def key(self) -> tuple:
        return (self.fn, self.role, self.channel)

    def value(self) -> bytes:
        return struct.pack("<HBH", self.fn, self.role, self.channel)


@dataclass(kw_only=True)
class Label:
    channel: int
    text: str
    TAG = ITEM["label"]

    def key(self) -> tuple:
        return (self.channel,)

    def value(self) -> bytes:
        raw = self.text.encode()
        if not 1 <= len(raw) <= LABEL_MAX or not m.valid_text(raw):
            # probe.config §1: 1 to 32 bytes (the probe refuses other lengths); this host sends no control characters
            raise ValueError(f"label {self.text!r}: 1 to {LABEL_MAX} bytes of text without control characters")
        return struct.pack("<H", self.channel) + raw


@dataclass(kw_only=True)
class Idle:
    """The state of a channel no plan or connection uses (probe.config §1): at boot and after every release. mode:
    hi-z, pull-up, pull-down, output-low, output-high (output_low / output_high too). An output mode keeps driving that
    level while the channel is free - a target's power switch kept on - and a gpio plan that takes the channel keeps
    it until its first set (fixture §1); a probe that cannot drive the channel refuses it unsupported.
    drive (output modes only): the strength it drives at (`fixture.Drive`, or an int level number; fixture §1.1) -
    also what a gpio set without its own drive uses on that channel. None: the default level (sent as 0xFF: the item
    is always 4 bytes, probe.config §1; an input mode's drive is not looked at). A level past the probe's drive_levels,
    or any level on a probe without them, is refused unsupported."""
    channel: int
    mode: str = "pull-up"          # hi-z, pull-up, pull-down, output-low, output-high
    drive: Drive | int | None = None
    TAG = ITEM["idle"]

    def __post_init__(self):
        self.mode = self.mode.replace("_", "-")
        if self.drive is not None:
            self.drive = Drive.of(self.drive)

    def key(self) -> tuple:
        return (self.channel,)

    def value(self) -> bytes:
        """channel(u16) mode(u8) drive(u8): 4 bytes (probe.config §1)."""
        if self.mode not in IDLE:
            raise ValueError(f"idle mode {self.mode!r}: one of {', '.join(IDLE)}")
        v = struct.pack("<HB", self.channel, IDLE[self.mode])
        drive = self.drive if self.drive is not None else Drive.default()
        if not drive.is_default and self.mode not in ("output-low", "output-high"):
            raise ValueError(f"idle mode {self.mode}: a drive goes with output-low / output-high only")
        return v + drive.pack()                                     # drive(u8): a level, 0xFF the default


@dataclass(kw_only=True)
class Disable:
    """A channel the probe never uses or touches (probe.config §1, item 0x07): not on this board, or wired to another
    part. Any request naming it is refused unavailable (cause 5, held by settings); describe still declares it."""
    channel: int
    TAG = ITEM["disable"]

    def key(self) -> tuple:
        return (self.channel,)

    def value(self) -> bytes:
        return struct.pack("<H", self.channel)


@dataclass(kw_only=True)
class Slot:
    """A place a target is wired to (probe.config §1.1; the probe checks no target - a host does, with connections'
    tid). pins: (swdio, swclk), swclk 0xFFFF on one wire (swio). retry_s goes on the wire as retry_ms (u32; at-boot
    slots only, 0 on a host slot), max_speed as max_speed_hz (u32). mechanism "none": no console on this slot."""
    slot: int
    wire_fn: int
    pins: tuple[int, int]
    name: str
    attach: str = "host"           # host, at-boot
    retry_s: float = 0             # at-boot: try again every retry_s while the target is not there (0: never)
    mechanism: str = "dmseq"       # sdi, dmdata, dmseq, none
    max_speed: int = 0             # the line's ceiling in Hz for the probe's own attach (0: none) - the target's
    idle_clock: str = "high"       # rvswd: SWCLK while the line rests, high / low - the target's (oep-if-debug §3)
    TAG = ITEM["slot"]

    def key(self) -> tuple:
        return (self.slot,)

    def value(self) -> bytes:
        """The fixed fields, then the name (the item ends with it, probe.config §1.1)."""
        name = self.name.encode()
        retry_ms = round(self.retry_s * 1000) if self.attach == "at-boot" else 0
        return SLOT_HEAD.pack(self.slot, self.wire_fn, *self.pins, ATTACH[self.attach], retry_ms, self.max_speed,
                              IDLE_CLOCK[self.idle_clock], MECHANISM[self.mechanism], len(name)) + name


@dataclass(kw_only=True)
class Bind:
    """The one stream serial port `port` (the describe transport index) carries (probe.config §1.2): ("slot", n) - a
    slot's console - or ("uart", fn) - a fixture UART's RX. Another stream: set the bind again."""
    port: int
    stream: tuple[str, int]
    TAG = ITEM["bind"]

    def key(self) -> tuple:
        return (self.port,)

    def value(self) -> bytes:
        kind, i = self.stream
        return struct.pack("<BBH", self.port, STREAM[kind], i)    # port(u8) kind(u8) id(u16)


@dataclass(kw_only=True)
class Uart:
    """A fixture UART's baud and format (probe.config §1, item 0x06): applied whenever that fn's plan gets RX or TX
    (a session's configure wins until the plan is released). format: fixture.FixtureUart.format_byte()."""
    fn: int
    baud: int
    format: int = 0
    TAG = ITEM["uart"]

    def key(self) -> tuple:
        return (self.fn,)

    def value(self) -> bytes:
        return struct.pack("<HIB", self.fn, self.baud, self.format)


class _Keep:
    """A wifi item's passphrase as get shows it: one is set, and a set carrying this keeps it (pass_len 0xFF)."""

    def __repr__(self) -> str:
        return "KEEP"

    def __reduce__(self):
        return "KEEP"


KEEP = _Keep()


def check_passphrase(raw: bytes) -> None:
    """A passphrase as the wifi item takes it (probe.config §1.4): 8 to 63 bytes of 0x20-0x7E, or 64 hex digits. The
    message never carries the passphrase."""
    if len(raw) == PSK_HEX and all(chr(b) in "0123456789abcdefABCDEF" for b in raw):
        return
    if not PASS_MIN <= len(raw) <= PASS_MAX or any(b < 0x20 or b > 0x7E for b in raw):
        raise ValueError(f"wifi passphrase ({len(raw)} bytes): 8 to 63 printable ASCII characters or 64 hex digits")


@dataclass(kw_only=True, repr=False)
class Wifi:
    """A network the probe joins to serve OEP over TCP (the wifi item, key index; tried in index order). ssid: 1 to 32
    bytes. passphrase: None for an open network, `KEEP` for the one the entry has already (get's form: a set with it
    changes nothing of the passphrase), else 8-63 printable ASCII characters or 64 hex digits.

    The passphrase is write-only: get never returns it (pass_len 0xFF: one is set, 0: none). Nothing here prints it:
    repr and `shown()` say "set" or "none"."""
    index: int
    ssid: str | bytes
    passphrase: str | bytes | _Keep | None = None
    TAG = ITEM["wifi"]

    def key(self) -> tuple:
        return (self.index,)

    @property
    def ssid_bytes(self) -> bytes:
        return self.ssid if isinstance(self.ssid, bytes) else self.ssid.encode()

    @property
    def has_passphrase(self) -> bool:
        return self.passphrase is KEEP or bool(self.passphrase)

    def shown(self) -> dict:
        """What may be printed: index, ssid (safe text), passphrase "set" / "none"."""
        return {"index": self.index, "ssid": m.shown(self.ssid_bytes),
                "passphrase": "set" if self.has_passphrase else "none"}

    def __repr__(self) -> str:
        s = self.shown()
        return f"Wifi(index={s['index']}, ssid={s['ssid']!r}, passphrase={s['passphrase']})"

    def value(self) -> bytes:
        """index(u8) ssid_len(u8) ssid pass_len(u8) passphrase (pass_len 0xFF and nothing after: keep it)."""
        ssid = self.ssid_bytes
        if not 1 <= len(ssid) <= SSID_MAX:
            raise ValueError(f"wifi ssid {m.shown(ssid)!r}: 1 to {SSID_MAX} bytes")
        if not 0 <= self.index < 0xFF:
            raise ValueError(f"wifi index {self.index}: 0 to 254 (below the probe's wifi_max)")
        head = bytes([self.index, len(ssid)]) + ssid
        if self.passphrase is KEEP:
            return head + bytes([PASS_SET])
        if not self.passphrase:
            return head + b"\x00"
        raw = self.passphrase if isinstance(self.passphrase, bytes) else self.passphrase.encode()
        check_passphrase(raw)
        return head + bytes([len(raw)]) + raw


def wifi_get_form(value: bytes) -> bytes:
    """A wifi item's value as get shows it: the passphrase replaced by pass_len 0xFF when there is one (a value too
    short for its counts is returned as it is)."""
    if len(value) < 3 or len(value) < 3 + value[1]:
        return bytes(value)
    pass_len = value[2 + value[1]]
    return bytes(value[:2 + value[1]]) + bytes([PASS_SET if pass_len else 0])


def wifi_from_env(environ=None, count: int = 8) -> list[Wifi]:
    """The wifi entries the environment gives: OEP_WIFI_SSID_<n> and OEP_WIFI_PASS_<n> (n = the index, 0 to count-1;
    no PASS: an open network). Read for a bench's set-up (`oep config wifi --from-env`, tests/hw); the values are never
    printed."""
    import os
    env = os.environ if environ is None else environ
    out = []
    for n in range(count):
        ssid = env.get(f"OEP_WIFI_SSID_{n}")
        if ssid:
            out.append(Wifi(index=n, ssid=ssid, passphrase=env.get(f"OEP_WIFI_PASS_{n}") or None))
    return out


@dataclass(frozen=True)
class Removal:
    """What `remove()` makes: one key for unset (op 0x05). `set()` sends these as an unset after its set."""
    kind: str
    key: int

    def encoded(self) -> bytes:
        """len(u8) tag(u8) key, len the key's length (probe.config §2): the key is fn(u16) for plan / uart, channel(u16)
        for label / idle / disable, slot(u8), port(u8), index(u8) for wifi."""
        tag = ITEM[self.kind]
        key = bytes([self.key]) if tag in BYTE_KEYED else struct.pack("<H", self.key)
        return bytes([len(key), tag]) + key


def item(it) -> bytes:
    return m.tlv(it.TAG, it.value())


def remove(kind: str, key: int) -> Removal:
    """The removal of the item of this key (for `unset`, or in a `set` list): kind plan (key fn: its whole plan), label
    / idle / disable (channel), slot, bind (port), uart (fn), wifi (index)."""
    if kind not in ITEM:
        raise ValueError(f"no item kind {kind!r}")
    return Removal(kind, key)


def decode(tag: int, v: bytes):
    """One item as one of the classes above (an unknown tag: (tag, value))."""
    if tag == ITEM["plan"] and len(v) >= 5:
        fn, role, channel = struct.unpack_from("<HBH", v)
        return Plan(fn=fn, role=role, channel=channel)
    if tag == ITEM["label"] and len(v) >= 2:
        return Label(channel=struct.unpack_from("<H", v)[0], text=v[2:].decode("utf-8", "replace"))
    if tag == ITEM["idle"] and len(v) >= 4:
        drive = Drive.unpack(v[3:4])
        return Idle(channel=struct.unpack_from("<H", v)[0], mode=_name(IDLE, v[2]),
                    drive=None if drive.is_default else drive)
    if tag == ITEM["disable"] and len(v) >= 2:
        return Disable(channel=struct.unpack_from("<H", v)[0])
    if tag == ITEM["slot"] and len(v) >= SLOT_HEAD.size:
        n, wire_fn, swdio, swclk, attach, retry_ms, max_speed, idle, mech, name_len = SLOT_HEAD.unpack_from(v)
        name = v[SLOT_HEAD.size:SLOT_HEAD.size + name_len].decode("ascii", "replace")
        retry_s = retry_ms / 1000
        return Slot(slot=n, wire_fn=wire_fn, pins=(swdio, swclk), name=name, attach=_name(ATTACH, attach),
                    retry_s=int(retry_s) if retry_s == int(retry_s) else retry_s, mechanism=_name(MECHANISM, mech),
                    max_speed=max_speed, idle_clock=_name(IDLE_CLOCK, idle))
    if tag == ITEM["bind"] and len(v) >= 4:
        port, kind, i = struct.unpack_from("<BBH", v)
        return Bind(port=port, stream=(_name(STREAM, kind), i))
    if tag == ITEM["uart"] and len(v) >= 7:
        fn, baud, fmt = struct.unpack_from("<HIB", v)
        return Uart(fn=fn, baud=baud, format=fmt)
    if tag == ITEM["wifi"] and len(v) >= 3 and len(v) >= 3 + v[1]:
        pass_len = v[2 + v[1]]
        # get carries no passphrase (pass_len 0xFF: set); one a probe sent anyway is kept, never shown
        passphrase = KEEP if pass_len == PASS_SET else (bytes(v[3 + v[1]:3 + v[1] + pass_len]) or KEEP) if pass_len else None
        return Wifi(index=v[0], ssid=bytes(v[2:2 + v[1]]).decode("utf-8", "replace"), passphrase=passphrase)
    return (tag, v)


def _sort_key(tag: int, value: bytes) -> tuple:
    """get's order of one item (probe.config §2): tag, then the key - plan (fn, role, channel), label / idle / disable
    channel, slot, port, uart fn."""
    if tag == ITEM["plan"] and len(value) >= 5:
        return (tag,) + struct.unpack_from("<HBH", value)
    if tag in BYTE_KEYED:
        return (tag, value[0] if value else -1)
    return (tag, struct.unpack_from("<H", value)[0] if len(value) >= 2 else -1)


def _pairs(items, as_get: bool = False) -> list[tuple[int, bytes]]:
    """Items (objects of the classes above, or (tag, value) pairs) as (tag without bit 7, value), in get's order.
    as_get: a wifi item's passphrase as get shows it (`wifi_get_form`)."""
    pairs = [(it[0] & 0x7F, bytes(it[1])) if isinstance(it, tuple) else (it.TAG, it.value()) for it in items]
    if as_get:
        pairs = [(t, wifi_get_form(v) if t == ITEM["wifi"] else v) for t, v in pairs]
    return sorted(pairs, key=lambda p: _sort_key(*p))


_KIND = {v: k for k, v in ITEM.items()}


def _key_of(tag: int, value: bytes) -> tuple[str, int]:
    """The (kind, key) an unset names for an item (probe.config §2): plan and uart fn(u16), label / idle / disable
    channel(u16), slot and bind their first byte."""
    if tag in BYTE_KEYED:
        return _KIND[tag], value[0]
    return _KIND.get(tag, str(tag)), struct.unpack_from("<H", value)[0]


def same_items(a, b) -> bool:
    """Whether two configurations hold the same items, item by item (host guide §15: a host compares what it wants with
    get's items; the probe's hash is its own and is never computed here). A wifi item compares as get shows it: its
    passphrase is write-only, so only whether one is set counts - a changed passphrase of the same entry is not seen
    (set it with `set`)."""
    return _pairs(a, as_get=True) == _pairs(b, as_get=True)


@dataclass
class SlotState:
    """One slot's state (probe.config §3.3): connected (a connection on its place - which target, the host checks with
    connections' tid) or absent; last_try_at_ns None: never tried (also an at-boot slot the probe could not try yet)."""
    slot: int
    state: str                     # connected, absent
    connection: int
    last_try_at_ns: int | None     # the probe's clock when it last tried an automatic attach (None: never tried)


@dataclass
class BindState:
    port: int
    flow: str                      # idle, streaming, held


@dataclass
class Declared:
    """What the probe's describe declares (fixed for one boot): the storage's size, the item tags it takes, how many
    slots, how many wifi entries (0: no wifi item)."""
    storage_bytes: int = 0
    items: list[int] = field(default_factory=list)
    slots_max: int = 0
    wifi_max: int = 0


@dataclass
class WifiState:
    """The probe's Wi-Fi link (the state answer's wifi TLV, probe.config §3.3): state off / connecting / connected /
    waiting (every entry failed; it waits, then tries again), the entry (index) in use or being tried (None: none), why the last try failed (reason
    none, not-found, auth, no-address, other), rssi in dBm and the IPv4 address while connected (None otherwise)."""
    state: str
    entry: int | None
    reason: str
    rssi: int | None
    ipv4: str | None

    @classmethod
    def unpack(cls, v: bytes) -> WifiState:
        state, entry, reason, rssi = struct.unpack_from("<BBBb", v)
        ip = ".".join(str(b) for b in v[4:8])
        connected = state == WIFI_STATE["connected"]
        return cls(_name(WIFI_STATE, state), None if entry == NO_ENTRY else entry, _name(WIFI_REASON, reason),
                   rssi if connected and rssi else None, ip if connected and ip != "0.0.0.0" else None)

    def text(self) -> str:
        out = self.state + (f", entry {self.entry}" if self.entry is not None else "")
        out += f", reason {self.reason}" if self.reason != "none" else ""
        out += f", rssi {self.rssi} dBm" if self.rssi is not None else ""
        return out + (f", ip {self.ipv4}" if self.ipv4 else "")


@dataclass
class State:
    """The live state (op state, lock-free; probe.config §3.3): the saved settings and the slots and binds."""
    storage: str = "none"          # none, applied, unreadable
    saved_hash: int = 0            # get's hash when the saved settings became the current ones (0: none / unreadable)
    unreadable: str | None = None  # why, when unreadable
    slots: list[SlotState] = field(default_factory=list)
    binds: list[BindState] = field(default_factory=list)
    wifi: WifiState | None = None  # the Wi-Fi link, on a probe with the wifi item (the last page's)


class ProbeConfig(Interface):
    NAME = "oep.probe.config"
    REVISION = 1
    GET, SET, SAVE, ERASE, UNSET, STATE = (_CFG.op[k] for k in ("get", "set", "save", "erase", "unset", "state"))

    def get(self) -> tuple[int, list[tuple[int, bytes]]]:
        """-> (hash, the items as (tag, value) in get's order: tag, then key), paged. No lock. Every page carries the
        same hash; when it changes between pages the probe's settings moved and the read starts over."""
        while True:
            out, h, first_hash = [], 0, None
            while True:
                rd = m.Reader(self._call(self.GET, struct.pack("<H", len(out)), locked=False).payload)
                more, h = rd.take("BI")
                page = catalog.split_tlv(rd.rest())
                if first_hash is None:
                    first_hash = h
                elif h != first_hash:
                    break                                          # changed under us: again from the start
                out += page
                if not more or not page:
                    return h, out

    def items(self) -> list:
        """The current settings, decoded (Plan, Label, Idle, Slot, Bind, Uart, Disable, Wifi)."""
        return [decode(t, v) for t, v in self.get()[1]]

    def set(self, items: list) -> int:
        """Items (objects of the classes above, or item TLV bytes) -> the new hash. Removals (`remove()`) in the list go
        as an unset after the set (two requests; each is atomic by itself). Needs the lock."""
        removals = [it for it in items if isinstance(it, Removal)]
        rest = [it for it in items if not isinstance(it, Removal)]
        h = None
        if rest or not removals:
            body = b"".join(it if isinstance(it, (bytes, bytearray)) else item(it) for it in rest)
            self._check_fits(body)
            h = self._hash_answer(self._call(self.SET, body))
        if removals:
            h = self.unset([(r.kind, r.key) for r in removals])
        return h

    def _check_fits(self, body: bytes) -> None:
        """A set request longer than the probe's max_frame is refused here, before anything is sent (ValueError): the
        probe could not take it. One set of the longest wifi item is wifi_min_max_frame (112) bytes, and a probe with
        the wifi item answers at least that on every transport (probe.config §1.4); several items may need several
        sets."""
        size = m.REQUEST_HEADER + len(self.prefix) + len(body)
        limit = core.confirm(self.host)["max_frame"]
        if size > limit:
            wifi = any(t & 0x7F == ITEM["wifi"] for t, _ in catalog.split_tlv(body))
            raise ValueError(
                f"probe.config set of {size} bytes exceeds this transport's max_frame {limit}: "
                + (f"a probe with the wifi item answers max_frame {WIFI_MIN_MAX_FRAME} or more on every transport "
                   "(probe.config §1.4) - this one does not; " if wifi and limit < WIFI_MIN_MAX_FRAME else "")
                + "send fewer items per set")

    def unset(self, keys: list[tuple[str, int]]) -> int:
        """Remove the items of these (kind, key) (probe.config §2 unset): a key that is not there is nothing; the
        whole must still be consistent or nothing changes. -> the new hash. Needs the lock."""
        body = bytes([len(keys)]) + b"".join(Removal(kind, key).encoded() for kind, key in keys)
        return self._hash_answer(self._call(self.UNSET, body))

    @staticmethod
    def _hash_answer(r: m.Result) -> int:
        rd = m.Reader(r.payload)
        h = rd.u32()
        rd.tail()
        return h

    def save(self) -> int:
        """Save the current settings (a probe with storage; the whole is replaced). -> the hash saved. The probe answers
        nothing while it writes: its argument time is max_op_ms (probe.config §2, core §4.4)."""
        return self._hash_answer(self._call(self.SAVE, expect_ms=core.max_op_ms(self.host)))

    def needs_save(self) -> bool:
        """Whether a save would change what is stored (host guide §15 step 5): not when the storage is applied and its
        storage_hash is the current settings' (get's) hash. A flash write wears it and stops the probe meanwhile."""
        st = self.state()
        return not (st.storage == "applied" and st.saved_hash == self.get()[0])

    def apply(self, wanted: list, save: bool = False) -> bool:
        """Make the probe's settings `wanted` (host guide §15): get, compared item by item (`same_items`; a wifi item by
        its ssid and whether it has a passphrase); when they differ, set what is wanted - a wifi entry the probe has as
        wanted with pass_len 0xFF, so no passphrase is sent again - and unset the keys get has and `wanted` does not;
        with `save`, save when `needs_save`. -> whether anything was sent. Needs the lock when something changes."""
        _, have = self.get()
        changed = False
        if not same_items(have, wanted):
            want = _pairs(wanted)
            keys = {_key_of(t, v) for t, v in want}
            # a wifi entry the probe has as wanted (ssid, passphrase or none) goes in get's form: its passphrase is
            # sent only for an entry that changes (host guide §15.1)
            had = {_key_of(t, v): v for t, v in _pairs(have, as_get=True)}
            want = [(t, wifi_get_form(v)) if t == ITEM["wifi"] and had.get(_key_of(t, v)) == wifi_get_form(v) else (t, v)
                    for t, v in want]
            self.set([m.tlv(t, v) for t, v in want])
            gone = sorted({_key_of(t, v) for t, v in have} - keys)
            if gone:
                self.unset([(kind, key) for kind, key in gone])
            changed = True
        if save and self.needs_save():
            self.save()
            changed = True
        return changed

    def erase(self) -> None:
        self._call(self.ERASE)

    def describe(self) -> Declared:
        """The declarations (describe, cached by the host while the probe's boot_id holds)."""
        d = Declared()
        for tag, v in core.describe(self.host, self.fn):
            tag &= 0x7F
            if tag == DESCRIBE["storage"] and len(v) >= 4:
                d.storage_bytes = struct.unpack_from("<I", v)[0]
            elif tag == DESCRIBE["items"]:
                d.items = list(v)
            elif tag == DESCRIBE["slots_max"] and v:
                d.slots_max = v[0]
            elif tag == DESCRIBE["wifi_max"] and v:
                d.wifi_max = v[0]
        return d

    def state(self) -> State:
        """The storage's state, the live slot_state / bind_state (op state, lock-free, paged by first_slot /
        first_bind) and, on a probe with the wifi item, the Wi-Fi link (`WifiState`). Each page carries storage_state, storage_hash and unreadable_reason as they were when it was
        answered: the last page's are kept (probe-config §3.3, PC-9). The slots and binds may change between pages
        too; a caller that needs them to stay the same pages while it holds the lock."""
        st = State()
        first_slot = first_bind = 0
        while True:
            rd = m.Reader(self._call(self.STATE, bytes([first_slot, first_bind]), locked=False).payload)
            more, storage, st.saved_hash, why = rd.take("BBIB")
            st.storage = STORAGE_STATE.get(storage, str(storage))
            st.unreadable = UNREADABLE.get(why, str(why)) if why else None
            n_slots = rd.u8()
            for _ in range(n_slots):                               # count x slot_state, no element length (§2.3)
                n, state, conn, tried = rd.take("BBHQ")
                st.slots.append(SlotState(n, SLOT_STATE.get(state, str(state)), conn,
                                          None if tried == NEVER_NS else tried))
            n_binds = rd.u8()
            for _ in range(n_binds):
                port, flow = rd.take("BB")
                st.binds.append(BindState(port, BIND_FLOW.get(flow, str(flow))))
            wifi = rd.tail().get(STATE_TLV["wifi"])
            if wifi is not None and len(wifi) >= 8:
                st.wifi = WifiState.unpack(wifi)
            if not more or not (n_slots or n_binds):
                return st
            first_slot += n_slots
            first_bind += n_binds


# The line names of the label convention (probe.config §1.3): `nrst` a target's reset, `power_hi` high powers it,
# `power_lo` low powers it. Per slot `<slot name>.<name>`; the bare name on settings with at most one slot.
LINE_NAMES = tuple(_CFG.line_names)                 # the registry's standard names; private ones start with x- (PC-2)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold_name(text: str) -> str:
    """ASCII case folded (only A-Z: probe.config §1.3 compares ignoring ASCII case)."""
    return text.translate(_ASCII_LOWER)


def line_from_labels(labels, n_slots: int, slot_name: str | None, name: str, firmware=()) -> int | None:
    """probe.config §1.3 on bare data: labels as (channel, text) - the settings' label items - n_slots the settings'
    slot items, firmware the firmware's fixed labels (fn 0 describe 0x46) as (channel, text). In order, stopping at
    the first step that finds exactly one channel: (a) a settings label equal to `<slot_name>.<name>`; (b) only with at
    most one slot item, a settings label equal to `name`; (c) only with at most one slot item, a firmware label equal
    to `name`. A step that finds two or more ends the search with none (no fall-through). Texts compare ignoring ASCII
    case. slot_name None (settings without slot items): steps (b) and (c)."""
    steps = ([(labels, f"{slot_name}.{name}")] if slot_name is not None else []) + (
        [(labels, name), (firmware, name)] if n_slots <= 1 else [])
    for source, text in steps:
        found = {ch for ch, t in source if fold_name(t) == fold_name(text)}
        if len(found) > 1:
            return None                                            # ambiguous at this step: no such line
        if found:
            return found.pop()
    return None


def find_line(config, slot_name: str | int | None, name: str, firmware=None) -> int | None:
    """The channel of the line `name` (nrst, power_hi, power_lo: LINE_NAMES) of a slot by the label convention
    (probe.config §1.3), or None when that slot has no such line. config: a Host (its settings are read with
    `ProbeConfig.items()`, no lock) or the decoded items. slot_name: the slot's name or number; None on settings with
    no slot item (the target connected to the probe) or one (that slot).

    `<slot>.<name>` first, ignoring ASCII case; then the bare `name`, only when the settings hold at most one slot item;
    then (PC-1) the firmware's fixed label equal to `name` (fn 0 describe 0x46), on the same condition; two or more
    channels matching at one step mean no such line. firmware: the fixed labels as (channel, text) - read from the
    probe's describe when `config` is a Host, none when it is a list of items and this is not given. Raises ValueError for slot_name None with several
    slots, LookupError for a slot number not in the settings."""
    if isinstance(config, (list, tuple)):
        items, fixed = config, list(firmware or ())
    else:
        items = ProbeConfig(config).items()
        fixed = list(firmware) if firmware is not None else core.firmware_labels(config)
    labels = [(it.channel, it.text) for it in items if isinstance(it, Label)]
    slots = [it for it in items if isinstance(it, Slot)]
    if isinstance(slot_name, int):
        named = [s.name for s in slots if s.slot == slot_name]
        if not named:
            raise LookupError(f"no slot {slot_name} in the probe's settings")
        slot_name = named[0]
    if slot_name is None and len(slots) > 1:
        raise ValueError(f"{name}: the settings hold {len(slots)} slots; name the slot")
    if slot_name is None and slots:
        slot_name = slots[0].name
    return line_from_labels(labels, len(slots), slot_name, name, fixed)
