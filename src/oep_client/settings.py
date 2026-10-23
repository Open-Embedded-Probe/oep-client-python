"""The probe's settings (oep-spec oep-core §13) and their file (docs/oep-settings-file.ja.md).

The core gathers the settings; each item belongs to an owner - the core (fn 0) or an interface (its fn) - that
declares it in its describe (setting, setting_choice). fn 0's ops read and write them: get / set / unset (save / state
with storage, network with the network items). A value is (fn, item number, key) on the wire for one boot; its names
- the category (``core`` or ``name/instance``), the flat name (the item's name with the key's name in place of ``{}``)
and the value with references as names - are what a host keeps, shows and writes to a file.

    st = settings.Settings(hst)
    st.show()                                          # [(category, flat name, value)]
    st.set_named("core", {"channel.GPIO14.label": "NRST", "channel.GPIO14.idle": "pull-up"})
    st.save()
    doc = st.export()                                   # the settings file (JSON), every value
    plan = st.plan_import(doc); st.apply_import(plan)

Secrets (core §13.5) are never read: get gives no data, the file a ``{"secret": true}``.
"""

from __future__ import annotations

import ipaddress
import re
import struct
from dataclasses import dataclass, field

from . import catalog, core, message as m, registry as reg

_C = reg.CORE
OP = {k: _C.op[k] for k in ("get", "set", "unset", "save", "state", "network")}
FORM = {v: k for k, v in _C.enum["setting_form"].items()}
KEY_KIND = {v: k for k, v in _C.enum["setting_key_kind"].items()}
SECRET = _C.enum["setting_flag"]["secret"]
STORAGE_STATE = {v: k for k, v in _C.enum["storage_state"].items()}
STORAGE_REASON = {v: k for k, v in _C.enum["storage_reason"].items()}
NET_STATE = {v: k for k, v in _C.enum["network_state"].items()}
NET_REASON = {v: k for k, v in _C.enum["network_reason"].items()}
ROUTE = {v: k for k, v in _C.enum["route_kind"].items()}          # a transport kind's short name (core §13.3)
_D = _C.tlv["describe"]
SETTING, SETTING_CHOICE = reg.DESCRIBE_COMMON["setting"], reg.DESCRIBE_COMMON["setting_choice"]
VALUE_HEADER = 7                                                   # fn number key len, get's and set's alike (core §13.5)
FILE_VERSION = 1                                                   # the settings file's form (oep_settings)
CORE_CATEGORY = "core"


@dataclass
class Item:
    """One item as its owner declares it (core §13.2)."""
    fn: int
    number: int
    name: str
    form: str
    key_kind: str
    keys: int
    max_len: int
    flags: int
    choices: dict[int, str] = field(default_factory=dict)

    @property
    def secret(self) -> bool:
        return bool(self.flags & SECRET)


@dataclass
class Value:
    """One value as get gives it (no origin: core §13.4)."""
    fn: int
    number: int
    key: int
    data: bytes


@dataclass
class State:
    storage: str
    saved_generation: int
    reason: str | None


@dataclass
class NetifState:
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


@dataclass
class ImportPlan:
    """What an import does (settings file §4): the values to set and unset (wire triples), the secrets the probe has
    no value for, and the members that do not resolve on this probe."""
    sets: list[tuple[int, int, int, bytes]] = field(default_factory=list)
    unsets: list[tuple[int, int, int]] = field(default_factory=list)
    secret_without_value: list[tuple[str, str]] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    other_probe: bool = False


