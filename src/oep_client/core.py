"""The probe's core (fn 0) as the other clients need it: finding interfaces by name, confirm, the probe's own labels,
the pin plan - and `Interface`, the base every interface client shares (its fn, its revision checked against the list
entry, and calls that raise unless they worked)."""

from __future__ import annotations

import contextlib

import struct

from . import catalog, host as h, message as m, registry as reg

OP_PLAN_APPLY, OP_PLAN_RELEASE = m.OP_PLAN_APPLY, m.OP_PLAN_RELEASE
TAG_ROLE_ASSIGNMENT = reg.CORE.tlv["plan_apply"]["role_assignment"]     # already critical (0x90)
CORE_LABEL = reg.CORE.tlv["describe"]["label"]
CORE_TRANSPORT = reg.CORE.tlv["describe"]["transport"]
CORE_MAX_OP_MS = reg.CORE.tlv["describe"]["max_op_ms"]
TRANSPORT_KIND = reg.CORE.enum["transport_kind"]
SERIAL_KINDS = {TRANSPORT_KIND["uart_bridge"], TRANSPORT_KIND["usb_cdc"], TRANSPORT_KIND["usb_serial_jtag"]}


class UnsupportedRevision(h.OepError):
    """The probe offers the interface in a revision whose payload shapes this client does not speak (oep-core §2.7:
    a host never uses an interface revision it does not know)."""


def list_entries(hst: h.Host, name: str = "", exact: bool = False) -> list[catalog.ListEntry]:
    """Every list entry under `name` (exact: that name only), paged by first (u16). The fn -> revision of each is
    remembered on the host."""
    entries: list[catalog.ListEntry] = []
    while True:
        total, page = catalog.unpack_list_result(
            hst.request(m.CORE_FN, m.OP_LIST, catalog.pack_list_request(name, exact, len(entries)), locked=False).payload)
        entries += page
        if not page or len(entries) >= total:    # an empty page ends it too: no endless loop on a short answer
            break
    for e in entries:
        hst._revisions[e.fn] = e.revision
    return entries


def find_all(hst: h.Host, name: str) -> list[int]:
    """fns of every interface with exactly this name (instances of the same kind, e.g. two UARTs)."""
    return [e.fn for e in list_entries(hst, name, True)]


def find(hst: h.Host, name: str) -> int:
    """fn of the first interface with exactly this name. Cached on the host until the probe reboots (boot_id), so
    building a client per connection costs no list request."""
    fn = hst._fns.get(name)
    if fn is None:
        fns = find_all(hst, name)
        if not fns:
            raise LookupError(f"probe does not offer {name}")
        fn = hst._fns[name] = fns[0]
    return fn


def revision(hst: h.Host, name: str, fn: int) -> int:
    """The list entry's revision of interface `fn` (asked by name when not known yet)."""
    if fn not in hst._revisions:
        list_entries(hst, name, True)
    if fn not in hst._revisions:
        raise LookupError(f"probe lists no {name} at fn {fn}")
    return hst._revisions[fn]


def confirm(hst: h.Host) -> dict:
    """The probe's limits (asked once per host): revision, flags, max_frame, window (u32), max_inflight."""
    return hst.confirmed()


def describe(hst: h.Host, fn: int = 0) -> list[tuple[int, bytes]]:
    """Every describe TLV of `fn` (0: the probe itself), paged by first. Declarations only (core §7.3): cached on the
    host while the probe's boot_id stays the same."""
    cached = hst._describes.get(fn)
    if cached is not None:
        return list(cached)
    data, first = b"", 0
    while True:
        p = hst.request(m.CORE_FN, m.OP_DESCRIBE, catalog.pack_describe_request(fn, first), locked=False).payload
        more, chunk = m.Reader(p).u8(), p[1:]
        data += chunk
        first += len(catalog.split_tlv(chunk))
        if not more or not chunk:
            break
    out = catalog.split_tlv(data)
    hst._describes[fn] = out
    return list(out)


