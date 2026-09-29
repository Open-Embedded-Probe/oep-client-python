"""oep.probe.config revision 1 (oep-spec docs/oep-if-probe-config.ja.md): the probe's settings - plan, labels, idle
pins, slots, binds - read and set as items, saved when the host says so, and the live slot / bind state.

    cfg = config.ProbeConfig(hst)
    cfg.set([config.Slot(0, wire_fn, (2, 54), "x035", attach="at-boot", retry_s=1, mechanism="dmseq"),
             config.Bind(1, "last-reset", [("slot", 0)])])
    cfg.save()
    cfg.items(), cfg.state()

An item goes as its TLV; one with only its key removes the item of that key (`config.remove`). A set replaces the keys it
carries and keeps the others; the probe checks the whole and changes nothing on a refusal.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import catalog, core, message as m, registry as reg
from .core import Interface

_CFG = reg.PROBE_CONFIG
ITEM = _CFG.tlv["item"]
DESCRIBE = _CFG.tlv["describe"]
ATTACH = {k.replace("_", "-"): v for k, v in _CFG.enum["slot_attach"].items()}
MODE = {k.replace("_", "-"): v for k, v in _CFG.enum["bind_mode"].items()}
STREAM = {"slot": _CFG.enum["bind_stream"]["slot_console"], "uart": _CFG.enum["bind_stream"]["fixture_uart"]}
MECHANISM = {k: v for k, v in reg.TARGET_CONSOLE.enum["mechanism"].items()}
SLOT_STATE = {v: k.replace("_", "-") for k, v in _CFG.enum["slot_state"].items()}
BIND_FLOW = {v: k for k, v in _CFG.enum["bind_flow"].items()}
IDLE = {k.replace("_", "-"): v for k, v in _CFG.enum["idle_mode"].items()}
STORAGE_STATE = {v: k for k, v in _CFG.enum["storage_state"].items()}


def _name(table: dict[str, int], value: int) -> str:
    return next((k for k, v in table.items() if v == value), str(value))


@dataclass
class Plan:
    fn: int
    role: int
    channel: int
    TAG = ITEM["plan"]

    def value(self) -> bytes:
        return struct.pack("<HBH", self.fn, self.role, self.channel)


@dataclass
class Label:
    channel: int
    text: str
    TAG = ITEM["label"]

    def value(self) -> bytes:
        return struct.pack("<H", self.channel) + self.text.encode()


@dataclass
class Idle:
    channel: int
    mode: str = "pull-up"          # hi-z, pull-up, pull-down
    TAG = ITEM["idle"]

    def value(self) -> bytes:
        return struct.pack("<HB", self.channel, IDLE[self.mode])


@dataclass
class Slot:
    """A place a target is wired to (probe.config §1.1). pins: (swdio, swclk), swclk 0xFFFF on one wire (swio).
    lock: (scheme, mask, value) - the target_id a connection must show, e.g. (1, mask u32 LE, value u32 LE)."""
    slot: int
    wire_fn: int
    pins: tuple[int, int]
    name: str
    attach: str = "host"           # host, at-boot
    retry_s: int = 0               # at-boot: try again every retry_s while the target is not there (0: never)
    mechanism: str = "dmseq"       # sdi, dmdata, dmseq
    lock: tuple[int, bytes, bytes] | None = None
    TAG = ITEM["slot"]

    def value(self) -> bytes:
        name = self.name.encode()
        v = struct.pack("<BHHHBHBB", self.slot, self.wire_fn, *self.pins, ATTACH[self.attach],
                        self.retry_s if self.attach == "at-boot" else 0, MECHANISM[self.mechanism], len(name)) + name
        if self.lock is None:
            return v + b"\x00"
        scheme, mask, value = self.lock
        if len(mask) != len(value) or not mask:
            raise ValueError("a lock's mask and value have the same length, at least 1 byte")
        return v + bytes([scheme]) + mask + value


@dataclass
class Bind:
    """What serial port `port` (the describe transport index) carries (probe.config §1.2). streams: ("slot", n) or
    ("uart", fn); selected: manual's choice (an index into streams)."""
    port: int
    mode: str = "last-reset"       # last-reset, manual, mixed
    streams: list[tuple[str, int]] = field(default_factory=list)
    selected: int = 0
    TAG = ITEM["bind"]

    def value(self) -> bytes:
        v = struct.pack("<BBBB", self.port, MODE[self.mode], self.selected if self.mode == "manual" else 0,
                        len(self.streams))
        return v + b"".join(struct.pack("<BH", STREAM[kind], i) for kind, i in self.streams)


def item(it) -> bytes:
    return m.tlv(it.TAG, it.value())


def remove(kind: str, key: int) -> bytes:
    """The item that removes the item of this key: kind plan (key fn), label / idle (channel), slot, bind (port)."""
    tag = ITEM[kind]
    return m.tlv(tag, bytes([key]) if kind in ("slot", "bind") else struct.pack("<H", key))