class Catalog:
    """The names of one probe for one boot (core §13.3): each fn's category and items, the channels', routes' and
    netifs' names. Built from the probe's describes, or from TLVs (a file's check without a probe)."""

    def __init__(self, fn0: list[tuple[int, bytes]], interfaces: list[tuple[int, str, int, list[tuple[int, bytes]]]]):
        self.category = {0: CORE_CATEGORY}
        self.items: dict[int, list[Item]] = {0: self._items(0, fn0)}
        for fn, name, instance, tlvs in interfaces:
            self.category[fn] = f"{name}/{instance}"
            self.items[fn] = self._items(fn, tlvs)
        self.channel = {struct.unpack_from("<H", v)[0]: v[2:].decode("utf-8", "replace")
                        for tag, v in fn0 if tag == _D["channel_name"] and len(v) >= 2}
        kinds = {}
        for tag, v in fn0:
            if tag == _D["transport"] and len(v) >= 2:
                kinds.setdefault(v[0], v[1])
        self.route, seen = {}, {}
        for index in sorted(kinds):
            short = ROUTE.get(kinds[index], f"kind{kinds[index]}")
            self.route[index] = f"{short}{seen.get(short, 0)}"
            seen[short] = seen.get(short, 0) + 1
        self.netif = {k: v.decode("utf-8", "replace") for k, v in
                      enumerate((v for tag, v in fn0 if tag == _D["netif"]), 1)}
        info = {tag: v for tag, v in reversed(fn0)}
        self.probe = {k: info[_D[k]].decode("utf-8", "replace") for k in ("model", "unit_id", "firmware") if _D[k] in info}

    @staticmethod
    def _items(fn: int, tlvs) -> list[Item]:
        out: list[Item] = []
        for tag, v in tlvs:
            if tag == SETTING and len(v) >= 6:
                form, kind, keys, max_len, flags = struct.unpack_from("<BBHBB", v)
                out.append(Item(fn, len(out) + 1, v[6:].decode("utf-8", "replace"), FORM.get(form, str(form)),
                                KEY_KIND.get(kind, str(kind)), keys, max_len, flags))
            elif tag == SETTING_CHOICE and len(v) >= 2 and 1 <= v[0] <= len(out):
                out[v[0] - 1].choices[v[1]] = v[2:].decode("utf-8", "replace")
        return out

    def item(self, fn: int, number: int) -> Item | None:
        items = self.items.get(fn, [])
        return items[number - 1] if 1 <= number <= len(items) else None

    def fn_of(self, category: str) -> int | None:
        return next((fn for fn, c in self.category.items() if c == category), None)

    # -- names (core §13.3) --
    def key_name(self, it: Item, key: int) -> str | None:
        kind = it.key_kind
        if kind == "none":
            return None
        if kind == "index":
            return str(key)
        table = {"channel": self.channel, "transport": self.route, "netif": self.netif}.get(kind, {})
        return table.get(key)

    def key_of(self, it: Item, name: str) -> int | None:
        if it.key_kind == "index":
            return int(name) if name.isdigit() and int(name) < it.keys else None
        table = {"channel": self.channel, "transport": self.route, "netif": self.netif}.get(it.key_kind, {})
        return next((k for k, n in table.items() if n == name), None)

    def flat(self, it: Item, key: int) -> str | None:
        if it.key_kind == "none":
            return it.name
        name = self.key_name(it, key)
        return None if name is None else it.name.replace("{}", name)

    def resolve(self, category: str, flat: str) -> tuple[Item, int] | None:
        """The item and key a flat name names (settings file §2.2): exactly one item, else None."""
        fn = self.fn_of(category)
        if fn is None:
            return None
        found = []
        for it in self.items.get(fn, []):
            if it.key_kind == "none":
                if flat == it.name:
                    found.append((it, 0))
                continue
            head, _, tail = it.name.partition("{}")
            if flat.startswith(head) and flat.endswith(tail) and len(flat) > len(head) + len(tail):
                key = self.key_of(it, flat[len(head):len(flat) - len(tail)])
                if key is not None:
                    found.append((it, key))
        return found[0] if len(found) == 1 else None

    def to_json(self, it: Item, data: bytes):
        """A value as the file writes it (settings file §2.3)."""
        f = it.form
        if f == "bytes":
            return data.hex()
        if f == "text":
            return data.decode("utf-8", "replace")
        if f == "u32":
            return struct.unpack("<I", data)[0]
        if f == "ipv4":
            return str(ipaddress.IPv4Address(data))
        if f == "bool":
            return data[0] != 0
        if f == "enum":
            return it.choices.get(data[0], data[0])
        if f == "channel":
            return self.channel.get(struct.unpack("<H", data)[0], struct.unpack("<H", data)[0])
        if f == "transport":
            return self.route.get(data[0], data[0])
        if f == "fn":
            return self.category.get(struct.unpack("<H", data)[0], struct.unpack("<H", data)[0])
        return data.hex()

    def from_json(self, it: Item, value) -> bytes:
        """The wire's data of a file value (ValueError: not this item's form, or a name this probe does not have)."""
        f = it.form
        rev = lambda table: {v: k for k, v in table.items()}
        try:
            if f == "bytes":
                return bytes.fromhex(value)
            if f == "text":
                if not isinstance(value, str):
                    raise ValueError
                return value.encode()
            if f == "u32":
                if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
                    raise ValueError
                return struct.pack("<I", value)
            if f == "ipv4":
                return ipaddress.IPv4Address(value).packed
            if f == "bool":
                if not isinstance(value, bool):
                    raise ValueError
                return bytes([1 if value else 0])
            if f == "enum":
                return bytes([rev(it.choices)[value]])
            if f == "channel":
                return struct.pack("<H", rev(self.channel)[value])
            if f == "transport":
                return bytes([rev(self.route)[value]])
            if f == "fn":
                return struct.pack("<H", rev(self.category)[value])
        except (KeyError, TypeError, ValueError, ipaddress.AddressValueError):
            pass
        raise ValueError(f"{value!r} is not a value of {it.name} ({f})")

    def text_value(self, it: Item, text: str):
        """A command line's text as the file's JSON value of this item."""
        if it.form == "u32":
            return int(text, 0)
        if it.form == "bool":
            if text.lower() not in ("true", "false", "1", "0", "on", "off"):
                raise ValueError(f"{text!r}: true or false")
            return text.lower() in ("true", "1", "on")
        return text