def probe_labels(hst: h.Host) -> dict[str, int]:
    """The firmware's fixed channel labels from oep.core's describe (tag 0x46): {"NRST": 23, ...}. Labels the
    settings gave are read from oep.probe.config (config.ProbeConfig.items(), Label)."""
    return {value[2:].decode("ascii", "replace"): struct.unpack_from("<H", value)[0]
            for tag, value in describe(hst) if tag & 0x7F == CORE_LABEL and len(value) >= 2}


def max_op_ms(hst: h.Host) -> int:
    """The longest one request may take on this probe (oep.core describe max_op_ms, core §7.5): the ceiling of run's
    timeout_ms, a dmi list's waits, an attach's hold_ms. A probe that declares none (not v1-complete) is taken as the
    reference firmware's 10000 ms."""
    for tag, value in describe(hst):
        if tag & 0x7F == CORE_MAX_OP_MS and len(value) >= 4:
            return struct.unpack_from("<I", value)[0]
    return reg.LIMITS["max_op_ms_reference"]


def transports(hst: h.Host) -> list[tuple[int, int, int]]:
    """The probe's transports from oep.core's describe (core §7.5): [(index, kind, usb interface or 0xFF)]."""
    return [(v[0], v[1], v[2] if len(v) > 2 else 0xFF) for tag, v in describe(hst) if tag & 0x7F == CORE_TRANSPORT
            and len(v) >= 2]


def take(hst: h.Host, lease_ms: int = 3000, *, owner: str | None = None, wait_s: float = 5.0,
         force: bool = False) -> h.Opened:
    """Take the lock as host guide §2 says: when the probe's only transport is a serial port and this host opened it
    exclusively, the previous holder cannot be there any more - force at once; otherwise wait out the holder's lease
    (up to wait_s), and name it (InUse) if it keeps it going. force: the user asked for it."""
    link = getattr(hst, "link", None)
    ways = transports(hst)
    only = len(ways) == 1 and ways[0][1] in SERIAL_KINDS and getattr(link, "framing", None) == "cobs" \
        and getattr(link, "transport", None) == "serial"
    return hst.take(lease_ms, owner=owner, only_way_in=only, wait_s=wait_s, force=force)


def plan_apply(hst: h.Host, assignments: list[tuple[int, int, int]]) -> None:
    """assignments: (fn, role, channel). The fns named get these plans, every other fn keeps its own (oep-core §8);
    all interfaces accept their roles or nothing changes. The plan is the
    session's resource: kept over an explicit end, released at a lease lapse or a force takeover (oep-core §9)."""
    tlv = b"".join(bytes([TAG_ROLE_ASSIGNMENT, 5]) + struct.pack("<HBH", fn, role, ch) for fn, role, ch in assignments)
    try:
        hst.call(m.CORE_FN, OP_PLAN_APPLY, tlv)
    except h.Rejected as e:
        if e.result.detail != m.UNAVAILABLE:
            raise
        why = _pin_holders(hst, assignments)
        if why:
            raise PinsTaken(e.result, why) from None
        raise


class PinsTaken(h.Rejected):
    """plan_apply refused (unavailable) and the probe's settings say why: a pin kept by another fn's saved plan or by a
    slot, or a channel the settings disable (oep.probe.config). `holders`: (channel, what holds it)."""

    def __init__(self, result, holders: list[tuple[int, str]]):
        super().__init__(result)
        self.holders = holders

    def __str__(self) -> str:
        return "pins taken: " + "; ".join(f"channel {ch} by {who}" for ch, who in self.holders) + \
            " (oep config show; oep config remove <probe> plan <fn> frees a saved plan)"


def _pin_holders(hst: h.Host, assignments: list[tuple[int, int, int]]) -> list[tuple[int, str]]:
    """What the settings keep on the channels asked for (best effort: an empty list when there is no oep.probe.config
    or it cannot be read)."""
    from . import config
    try:
        items = config.ProbeConfig(hst).items()
    except (LookupError, h.OepError):
        return []
    fns = {fn for fn, _, _ in assignments}
    out = []
    for fn, role, ch in assignments:
        for it in items:
            if isinstance(it, config.Plan) and it.channel == ch and it.fn not in fns:
                out.append((ch, f"the saved plan of fn {it.fn} (role {it.role})"))
            elif isinstance(it, config.Slot) and ch in it.pins:
                out.append((ch, f"slot {it.slot} {it.name}"))
            elif isinstance(it, config.Disable) and it.channel == ch:
                out.append((ch, "the settings (disabled; oep config remove <probe> disable <channel> enables it)"))
    return out