def decode(tag: int, v: bytes):
    """One item as one of the classes above (an unknown tag: (tag, value))."""
    if tag == ITEM["plan"] and len(v) == 5:
        return Plan(*struct.unpack("<HBH", v))
    if tag == ITEM["label"] and len(v) >= 2:
        return Label(struct.unpack_from("<H", v)[0], v[2:].decode("utf-8", "replace"))
    if tag == ITEM["idle"] and len(v) == 3:
        return Idle(struct.unpack_from("<H", v)[0], _name(IDLE, v[2]))
    if tag == ITEM["slot"] and len(v) >= 13:
        n, wire_fn, swdio, swclk, attach, retry_s, mech, name_len = struct.unpack_from("<BHHHBHBB", v)
        name = v[12:12 + name_len].decode("ascii", "replace")
        rest = v[12 + name_len:]
        lock = None
        if rest and rest[0]:
            half = (len(rest) - 1) // 2
            lock = (rest[0], rest[1:1 + half], rest[1 + half:])
        return Slot(n, wire_fn, (swdio, swclk), name, _name(ATTACH, attach), retry_s, _name(MECHANISM, mech), lock)
    if tag == ITEM["bind"] and len(v) >= 4:
        port, mode, selected, n = struct.unpack_from("<BBBB", v)
        streams = [(_name(STREAM, v[4 + 3 * k]), struct.unpack_from("<H", v, 5 + 3 * k)[0]) for k in range(n)]
        return Bind(port, _name(MODE, mode), streams, selected)
    return (tag, v)


@dataclass
class SlotState:
    slot: int
    state: str                     # connected, absent, lock-mismatch, no-target-id
    connection: int
    last_try_ms: int | None        # since the last automatic attach (None: never tried)
    target_id: bytes | None


@dataclass
class BindState:
    port: int
    mode: str
    selected: int | None           # None in mixed
    flow: str                      # idle, streaming, held


@dataclass
class State:
    storage_bytes: int = 0
    storage: str = "none"          # none, applied, unreadable
    saved_hash: int = 0
    items: list[int] = field(default_factory=list)
    slots_max: int = 0
    bind_modes: list[str] = field(default_factory=list)
    slots: list[SlotState] = field(default_factory=list)
    binds: list[BindState] = field(default_factory=list)


class ProbeConfig(Interface):
    NAME = "oep.probe.config"
    REVISION = 1
    GET, SET, SAVE, ERASE = (_CFG.op[k] for k in ("get", "set", "save", "erase"))

    def get(self) -> tuple[int, list[tuple[int, bytes]]]:
        """-> (hash, the items as (tag, value) in the canonical order), paged. No lock."""
        out, h = [], 0
        while True:
            p = self._call(self.GET, struct.pack("<H", len(out)), locked=False).payload
            more, h = struct.unpack_from("<BI", p)
            page = catalog.split_tlv(p[5:])
            out += page
            if not more or not page:
                return h, out

    def items(self) -> list:
        """The current settings, decoded (Plan, Label, Idle, Slot, Bind)."""
        return [decode(t, v) for t, v in self.get()[1]]

    def set(self, items: list) -> int:
        """Items (objects of the classes above, or item TLV bytes such as remove()) -> the new hash. Needs the lock."""
        body = b"".join(it if isinstance(it, (bytes, bytearray)) else item(it) for it in items)
        return struct.unpack("<I", self._call(self.SET, body).payload[:4])[0]

    def save(self) -> int:
        return struct.unpack("<I", self._call(self.SAVE).payload[:4])[0]

    def erase(self) -> None:
        self._call(self.ERASE)

    def state(self) -> State:
        """storage, the items taken, slots_max, bind modes, and the live slot_state / bind_state (lock-free)."""
        st = State()
        for tag, v in core.describe(self.host, self.fn):
            tag &= 0x7F
            if tag == DESCRIBE["storage"] and len(v) >= 9:
                st.storage_bytes, state, st.saved_hash = struct.unpack_from("<IBI", v)
                st.storage = STORAGE_STATE.get(state, str(state))
            elif tag == DESCRIBE["items"]:
                st.items = list(v)
            elif tag == DESCRIBE["slots_max"] and v:
                st.slots_max = v[0]
            elif tag == DESCRIBE["bind_modes"] and v:
                st.bind_modes = [name for name, bit in MODE.items() if v[0] >> bit & 1]
            elif tag == DESCRIBE["slot_state"] and len(v) >= 10:
                n, state, conn, age, _scheme, tlen = struct.unpack_from("<BBHIBB", v)
                st.slots.append(SlotState(n, SLOT_STATE.get(state, str(state)), conn,
                                          None if age == 0xFFFFFFFF else age, bytes(v[10:10 + tlen]) or None))
            elif tag == DESCRIBE["bind_state"] and len(v) >= 4:
                port, mode, sel, flow = v[:4]
                st.binds.append(BindState(port, _name(MODE, mode), None if sel == 0xFF else sel,
                                          BIND_FLOW.get(flow, str(flow))))
        return st