def values_from(payload_pages) -> list[Value]:
    out = []
    for raw in payload_pages:
        at = 0
        while at + VALUE_HEADER <= len(raw):
            fn, number, key, n = struct.unpack_from("<HBHH", raw, at)
            out.append(Value(fn, number, key, raw[at + VALUE_HEADER:at + VALUE_HEADER + n]))
            at += VALUE_HEADER + n
    return out


def split_values(data: bytes, count: int) -> tuple[list[Value], bytes]:
    """count values (fn number key len data) from get's answer, then the answer's own TLV tail."""
    out, at = [], 0
    for _ in range(count):
        if at + VALUE_HEADER > len(data):
            raise m.ProtocolError("get: a value past the answer")
        fn, number, key, n = struct.unpack_from("<HBHH", data, at)
        if at + VALUE_HEADER + n > len(data):
            raise m.ProtocolError("get: a value's data past the answer")
        out.append(Value(fn, number, key, data[at + VALUE_HEADER:at + VALUE_HEADER + n]))
        at += VALUE_HEADER + n
    return out, data[at:]


class Settings:
    """fn 0's settings ops on a host (core §13)."""

    def __init__(self, hst):
        self.host = hst

    # -- what the probe declares --
    def catalog(self) -> Catalog:
        """The names (describe of fn 0 and of every listed fn, cached by the host while its boot_id holds)."""
        listed = core.list_entries(self.host)
        return Catalog(core.describe(self.host, 0),
                       [(e.fn, e.name, e.instance, core.describe(self.host, e.fn)) for e in listed])

    def offers(self, op: str) -> bool:
        return core.offers(self.host, 0, OP[op])

    def _call(self, op: str, body: bytes = b"", *, locked: bool = True, expect_ms: int = 0) -> m.Result:
        core.require(self.host, 0, OP[op])
        if expect_ms and hasattr(self.host, "expecting"):
            with self.host.expecting(expect_ms):
                return self.host.call(0, OP[op], body, locked=locked)
        return self.host.call(0, OP[op], body, locked=locked)

    # -- the ops --
    def get(self) -> tuple[int, list[Value]]:
        """-> (generation, every value in get's order), paged. No lock. When the generation changes between pages the
        settings moved: the read starts over."""
        while True:
            out, first_gen = [], None
            while True:
                rd = m.Reader(self._call("get", struct.pack("<H", len(out)), locked=False).payload)
                more, gen, count = rd.take("BIB")
                page, tail = split_values(rd.rest(), count)
                m.Tail.parse(tail)                                 # the answer's own TLV tail, not values
                if first_gen is None:
                    first_gen = gen
                elif gen != first_gen:
                    break                                          # changed under us: again from the start
                out += page
                if not more or not page:
                    return gen, out

    @staticmethod
    def _generation(r: m.Result) -> int:
        rd = m.Reader(r.payload)
        gen = rd.u32()
        rd.tail()
        return gen

    def _batches(self, rows: list[bytes]) -> list[list[bytes]]:
        """Values in requests that each fit max_frame (a set or unset of more values goes as several, each atomic)."""
        limit = core.confirm(self.host)["max_frame"] - m.REQUEST_HEADER - 1
        out, cur, size = [], [], 0
        for r in rows:
            if cur and (size + len(r) > limit or len(cur) == 255):
                out.append(cur)
                cur, size = [], 0
            cur.append(r)
            size += len(r)
        if cur:
            out.append(cur)
        return out

    def set(self, values: list[tuple[int, int, int, bytes]]) -> int:
        """(fn, number, key, data) -> the new generation. Needs the lock."""
        gen = None
        rows = [struct.pack("<HBHH", fn, n, k, len(d)) + d for fn, n, k, d in values]
        for batch in self._batches(rows) or [[]]:
            gen = self._generation(self._call("set", bytes([len(batch)]) + b"".join(batch)))
        return gen

    def unset(self, keys: list[tuple[int, int, int]]) -> int:
        """(fn, number, key): each back to its default, or no value (core §13.4). -> the new generation."""
        gen = None
        rows = [struct.pack("<HBH", fn, n, k) for fn, n, k in keys]
        for batch in self._batches(rows) or [[]]:
            gen = self._generation(self._call("unset", bytes([len(batch)]) + b"".join(batch)))
        return gen

    def save(self) -> int:
        """Save the values (a probe with storage). -> the current generation (completed failed raises: `state`
        says what is kept). Its argument time is max_op_ms (core §13.7)."""
        return self._generation(self._call("save", expect_ms=core.max_op_ms(self.host)))

    def state(self) -> State:
        rd = m.Reader(self._call("state", locked=False).payload)
        storage, saved, why = rd.take("BIB")
        rd.tail()
        return State(STORAGE_STATE.get(storage, str(storage)), saved, STORAGE_REASON.get(why, str(why)) if why else None)

    def needs_save(self) -> bool:
        st = self.state()
        return not (st.storage == "applied" and st.saved_generation == self.get()[0])

    def network(self) -> Network:
        netifs = self.catalog().netif
        rd = m.Reader(self._call("network", locked=False).payload)
        port, count = rd.take("HB")
        out = []
        ip = lambda b: str(ipaddress.IPv4Address(b)) if any(b) else None
        for _ in range(count):
            netif, st, entry, reason, rssi = rd.take("BBHBb")
            addr, pfx, gw, dns = rd.bytes(4), rd.u8(), rd.bytes(4), rd.bytes(4)
            out.append(NetifState(netifs.get(netif, str(netif)), NET_STATE.get(st, str(st)),
                                  None if entry == 0xFFFF else entry, NET_REASON.get(reason, str(reason)), rssi,
                                  f"{ip(addr)}/{pfx}" if any(addr) else None, ip(gw), ip(dns)))
        rd.tail()
        return Network(port, out)

    # -- by name --
    def show(self) -> list[tuple[str, str, object]]:
        """Every value as (category, flat name, value); a secret's value is {"secret": True}."""
        cat = self.catalog()
        out = []
        for v in self.get()[1]:
            it = cat.item(v.fn, v.number)
            if it is None:
                continue
            flat = cat.flat(it, v.key) or f"{it.name}#{v.key}"
            out.append((cat.category.get(v.fn, str(v.fn)), flat,
                        {"secret": True} if it.secret else cat.to_json(it, v.data)))
        return out

    def _named(self, cat: Catalog, category: str, flat: str) -> tuple[Item, int]:
        got = cat.resolve(category, flat)
        if got is None:
            raise LookupError(f"{category}: no item {flat!r} on this probe")
        return got

    def set_named(self, category: str, values: dict) -> int:
        """Values by flat name (JSON values, or text from a command line) in one category. Needs the lock."""
        cat = self.catalog()
        rows = []
        for flat, value in values.items():
            it, key = self._named(cat, category, flat)
            if isinstance(value, str) and it.form not in ("text", "bytes", "enum", "channel", "transport", "fn", "ipv4"):
                value = cat.text_value(it, value)
            rows.append((it.fn, it.number, key, cat.from_json(it, value)))
        return self.set(rows)

    def unset_named(self, category: str, flats: list[str]) -> int:
        cat = self.catalog()
        keys = []
        for flat in flats:
            it, key = self._named(cat, category, flat)
            keys.append((it.fn, it.number, key))
        return self.unset(keys)

    def changeable(self) -> bool:
        """Whether the probe lets its values change: set / unset offered (a probe of initial values only has get alone,
        core §13.5)."""
        return self.offers("set") and self.offers("unset")

    def unset_all(self) -> int | None:
        """Every value away (then save to empty the storage: core §13.7). None: there was none."""
        keys = [(v.fn, v.number, v.key) for v in self.get()[1]]
        return self.unset(keys) if keys else None

    # -- the file (settings file §3, §4) --
    def export(self) -> dict:
        return export_file(self.catalog(), self.get()[1])

    def plan_import(self, doc: dict) -> ImportPlan:
        return plan_import(self.catalog(), self.get()[1], doc)

    def apply_import(self, plan: ImportPlan) -> int | None:
        """Unset first (a disable that replaces an idle), then set. Needs the lock."""
        gen = None
        if plan.unsets:
            gen = self.unset(plan.unsets)
        if plan.sets:
            gen = self.set(plan.sets)
        return gen


