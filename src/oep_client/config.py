"""oep.probe.config revision 1 (oep-spec docs/oep-if-probe-config.ja.md): the probe's settings - plan, labels, idle
pins, slots, binds, fixture UART settings - read and set as items, removed with unset, saved when the host says so, and
the live slot / bind / storage state as its own lock-free operation (describe is declarations only, core §7.3).

    cfg = config.ProbeConfig(hst)
    cfg.set([config.Slot(slot=0, wire_fn=wire_fn, pins=(2, 54), name="x035", attach="at-boot", retry_s=1),
             config.Bind(port=1, mode="last-reset", streams=[("slot", 0)])])
    cfg.save()
    cfg.items(), cfg.describe(), cfg.state()
    cfg.unset([("bind", 1)])          # or cfg.set([config.remove("bind", 1)])

An item goes as its TLV; a set replaces the keys it carries and keeps the others; the probe checks the whole and changes
nothing on a refusal. The probe keeps each item's bytes as sent and hashes the canonical form (tag order, key order, the
one TLV encoding): `hash_of(items)` computes the same value here.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

from . import catalog, core, message as m, registry as reg
from .core import Interface

_CFG = reg.PROBE_CONFIG
ITEM = _CFG.tlv["item"]
DESCRIBE = _CFG.tlv["describe"]
ATTACH = {k.replace("_", "-"): v for k, v in _CFG.enum["slot_attach"].items()}
MODE = {k.replace("_", "-"): v for k, v in _CFG.enum["bind_mode"].items()}
STREAM = {"slot": _CFG.enum["bind_stream"]["slot_console"], "uart": _CFG.enum["bind_stream"]["fixture_uart"]}
MECHANISM = {k: v for k, v in reg.TARGET_CONSOLE.enum["mechanism"].items()}      # includes "none" = 0xFF: no console
SLOT_STATE = {v: k.replace("_", "-") for k, v in _CFG.enum["slot_state"].items()}
BIND_FLOW = {v: k for k, v in _CFG.enum["bind_flow"].items()}
IDLE = {k.replace("_", "-"): v for k, v in _CFG.enum["idle_mode"].items()}
IDLE_CLOCK = dict(reg.WIRE_RVSWD.enum["idle_clock"])            # a slot's idle_clock (oep-if-debug §3)
STORAGE_STATE = {v: k for k, v in _CFG.enum["storage_state"].items()}
UNREADABLE = {1: "unreadable form", 2: "an interface it names is gone or of another revision", 3: "refused when applied"}
NEVER_NS = 0xFFFFFFFFFFFFFFFF                                   # last_try_at_ns: never tried
SLOT_HEAD = struct.Struct("<BHHHBIIBBB")   # slot wire_fn swdio swclk attach retry_ms max_speed_hz idle_clock mechanism name_len


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
        return struct.pack("<H", self.channel) + self.text.encode()


@dataclass(kw_only=True)
class Idle:
    channel: int
    mode: str = "pull-up"          # hi-z, pull-up, pull-down
    TAG = ITEM["idle"]

    def key(self) -> tuple:
        return (self.channel,)

    def value(self) -> bytes:
        return struct.pack("<HB", self.channel, IDLE[self.mode])


@dataclass(kw_only=True)
class Slot:
    """A place a target is wired to (probe.config §1.1). pins: (swdio, swclk), swclk 0xFFFF on one wire (swio).
    lock: (scheme, mask, value) - the target_id a connection must show, e.g. (1, mask u32 LE, value u32 LE); mask and
    value are as long as the scheme's value (4 bytes for scheme 1). retry_s goes on the wire as retry_ms (u32),
    max_speed as max_speed_hz (u32). mechanism "none": no console on this slot."""
    slot: int
    wire_fn: int
    pins: tuple[int, int]
    name: str
    attach: str = "host"           # host, at-boot
    retry_s: float = 0             # at-boot: try again every retry_s while the target is not there (0: never)
    mechanism: str = "dmseq"       # sdi, dmdata, dmseq, none
    lock: tuple[int, bytes, bytes] | None = None
    max_speed: int = 0             # the line's ceiling in Hz for the probe's own attach (0: none) - the target's
    idle_clock: str = "high"       # rvswd: SWCLK while the line rests, high / low - the target's (oep-if-debug §3)
    TAG = ITEM["slot"]

    def key(self) -> tuple:
        return (self.slot,)

    def value(self) -> bytes:
        name = self.name.encode()
        retry_ms = round(self.retry_s * 1000) if self.attach == "at-boot" else 0
        v = SLOT_HEAD.pack(self.slot, self.wire_fn, *self.pins, ATTACH[self.attach], retry_ms, self.max_speed,
                           IDLE_CLOCK[self.idle_clock], MECHANISM[self.mechanism], len(name)) + name
        if self.lock is None:
            return v + b"\x00"                                    # lock_len 0: no lock
        scheme, mask, value = self.lock
        if len(mask) != len(value) or not mask or not scheme:
            raise ValueError("a lock has a scheme and a mask and value of the same length, at least 1 byte")
        return v + bytes([1 + 2 * len(mask), scheme]) + mask + value


@dataclass(kw_only=True)
class Bind:
    """What serial port `port` (the describe transport index) carries (probe.config §1.2). streams: ("slot", n) or
    ("uart", fn); selected: manual's choice (an index into streams)."""
    port: int
    mode: str = "last-reset"       # last-reset, manual, mixed
    streams: list[tuple[str, int]] = field(default_factory=list)
    selected: int = 0
    TAG = ITEM["bind"]

    def key(self) -> tuple:
        return (self.port,)

    def value(self) -> bytes:
        v = struct.pack("<BBBB", self.port, MODE[self.mode], self.selected if self.mode == "manual" else 0,
                        len(self.streams))
        return v + b"".join(struct.pack("<BBH", 3, STREAM[kind], i) for kind, i in self.streams)   # len, kind, id


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


