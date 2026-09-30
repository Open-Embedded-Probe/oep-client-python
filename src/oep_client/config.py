"""oep.probe.config revision 1 (oep-spec docs/oep-if-probe-config.ja.md): the probe's settings - plan, labels, idle
pins, slots, binds - read and set as items, saved when the host says so, and the live slot / bind state.

    cfg = config.ProbeConfig(hst)
    cfg.set([config.Slot(slot=0, wire_fn=wire_fn, pins=(2, 54), name="x035", attach="at-boot", retry_s=1),
             config.Bind(port=1, mode="last-reset", streams=[("slot", 0)])])
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
IDLE_CLOCK = dict(reg.WIRE_RVSWD.enum["idle_clock"])            # a slot's idle_clock (oep-if-debug §3)
STORAGE_STATE = {v: k for k, v in _CFG.enum["storage_state"].items()}
UNREADABLE = {1: "unreadable form", 2: "an interface it names is gone or of another revision", 3: "refused when applied"}


def _name(table: dict[str, int], value: int) -> str:
    return next((k for k, v in table.items() if v == value), str(value))


@dataclass(kw_only=True)
class Plan:
    fn: int
    role: int
    channel: int
    TAG = ITEM["plan"]

    def value(self) -> bytes:
        return struct.pack("<HBH", self.fn, self.role, self.channel)


@dataclass(kw_only=True)
class Label:
    channel: int
    text: str
    TAG = ITEM["label"]

    def value(self) -> bytes:
        return struct.pack("<H", self.channel) + self.text.encode()


@dataclass(kw_only=True)
class Idle:
    channel: int
    mode: str = "pull-up"          # hi-z, pull-up, pull-down
    TAG = ITEM["idle"]

    def value(self) -> bytes:
        return struct.pack("<HB", self.channel, IDLE[self.mode])


@dataclass(kw_only=True)
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
    max_speed: int = 0             # the line's ceiling in Hz for the probe's own attach (0: none) - the target's
    idle_clock: str = "high"       # rvswd: SWCLK while the line rests, high / low - the target's (oep-if-debug §3)
    TAG = ITEM["slot"]

    def value(self) -> bytes:
        name = self.name.encode()
        v = struct.pack("<BHHHBHIBBB", self.slot, self.wire_fn, *self.pins, ATTACH[self.attach],
                        self.retry_s if self.attach == "at-boot" else 0, self.max_speed, IDLE_CLOCK[self.idle_clock],
                        MECHANISM[self.mechanism], len(name)) + name
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

    def value(self) -> bytes:
        v = struct.pack("<BBBB", self.port, MODE[self.mode], self.selected if self.mode == "manual" else 0,
                        len(self.streams))
        return v + b"".join(struct.pack("<BBH", 3, STREAM[kind], i) for kind, i in self.streams)   # len, kind, id


def item(it) -> bytes:
    return m.tlv(it.TAG, it.value())


def remove(kind: str, key: int) -> bytes:
    """The item that removes the item of this key: kind plan (key fn), label / idle (channel), slot, bind (port)."""
    tag = ITEM[kind]
    return m.tlv(tag, bytes([key]) if kind in ("slot", "bind") else struct.pack("<H", key))


def decode(tag: int, v: bytes):
    """One item as one of the classes above (an unknown tag: (tag, value))."""
    if tag == ITEM["plan"] and len(v) == 5:
        fn, role, channel = struct.unpack("<HBH", v)
        return Plan(fn=fn, role=role, channel=channel)
    if tag == ITEM["label"] and len(v) >= 2:
        return Label(channel=struct.unpack_from("<H", v)[0], text=v[2:].decode("utf-8", "replace"))
    if tag == ITEM["idle"] and len(v) == 3:
        return Idle(channel=struct.unpack_from("<H", v)[0], mode=_name(IDLE, v[2]))
    if tag == ITEM["slot"] and len(v) >= 18:
        n, wire_fn, swdio, swclk, attach, retry_s, max_speed, idle, mech, name_len = struct.unpack_from("<BHHHBHIBBB", v)
        name = v[17:17 + name_len].decode("ascii", "replace")
        at = 17 + name_len
        lock_len = v[at] if at < len(v) else 0
        part = v[at + 1:at + 1 + lock_len]                         # after it: later fields (core §2.3), skipped
        lock = None
        if lock_len >= 3:
            half = (lock_len - 1) // 2
            lock = (part[0], part[1:1 + half], part[1 + half:])
        return Slot(slot=n, wire_fn=wire_fn, pins=(swdio, swclk), name=name, attach=_name(ATTACH, attach), retry_s=retry_s,
                    mechanism=_name(MECHANISM, mech), lock=lock, max_speed=max_speed, idle_clock=_name(IDLE_CLOCK, idle))
    if tag == ITEM["bind"] and len(v) >= 4:
        port, mode, selected, n = struct.unpack_from("<BBBB", v)
        streams, at = [], 4
        for _ in range(n):                                         # len, kind, id: a longer one's tail skipped
            if at >= len(v) or v[at] < 3 or at + 1 + v[at] > len(v):
                return (tag, v)
            streams.append((_name(STREAM, v[at + 1]), struct.unpack_from("<H", v, at + 2)[0]))
            at += 1 + v[at]
        return Bind(port=port, mode=_name(MODE, mode), streams=streams, selected=selected)
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
    unreadable: str | None = None  # why, when unreadable (probe.config §4)
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
                if len(v) >= 14 and v[13]:
                    st.unreadable = UNREADABLE.get(v[13], str(v[13]))
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