def export_file(cat: Catalog, values: list[Value]) -> dict:
    """The settings file of every value (settings file §3)."""
    out: dict = {}
    for v in values:
        it = cat.item(v.fn, v.number)
        flat = cat.flat(it, v.key) if it is not None else None
        if flat is None:
            continue
        out.setdefault(cat.category[v.fn], {})[flat] = {"secret": True} if it.secret else cat.to_json(it, v.data)
    return {"oep_settings": FILE_VERSION, "probe": dict(cat.probe), "settings": out}


def check_file(doc) -> None:
    """The file's form (settings file §2): ValueError when it is not one this host reads."""
    if not isinstance(doc, dict) or doc.get("oep_settings") != FILE_VERSION:
        raise ValueError(f"not a settings file of version {FILE_VERSION}")
    if not isinstance(doc.get("settings"), dict) or not all(isinstance(v, dict) for v in doc["settings"].values()):
        raise ValueError("settings: an object of categories, each an object of values")


def plan_import(cat: Catalog, current: list[Value], doc: dict) -> ImportPlan:
    """What making the file's values the probe's takes (settings file §4)."""
    check_file(doc)
    plan = ImportPlan()
    probe = doc.get("probe", {})
    plan.other_probe = any(probe.get(k) not in (None, cat.probe.get(k)) for k in ("unit_id", "model"))
    have = {(v.fn, v.number, v.key): v for v in current}
    wanted = set()
    for category, members in doc["settings"].items():
        for flat, value in members.items():
            got = cat.resolve(category, flat)
            if got is None:
                plan.unresolved.append((category, flat))
                continue
            it, key = got
            wanted.add((it.fn, it.number, key))
            if it.secret:
                if (it.fn, it.number, key) not in have:
                    plan.secret_without_value.append((category, flat))
                continue
            try:
                data = cat.from_json(it, value)
            except ValueError:
                plan.unresolved.append((category, flat))
                continue
            now = have.get((it.fn, it.number, key))
            if now is None or now.data != data:
                plan.sets.append((it.fn, it.number, key, data))
    plan.unsets = [(v.fn, v.number, v.key) for v in current if (v.fn, v.number, v.key) not in wanted]
    return plan


