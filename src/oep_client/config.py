"""oep.probe.config revision 1 (oep-spec interfaces/oep-if-probe-config.ja.md): the probe's channel settings - labels,
idle states and disabled channels - read and set as items, removed with unset, saved when the host says so, and the
storage state as its own lock-free operation (describe is declarations only, core §7.3).

    cfg = config.ProbeConfig(hst)
    cfg.set([config.Label(channel=23, text="nrst"), config.Idle(channel=20, mode="pull-up")])
    if cfg.needs_save():
        cfg.save()
    cfg.items(), cfg.describe(), cfg.state()
    cfg.unset([("idle", 20)])          # or cfg.set([config.remove("idle", 20)])

An item is tag(u8) len(u16) value, its tag sent with bit 7 clear (probe.config §1: items are settings data, not the
request's TLV tail). A set replaces the (tag, channel) keys it carries and keeps the others; the probe checks the whole
and changes nothing on a refusal. The generation is the probe's change counter within one boot (probe.config §2): a host
compares the items themselves (`same_items`, host guide §15) and the generation only for equality (get's pages, state's
saved_generation against get's). Wiring, UART and network settings are the host's, not items here.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import catalog, core, message as m, registry as reg
from .core import Interface

_CFG = reg.PROBE_CONFIG
ITEM = _CFG.tlv["item"]
DESCRIBE = _CFG.tlv["describe"]
IDLE = {k.replace("_", "-"): v for k, v in _CFG.enum["idle_mode"].items()}
STORAGE_STATE = {v: k for k, v in _CFG.enum["storage_state"].items()}
UNREADABLE = {v: k for k, v in _CFG.enum["storage_unreadable"].items()}
LABEL_MAX = reg.LIMITS["label_max_bytes"]                       # a label's text: 1 to 32 bytes (probe.config §1)


def _name(table: dict[str, int], value: int) -> str:
    return next((k for k, v in table.items() if v == value), str(value))


@dataclass(kw_only=True)
class Label:
    """A name the settings give a channel (probe.config §1): the probe never moves a line by it; a host may find its
    lines by the label convention (`find_line`, host guide §5.3)."""
    channel: int
    text: str
    TAG = ITEM["label"]

    def key(self) -> tuple:
        return (self.TAG, self.channel)

    def value(self) -> bytes:
        raw = self.text.encode()
        if not 1 <= len(raw) <= LABEL_MAX or not m.valid_text(raw):
            # probe.config §1: 1 to 32 bytes (the probe refuses other lengths); this host sends no control characters
            raise ValueError(f"label {self.text!r}: 1 to {LABEL_MAX} bytes of text without control characters")
        return struct.pack("<H", self.channel) + raw


@dataclass(kw_only=True)
class Idle:
    """The state of a free channel (probe.config §1, core §8): at boot and after every release. mode: hi-z, pull-up,
    pull-down, output-low, output-high (output_low / output_high too). An output mode keeps driving that level while the
    channel is free - a target's power switch kept on - even after the host has gone; a probe that cannot drive the
    channel refuses it unsupported. The output strength is the probe's own."""
    channel: int
    mode: str = "pull-up"          # hi-z, pull-up, pull-down, output-low, output-high
    TAG = ITEM["idle"]

    def __post_init__(self):
        self.mode = self.mode.replace("_", "-")

    def key(self) -> tuple:
        return (self.TAG, self.channel)

    def value(self) -> bytes:
        """channel(u16) mode(u8): 3 bytes (probe.config §1)."""
        if self.mode not in IDLE:
            raise ValueError(f"idle mode {self.mode!r}: one of {', '.join(IDLE)}")
        return struct.pack("<HB", self.channel, IDLE[self.mode])


@dataclass(kw_only=True)
class Disable:
    """A channel the probe never drives or sets up (probe.config §1, item 0x07): not on this board, or wired to another
    part. Any request naming it is refused unavailable (cause 5, held by settings); describe still declares it."""
    channel: int
    TAG = ITEM["disable"]

    def key(self) -> tuple:
        return (self.TAG, self.channel)

    def value(self) -> bytes:
        return struct.pack("<H", self.channel)


KINDS = {"label": Label, "idle": Idle, "disable": Disable}


@dataclass(frozen=True)
class Removal:
    """What `remove()` makes: one key for unset (op unset). `set()` sends these as an unset after its set."""
    kind: str
    channel: int

    def encoded(self) -> bytes:
        """tag(u8) channel(u16) (probe.config §2)."""
        return struct.pack("<BH", ITEM[self.kind], self.channel)