@dataclass(frozen=True)
class Removal:
    """What `remove()` makes: one key for unset (op 0x05). `set()` sends these as an unset after its set."""
    kind: str
    key: int

    def encoded(self) -> bytes:
        """len(u8) tag(u8) key: the key is fn(u16) for plan / uart, channel(u16) for label / idle, slot(u8), port(u8)."""
        tag = ITEM[self.kind]
        key = bytes([self.key]) if self.kind in ("slot", "bind") else struct.pack("<H", self.key)
        return bytes([1 + len(key), tag]) + key


def item(it) -> bytes:
    return m.tlv(it.TAG, it.value())


def remove(kind: str, key: int) -> Removal:
    """The removal of the item of this key (for `unset`, or in a `set` list): kind plan (key fn: its whole plan), label
    / idle (channel), slot, bind (port), uart (fn)."""
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
    if tag == ITEM["idle"] and len(v) >= 3:
        return Idle(channel=struct.unpack_from("<H", v)[0], mode=_name(IDLE, v[2]))
    if tag == ITEM["slot"] and len(v) >= SLOT_HEAD.size + 1:
        n, wire_fn, swdio, swclk, attach, retry_ms, max_speed, idle, mech, name_len = SLOT_HEAD.unpack_from(v)
        at = SLOT_HEAD.size
        name = v[at:at + name_len].decode("ascii", "replace")
        at += name_len
        lock_len = v[at] if at < len(v) else 0
        part = v[at + 1:at + 1 + lock_len]                         # after it: later fields (core §2.3), skipped
        lock = None
        if lock_len >= 3:
            half = (lock_len - 1) // 2
            lock = (part[0], part[1:1 + half], part[1 + half:])
        retry_s = retry_ms / 1000
        return Slot(slot=n, wire_fn=wire_fn, pins=(swdio, swclk), name=name, attach=_name(ATTACH, attach),
                    retry_s=int(retry_s) if retry_s == int(retry_s) else retry_s, mechanism=_name(MECHANISM, mech),
                    lock=lock, max_speed=max_speed, idle_clock=_name(IDLE_CLOCK, idle))
    if tag == ITEM["bind"] and len(v) >= 4:
        port, mode, selected, n = struct.unpack_from("<BBBB", v)
        streams, at = [], 4
        for _ in range(n):                                         # len, kind, id: a longer one's tail skipped
            if at >= len(v) or v[at] < 3 or at + 1 + v[at] > len(v):
                return (tag, v)
            streams.append((_name(STREAM, v[at + 1]), struct.unpack_from("<H", v, at + 2)[0]))
            at += 1 + v[at]
        return Bind(port=port, mode=_name(MODE, mode), streams=streams, selected=selected)
    if tag == ITEM["uart"] and len(v) >= 7:
        fn, baud, fmt = struct.unpack_from("<HIB", v)
        return Uart(fn=fn, baud=baud, format=fmt)
    return (tag, v)


def _sort_key(tag: int, value: bytes) -> tuple:
    """The canonical order's key of one item (probe.config §2): tag, then the key - plan (fn, role, channel), label /
    idle channel, slot, port, uart fn."""
    if tag == ITEM["plan"] and len(value) >= 5:
        return (tag,) + struct.unpack_from("<HBH", value)
    if tag in (ITEM["slot"], ITEM["bind"]):
        return (tag, value[0])
    return (tag, struct.unpack_from("<H", value)[0] if len(value) >= 2 else -1)