# ---- ports and lines by label (host guide §5.3) -----------------------------------------------------------
# A label "S.N" names port S's line N: S 1-16 of letters, digits and '-', a letter first; N letters, digits, '-' and '_';
# one '.' between. Other labels are a pin's names only. Kept as written, compared ignoring ASCII case; a probe keeps its
# labels unique that way (core §13.8). Line names: `swdio`, `swclk`, `swio`, `rx`, `tx`, `nrst`, `power_hi`, `power_lo`.
LINE_NAMES = tuple(_C.line_names)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
LABEL = "channel.{}.label"
_PORT = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,15}")
_LINE = re.compile(r"[A-Za-z0-9_-]+")


def fold_name(text: str) -> str:
    """ASCII case folded (only A-Z): names compare ignoring ASCII case."""
    return text.translate(_ASCII_LOWER)


def port_line(text: str) -> tuple[str, str] | None:
    """(S, N) of a label that names a port's line, as written; None for any other label."""
    port, dot, line = text.partition(".")
    if not dot or not _PORT.fullmatch(port) or not _LINE.fullmatch(line):
        return None
    return port, line


def ports_named(labels) -> list[str]:
    """The ports the labels name (host guide §5.3): each S once (ignoring case), as written, in order."""
    out: list[str] = []
    for _, text in labels:
        pl = port_line(text)
        if pl and fold_name(pl[0]) not in {fold_name(p) for p in out}:
            out.append(pl[0])
    return out