def plan_release(hst: h.Host, fns: list[int] | tuple[int, ...] = ()) -> None:
    """Release the plan of these fns (none: every fn, oep-core §8)."""
    hst.call(m.CORE_FN, OP_PLAN_RELEASE, bytes([len(fns)]) + b"".join(struct.pack("<H", f) for f in fns))


class Interface:
    """One interface client: its fn (found by name, cached), and calls that raise Rejected / Failed unless the probe
    says it worked. `prefix` goes in front of every payload (the connection byte of a target interface).
    REVISION: the interface revision whose shapes the class speaks; the list entry must say the same (None: any)."""

    NAME = ""
    REVISION: int | None = None

    def __init__(self, hst: h.Host, name: str | None = None, prefix: bytes = b"", fn: int | None = None):
        self.host = hst
        self.name = name or self.NAME
        self.fn = fn if fn is not None else find(hst, self.name)
        self.prefix = prefix
        if self.REVISION is not None:
            rev = revision(hst, self.name, self.fn)
            if rev != self.REVISION:
                raise UnsupportedRevision(f"{self.name} (fn {self.fn}) is revision {rev} on this probe; this client "
                                          f"speaks revision {self.REVISION} only")

    def _expecting(self, ms: int):
        """Host.expecting(ms) when the host has it (a stand-in host may not)."""
        expecting = getattr(self.host, "expecting", None) if ms else None
        return expecting(ms) if expecting else contextlib.nullcontext()

    def _call(self, op: int, body: bytes = b"", *, locked: bool = True, expect_ms: int = 0) -> m.Result:
        with self._expecting(expect_ms):
            return self.host.call(self.fn, op, self.prefix + body, locked=locked)

    def _request(self, op: int, body: bytes = b"", *, locked: bool = True, expect_ms: int = 0) -> m.Result:
        """Rejections raise; completed results of any outcome come back for the caller to decode. expect_ms: how
        long it may take on the probe (Host.expecting)."""
        with self._expecting(expect_ms):
            return self.host.request(self.fn, op, self.prefix + body, locked=locked)

    def request(self, op: int, body: bytes = b"") -> tuple[int, int, bytes]:
        """The raw (fn, op, payload) of one operation, for Host.pipeline / pipeline_calls."""
        return self.fn, op, self.prefix + body


LINK_SOURCE, LINK_SINK = m.OP_LINK_SOURCE, m.OP_LINK_SINK   # core, lock-free


def link_speed(hst: h.Host, *, size: int | None = None, inflight: int | None = None, seconds: float = 1.0) -> dict:
    """The link's request/response throughput both ways, as a repeat read or a write sees it: `inflight` requests of
    `size` bytes kept going in batches for `seconds` (defaults: one full frame, the probe's in-flight limit).
    -> {"in_mb_s", "out_mb_s", "size", "inflight"} (in = probe to host)."""
    import time
    limits = confirm(hst)
    size = size or limits["max_frame"] - 16
    inflight = min(inflight or limits["max_inflight"], limits["max_inflight"])
    first = hst.call(m.CORE_FN, LINK_SOURCE, struct.pack("<I", size), locked=False).payload
    if len(first) != size or any(b != (k & 0xFF) for k, b in enumerate(first[:256])):
        raise h.ProtocolError(f"link_source answered {len(first)} bytes, not the {size} asked (or a wrong pattern)")
    out = {"size": size, "inflight": inflight}
    for key, op, body in (("in_mb_s", LINK_SOURCE, struct.pack("<I", size)), ("out_mb_s", LINK_SINK, bytes(size))):
        moved, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            for r in hst.pipeline_calls([(m.CORE_FN, op, body)] * inflight, locked=False):
                moved += len(r.payload) if op == LINK_SOURCE else m.Reader(r.payload).u32()
        out[key] = moved / (time.perf_counter() - t0) / 1e6
    return out