def canonical(items) -> bytes:
    """The canonical form of a whole configuration (probe.config §2): the items (objects of the classes above or
    (tag, value) pairs) in tag order, then key order, each as the one TLV encoding. What the probe hashes."""
    pairs = [(it[0] & 0x7F, bytes(it[1])) if isinstance(it, tuple) else (it.TAG, it.value()) for it in items]
    return b"".join(m.tlv(tag, value) for tag, value in sorted(pairs, key=lambda p: _sort_key(*p)))


def hash_of(items) -> int:
    """The hash the probe answers for this configuration (CRC-32 of the canonical form)."""
    return zlib.crc32(canonical(items))


@dataclass
class SlotState:
    slot: int
    state: str                     # connected, absent, lock-mismatch, no-target-id
    connection: int
    last_try_at_ns: int | None     # the probe's clock when it last tried an automatic attach (None: never tried)
    target_id: bytes | None


@dataclass
class BindState:
    port: int
    mode: str
    selected: int | None           # None in mixed
    flow: str                      # idle, streaming, held


@dataclass
class Declared:
    """What the probe's describe declares (fixed for one boot): the storage's size, the item tags it takes, how many
    slots, the bind modes."""
    storage_bytes: int = 0
    items: list[int] = field(default_factory=list)
    slots_max: int = 0
    bind_modes: list[str] = field(default_factory=list)


@dataclass
class State:
    """The live state (op state, lock-free; probe.config §3.3): the saved settings and the slots and binds."""
    storage: str = "none"          # none, applied, unreadable
    saved_hash: int = 0            # the saved settings' hash, as applied to this boot's fns (0: none / unreadable)
    unreadable: str | None = None  # why, when unreadable
    slots: list[SlotState] = field(default_factory=list)
    binds: list[BindState] = field(default_factory=list)


class ProbeConfig(Interface):
    NAME = "oep.probe.config"
    REVISION = 1
    GET, SET, SAVE, ERASE, UNSET, STATE = (_CFG.op[k] for k in ("get", "set", "save", "erase", "unset", "state"))

    def get(self) -> tuple[int, list[tuple[int, bytes]]]:
        """-> (hash, the items as (tag, value) in the canonical order), paged. No lock. Every page carries the same
        hash; when it changes between pages the probe's settings moved and the read starts over."""
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
        """The current settings, decoded (Plan, Label, Idle, Slot, Bind, Uart)."""
        return [decode(t, v) for t, v in self.get()[1]]

    def set(self, items: list) -> int:
        """Items (objects of the classes above, or item TLV bytes) -> the new hash. Removals (`remove()`) in the list go
        as an unset after the set (two requests; each is atomic by itself). Needs the lock."""
        removals = [it for it in items if isinstance(it, Removal)]
        rest = [it for it in items if not isinstance(it, Removal)]
        h = None
        if rest or not removals:
            body = b"".join(it if isinstance(it, (bytes, bytearray)) else item(it) for it in rest)
            h = self._hash_answer(self._call(self.SET, body))
        if removals:
            h = self.unset([(r.kind, r.key) for r in removals])
        return h

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
        """Save the current settings (a probe with storage; the whole is replaced). -> the hash saved."""
        return self._hash_answer(self._call(self.SAVE))

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
            elif tag == DESCRIBE["bind_modes"] and len(v) >= 4:
                bits = struct.unpack_from("<I", v)[0]
                d.bind_modes = [name for name, bit in MODE.items() if bits >> bit & 1]
        return d

    def state(self) -> State:
        """The storage's state and the live slot_state / bind_state (op state, lock-free, paged by first_slot /
        first_bind)."""
        st = State()
        first_slot = first_bind = 0
        while True:
            rd = m.Reader(self._call(self.STATE, bytes([first_slot, first_bind]), locked=False).payload)
            more, storage, st.saved_hash, why = rd.take("BBIB")
            st.storage = STORAGE_STATE.get(storage, str(storage))
            st.unreadable = UNREADABLE.get(why, str(why)) if why else None
            n_slots = rd.u8()
            for _ in range(n_slots):
                e = rd.element()
                n, state, conn, tried, _scheme, tlen = e.take("BBHQBB")
                st.slots.append(SlotState(n, SLOT_STATE.get(state, str(state)), conn,
                                          None if tried == NEVER_NS else tried, bytes(e.bytes(tlen)) or None))
            n_binds = rd.u8()
            for _ in range(n_binds):
                port, mode, sel, flow = rd.element().take("BBBB")
                st.binds.append(BindState(port, _name(MODE, mode), None if sel == 0xFF else sel,
                                          BIND_FLOW.get(flow, str(flow))))
            rd.tail()
            if not more or not (n_slots or n_binds):
                return st
            first_slot += n_slots
            first_bind += n_binds