def item(it) -> bytes:
    """One item: tag (bit 7 clear) len(u16) value (probe.config §1)."""
    return m.tlv(it.TAG, it.value())


def remove(kind: str, channel: int) -> Removal:
    """The removal of the item of this key (for `unset`, or in a `set` list): kind label / idle / disable."""
    if kind not in ITEM:
        raise ValueError(f"no item kind {kind!r}")
    return Removal(kind, channel)


def decode(tag: int, v: bytes):
    """One item as one of the classes above (an unknown tag: (tag, value))."""
    tag &= 0x7F
    if tag == ITEM["label"] and len(v) >= 2:
        return Label(channel=struct.unpack_from("<H", v)[0], text=v[2:].decode("utf-8", "replace"))
    if tag == ITEM["idle"] and len(v) >= 3:
        return Idle(channel=struct.unpack_from("<H", v)[0], mode=_name(IDLE, v[2]))
    if tag == ITEM["disable"] and len(v) >= 2:
        return Disable(channel=struct.unpack_from("<H", v)[0])
    return (tag, v)


def split_items(data: bytes, count: int) -> tuple[list[tuple[int, bytes]], bytes]:
    """`count` items from `data` -> (the (tag, value) pairs, what follows them: the answer's TLV tail)."""
    out, at = [], 0
    for _ in range(count):
        if at + 3 > len(data):
            raise m.ShortPayload(f"item {len(out)} of {count}: truncated")
        tag, n = struct.unpack_from("<BH", data, at)
        if at + 3 + n > len(data):
            raise m.ShortPayload(f"item 0x{tag:02x}: truncated value")
        out.append((tag & 0x7F, bytes(data[at + 3:at + 3 + n])))
        at += 3 + n
    return out, data[at:]


def _pairs(items) -> list[tuple[int, bytes]]:
    """Items (objects of the classes above, or (tag, value) pairs) as (tag without bit 7, value), in get's order: tag,
    then channel (probe.config §2)."""
    pairs = [(it[0] & 0x7F, bytes(it[1])) if isinstance(it, tuple) else (it.TAG, it.value()) for it in items]
    return sorted(pairs, key=lambda p: (p[0], struct.unpack_from("<H", p[1])[0] if len(p[1]) >= 2 else -1))


_KIND = {v: k for k, v in ITEM.items()}


def _key_of(tag: int, value: bytes) -> tuple[str, int]:
    """The (kind, channel) an unset names for an item."""
    return _KIND.get(tag, str(tag)), struct.unpack_from("<H", value)[0]


def same_items(a, b) -> bool:
    """Whether two configurations hold the same items, item by item (host guide §15: a host compares what it wants with
    get's items; the generation is a counter, never computed here)."""
    return _pairs(a) == _pairs(b)


@dataclass
class Declared:
    """What the probe's describe declares (fixed for one boot): the storage's size (0: no save / erase) and the item
    tags it takes."""
    storage_bytes: int = 0
    items: list[int] = field(default_factory=list)


@dataclass
class State:
    """The storage's state (op state, lock-free; probe.config §3): none, applied, unreadable or partial; the generation
    the saved settings were saved or applied at (0: none, unreadable or partial); why it is not applied whole."""
    storage: str = "none"          # none, applied, unreadable, partial
    saved_generation: int = 0
    reason: str | None = None      # form, item, capacity (None: none)