def line_from_labels(labels, name: str, port: str | None = None) -> int | None:
    """The channel of port `port`'s line `name` among labels as (channel, text), or None (host guide §5.3). Without a
    port, the only port the labels name (a tool's shortcut when exactly one is there). Compared ignoring ASCII case."""
    if port is None:
        ports = ports_named(labels)
        if len(ports) != 1:
            return None
        port = ports[0]
    found = {ch for ch, text in labels
             if (pl := port_line(text)) and fold_name(pl[0]) == fold_name(port) and fold_name(pl[1]) == fold_name(name)}
    return found.pop() if len(found) == 1 else None


def labels(hst) -> list[tuple[int, str]]:
    """Every channel label now (channel.{}.label) as (channel, text). A probe without the settings ops: none."""
    if not core.offers(hst, 0, OP["get"]):
        return []
    st = Settings(hst)
    cat = st.catalog()
    out = []
    for v in st.get()[1]:
        it = cat.item(v.fn, v.number)
        if it is not None and v.fn == 0 and it.name == LABEL:
            out.append((v.key, m.shown(v.data)))
    return out


def find_line(source, name: str, port: str | None = None) -> int | None:
    """The channel of the line `name` (LINE_NAMES) of port `port` by the labels (host guide §5.3), or None. source: a
    Host (its labels read with get, no lock) or labels as (channel, text)."""
    found = list(source) if isinstance(source, (list, tuple)) else labels(source)
    return line_from_labels(found, name, port)