class ProbeConfig(Interface):
    NAME = "oep.probe.config"
    REVISION = 1
    GET, SET, SAVE, ERASE, UNSET, STATE = (_CFG.op[k] for k in ("get", "set", "save", "erase", "unset", "state"))

    def get(self) -> tuple[int, list[tuple[int, bytes]]]:
        """-> (generation, the items as (tag, value) in get's order: tag, then channel), paged. No lock. Every page
        carries the generation; when it changes between pages the settings moved and the read starts over."""
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
                out += page
                if not more or not page:
                    return gen, out

    def items(self) -> list:
        """The current settings, decoded (Label, Idle, Disable)."""
        return [decode(t, v) for t, v in self.get()[1]]

    def set(self, items: list) -> int:
        """Items (objects of the classes above, or item bytes) -> the new generation. Removals (`remove()`) in the list
        go as an unset after the set (two requests; each is atomic by itself). Needs the lock."""
        removals = [it for it in items if isinstance(it, Removal)]
        rest = [it for it in items if not isinstance(it, Removal)]
        gen = None
        if rest or not removals:
            body = b"".join(it if isinstance(it, (bytes, bytearray)) else item(it) for it in rest)
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
        """Remove the items of these (kind, channel) (probe.config §2 unset): a key that is not there is nothing; the
        whole must still be consistent or nothing changes. -> the new generation. Needs the lock."""
        body = bytes([len(keys)]) + b"".join(Removal(kind, ch).encoded() for kind, ch in keys)
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

    def apply(self, wanted: list, save: bool = False) -> bool:
        """Make the probe's settings `wanted` (host guide §15): get, compared item by item (`same_items`); when they
        differ, set what is wanted and unset the keys get has and `wanted` does not; with `save`, save when
        `needs_save`. -> whether anything was sent. Needs the lock when something changes."""
        _, have = self.get()
        changed = False
        if not same_items(have, wanted):
            want = _pairs(wanted)
            keys = {_key_of(t, v) for t, v in want}
            gone = sorted({_key_of(t, v) for t, v in have} - keys)
            if gone:                                               # first, so a disable can replace an idle (§1)
                self.unset([(kind, ch) for kind, ch in gone])
            self.set([m.tlv(t, v) for t, v in want])
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
        # the first copy of each, bit 7 an unknown tag in an answer (core §2.2, §2.3); storage is a u32 exactly
        storage = catalog.first_fixed(tlvs, DESCRIBE["storage"], 4)
        if storage is not None:
            d.storage_bytes = struct.unpack("<I", storage)[0]
        items = catalog.first_value(tlvs, DESCRIBE["items"])
        if items is not None:
            d.items = list(items)
        return d

    def state(self) -> State:
        """The storage's state (op state, lock-free; probe.config §3): storage_state, saved_generation, reason."""
        rd = m.Reader(self._call(self.STATE, locked=False).payload)
        storage, saved, why = rd.take("BIB")
        rd.tail()
        return State(STORAGE_STATE.get(storage, str(storage)), saved, UNREADABLE.get(why, str(why)) if why else None)


# The line names of the label convention (host guide §5.3): `nrst` a target's reset, `power_hi` high powers it,
# `power_lo` low powers it. With several targets, `<profile>.<name>` (the host's connection profile name).
LINE_NAMES = tuple(_CFG.line_names)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold_name(text: str) -> str:
    """ASCII case folded (only A-Z: the convention compares ignoring ASCII case)."""
    return text.translate(_ASCII_LOWER)


def line_from_labels(labels, name: str, profile: str | None = None, firmware=(), single: bool = True) -> int | None:
    """Host guide §5.3 on bare data: labels as (channel, text) - the settings' label items - firmware the firmware's
    fixed labels (fn 0 describe) as (channel, text). In order, stopping at the first step that finds exactly one
    channel: (a) with a profile name, a settings label equal to `<profile>.<name>`; (b) only for a single target
    (`single`), a settings label equal to `name`; (c) the same, a firmware label equal to `name`. A step that finds two
    or more ends the search with none (ambiguous). Texts compare ignoring ASCII case."""
    steps = ([(labels, f"{profile}.{name}")] if profile is not None else []) + (
        [(labels, name), (firmware, name)] if single else [])
    for source, text in steps:
        found = {ch for ch, t in source if fold_name(t) == fold_name(text)}
        if len(found) > 1:
            return None                                            # ambiguous at this step: no such line
        if found:
            return found.pop()
    return None


def find_line(config, name: str, profile: str | None = None, firmware=None, single: bool = True) -> int | None:
    """The channel of the line `name` (nrst, power_hi, power_lo: LINE_NAMES) by the label convention (host guide §5.3),
    or None. config: a Host (its settings are read with `ProbeConfig.items()`, no lock) or the decoded items. profile:
    the host's connection profile name when the probe serves several targets (`single` False); firmware: the fixed
    labels as (channel, text) - read from the probe's describe when `config` is a Host, none when it is a list of items
    and this is not given."""
    if isinstance(config, (list, tuple)):
        items, fixed = config, list(firmware or ())
    else:
        items = ProbeConfig(config).items()
        fixed = list(firmware) if firmware is not None else core.firmware_labels(config)
    labels = [(it.channel, it.text) for it in items if isinstance(it, Label)]
    return line_from_labels(labels, name, profile, fixed, single)
