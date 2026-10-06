"""The probe's core (fn 0, which has no name and is never listed) as the other clients need it: finding interfaces by
name, confirm, describe and the ops it declares (with core §7.4's one encoding checked), the probe's own labels - the
pin plan (oep.probe.plan), restart_max_ms (oep.probe.restart) and the link test (oep.probe.link), each found by its
name - and `Interface`, the base every interface client shares (its fn, its revision checked against the list entry,
and calls that raise unless they worked)."""

from __future__ import annotations

import contextlib

import struct

from . import catalog, host as h, message as m, registry as reg

# oep.probe.plan (oep-if-plan): which channel each role of an interface uses; listed when an interface has plan roles
PLAN_NAME = reg.PROBE_PLAN.name
OP_PLAN_APPLY, OP_PLAN_RELEASE = reg.PROBE_PLAN.op["plan_apply"], reg.PROBE_PLAN.op["plan_release"]
TAG_ROLE_ASSIGNMENT = reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"]   # the number 0x10; always sent critical (0x90)
TAG_PLAN_ROLES = reg.PROBE_PLAN.tlv["describe"]["plan_roles"]
# oep.probe.restart (oep-if-restart): the probe restarts itself, optional
RESTART_NAME = reg.PROBE_RESTART.name
OP_RESTART = reg.PROBE_RESTART.op["restart"]
TAG_RESTART_MAX_MS = reg.PROBE_RESTART.tlv["describe"]["restart_max_ms"]
CORE_LABEL = reg.CORE.tlv["describe"]["label"]
CORE_TRANSPORT = reg.CORE.tlv["describe"]["transport"]
CORE_MAX_OP_MS = reg.CORE.tlv["describe"]["max_op_ms"]
TRANSPORT_KIND = reg.CORE.enum["transport_kind"]
SERIAL_KINDS = {TRANSPORT_KIND["uart_bridge"], TRANSPORT_KIND["usb_cdc"], TRANSPORT_KIND["usb_serial_jtag"]}


class UnsupportedRevision(h.OepError):
    """The probe offers the interface in a revision whose payload shapes this client does not speak (oep-core §2.7:
    a host never uses an interface revision it does not know)."""


class UnusableFunction(h.OepError, LookupError):
    """An fn whose describe breaks a rule that makes it unusable (core §7.4: its ops tag outside the one encoding): this
    host does not use it. fn 0's own raise NotUsable instead - the probe is not used."""


def list_entries(hst: h.Host, name: str = "", exact: bool = False) -> list[catalog.ListEntry]:
    """Every list entry under `name` (exact: that name only), paged by first (u16). The fn -> revision of each is
    remembered on the host. The core (fn 0) has no name and is never an entry (core §7.2): an entry with fn 0 from a
    probe that lists one anyway is left out."""
    entries: list[catalog.ListEntry] = []
    while True:
        total, page = catalog.unpack_list_result(
            hst.request(m.CORE_FN, m.OP_LIST, catalog.pack_list_request(name, exact, len(entries)), locked=False).payload)
        entries += page
        if not page or len(entries) >= total:    # an empty page ends it too: no endless loop on a short answer
            break
    entries = [e for e in entries if e.fn != m.CORE_FN]
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
    host while the probe's boot_id stays the same. fn 0's is checked as it comes: a max_op_ms outside 1..600000
    (core §4.4, §7.5) or an ops tag outside core §7.4's encoding makes the probe not used (NotUsable)."""
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
    if fn == m.CORE_FN:
        value = next((v for tag, v in out if tag & 0x7F == CORE_MAX_OP_MS and len(v) >= 4), None)
        why = h.check_max_op_ms(struct.unpack_from("<I", value)[0]) if value is not None else ""
        bad_ops = next((catalog.check_ops(v) for tag, v in out if tag & 0x7F == m.TAG_OPS and catalog.check_ops(v)), "")
        if bad_ops and not why:
            why = f"describe of fn 0: {bad_ops} - outside core §7.4's one encoding, so the probe is not used"
        if why:
            hst.not_usable(why)                                    # not conforming: not used (core §4.4, C-47)
    hst._describes[fn] = out
    return list(out)


def ops(hst: h.Host, fn: int = 0) -> set[int] | None:
    """The ops fn's describe declares in its ops tag (core §1.2, §7.4: the one declaration of every op an fn offers,
    the optional ones included); None when the describe carries no ops tag (a probe that does not conform: the host
    sends and lets the probe answer). An ops tag outside core §7.4's one encoding: the fn is not used -
    UnusableFunction (fn 0: NotUsable from `describe`, the probe is not used)."""
    found = None
    for tag, value in describe(hst, fn):
        if tag & 0x7F == m.TAG_OPS:
            why = catalog.check_ops(value)
            if why:
                raise UnusableFunction(f"fn {fn}: {why} - outside core §7.4's one encoding, so the fn is not used")
            found = (found or set()) | catalog.unpack_ops(value)
    return found


def offers(hst: h.Host, fn: int, op: int) -> bool:
    """Whether fn offers op by its ops tag (True when the describe declares no ops: unknown, the probe decides)."""
    declared = ops(hst, fn)
    return declared is None or op in declared


def not_offered(fn: int, op: int) -> h.Rejected:
    """What a request for an op the fn's ops tag does not set gets from the probe (core §1.2, §4.3 order 1): the same
    Rejected with detail unknown_operation as `Host.request` raises for that answer - a host that checks ops before
    sending raises this instead of sending."""
    return h.rejection(m.Result(0, m.REJECTED, m.UNKNOWN_OPERATION))


def require(hst: h.Host, fn: int, op: int) -> None:
    """Raise `not_offered` when fn's ops tag does not set op (nothing is sent then)."""
    if not offers(hst, fn, op):
        raise not_offered(fn, op)


def firmware_labels(hst: h.Host) -> list[tuple[int, str]]:
    """The firmware's fixed channel labels from fn 0's describe (tag 0x46) as (channel, text), in describe order -
    every one, two channels with the same text included (probe.config §1.3 step (c) finds none then). Text shown as
    core §2.1 says (`m.shown`)."""
    return [(struct.unpack_from("<H", value)[0], m.shown(value[2:]))
            for tag, value in describe(hst) if tag & 0x7F == CORE_LABEL and len(value) >= 2]


def probe_labels(hst: h.Host) -> dict[str, int]:
    """The firmware's fixed channel labels from fn 0's describe (tag 0x46): {"NRST": 23, ...}. Labels the
    settings gave are read from oep.probe.config (config.ProbeConfig.items(), Label)."""
    return {text: ch for ch, text in firmware_labels(hst)}


def max_op_ms(hst: h.Host) -> int:
    """The longest one request may take on this probe (fn 0's describe max_op_ms, core §7.5): the ceiling of run's
    timeout_ms, a dmi list's waits, an attach's hold_ms. A probe that declares none (not v1-complete) is taken as the
    reference firmware's 10000 ms."""
    for tag, value in describe(hst):
        if tag & 0x7F == CORE_MAX_OP_MS and len(value) >= 4:
            return struct.unpack_from("<I", value)[0]
    return reg.REFERENCE["max_op_ms"]


def restart_fn(hst: h.Host) -> int:
    """The fn of the probe's oep.probe.restart (LookupError: the probe offers none - it is optional, oep-if-restart)."""
    return find(hst, RESTART_NAME)


def restart_max_ms(hst: h.Host) -> int | None:
    """The longest the probe takes from restart's answer until it answers confirm again on the same transport
    (oep.probe.restart's describe restart_max_ms, oep-if-restart §1: required). None: the probe offers no
    oep.probe.restart, or its describe declares none (it does not conform)."""
    try:
        fn = restart_fn(hst)
    except LookupError:
        return None
    for tag, value in describe(hst, fn):
        if tag & 0x7F == TAG_RESTART_MAX_MS and len(value) >= 4:
            return struct.unpack_from("<I", value)[0]
    return None


def transports(hst: h.Host) -> list[tuple[int, int, int]]:
    """The probe's transports from fn 0's describe (core §7.5): [(index, kind, usb interface or 0xFF)]."""
    return [(v[0], v[1], v[2] if len(v) > 2 else 0xFF) for tag, v in describe(hst) if tag & 0x7F == CORE_TRANSPORT
            and len(v) >= 2]


def take(hst: h.Host, lease_ms: int = 3000, *, owner: str | None = None, wait_s: float = 5.0,
         force: bool = False) -> h.Opened:
    """Take the lock as host guide §6 says: when the probe's only transport is a serial port and this host opened it
    exclusively, the previous holder cannot be there any more - force at once; otherwise wait out the holder's lease
    (up to wait_s), and name it (InUse) if it keeps it going. force: the user asked for it."""
    link = getattr(hst, "link", None)
    ways = transports(hst)
    only = len(ways) == 1 and ways[0][1] in SERIAL_KINDS and getattr(link, "framing", None) == "cobs" \
        and getattr(link, "transport", None) == "serial"
    return hst.take(lease_ms, owner=owner, only_way_in=only, wait_s=wait_s, force=force)


def plan_fn(hst: h.Host) -> int:
    """The fn of the probe's oep.probe.plan (LookupError: the probe offers none - it lists one exactly when an
    interface has plan roles, oep-if-plan)."""
    return find(hst, PLAN_NAME)


def plan_roles(hst: h.Host) -> int | None:
    """The most role assignments the probe's plan holds at once, every fn together and the settings' plan included
    (oep.probe.plan's describe plan_roles, oep-if-plan §1). None: no oep.probe.plan, or no limit declared."""
    try:
        fn = plan_fn(hst)
    except LookupError:
        return None
    for tag, value in describe(hst, fn):
        if tag & 0x7F == TAG_PLAN_ROLES and len(value) >= 4:
            return struct.unpack_from("<I", value)[0]
    return None


def plan_apply(hst: h.Host, assignments: list[tuple[int, int, int]]) -> None:
    """assignments: (fn, role, channel), through oep.probe.plan (oep-if-plan §2.1; LookupError when the probe offers
    none). The fns named get these plans, every other fn keeps its own; all interfaces accept their roles or nothing
    changes. The plan is the session's resource: released when the session's lock ends, by end, lease expiry or force
    (core §9)."""
    tlv = b"".join(m.tlv(TAG_ROLE_ASSIGNMENT, struct.pack("<HBH", fn, role, ch), critical=True)
                   for fn, role, ch in assignments)
    try:
        hst.call(plan_fn(hst), OP_PLAN_APPLY, tlv)
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
    """Release the plan of these fns (none: every fn; the settings' plans stay), through oep.probe.plan (oep-if-plan
    §2.2)."""
    hst.call(plan_fn(hst), OP_PLAN_RELEASE, bytes([len(fns)]) + b"".join(struct.pack("<H", f) for f in fns))


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
        ops(hst, self.fn)                          # an ops tag outside core §7.4's encoding: not used (UnusableFunction)
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

    def ops(self) -> set[int] | None:
        """The ops this fn's describe declares (core §1.2, §7.4; None: no ops tag)."""
        return ops(self.host, self.fn)

    def offers(self, op: int) -> bool:
        """Whether this fn offers op by its ops tag (an optional op is offered exactly when set)."""
        return offers(self.host, self.fn, op)


# ---- oep.probe.link (oep-if-link): the link test and port_speed, an optional interface ------------------------------
_LINK = reg.PROBE_LINK
LINK_NAME = _LINK.name
LINK_SOURCE, LINK_SINK, LINK_PORT_SPEED = (_LINK.op[k] for k in ("source", "sink", "port_speed"))
LINK_SOURCE_OVERHEAD = reg.LIMITS["link_source_overhead_bytes"]   # source's len <= max_frame - this (oep-if-link §2)


def link_size(max_frame: int) -> int:
    """The most one source answer carries and one sink request may (oep-if-link §2): max_frame - 26, the answer's
    header, len and the ignored room (a sink request's header 10 and count 2 fit in that too)."""
    return max(1, max_frame - LINK_SOURCE_OVERHEAD)


def link_fn(hst: h.Host) -> int:
    """The fn of the probe's oep.probe.link (LookupError: the probe offers none - the link test and port_speed are
    optional, oep-if-link)."""
    return find(hst, LINK_NAME)


def link_source_request(size: int) -> bytes:
    """source's request: length(u32) (oep-if-link §2)."""
    return struct.pack("<I", size)


def link_sink_request(data: bytes) -> bytes:
    """sink's request: count(u16) data (oep-if-link §2)."""
    return struct.pack("<H", len(data)) + bytes(data)


def link_source_data(payload: bytes) -> bytes:
    """source's answer: len(u16) data [TLV] -> data (byte k = k & 0xFF; ShortPayload when len passes the end)."""
    rd = m.Reader(payload)
    data = rd.counted("H")
    rd.tail()
    return data


def link_speed(hst: h.Host, *, size: int | None = None, inflight: int | None = None, seconds: float = 1.0) -> dict:
    """The link's request/response throughput both ways through oep.probe.link source / sink (oep-if-link §2), as a repeat
    read or a write sees it: `inflight` requests of `size` bytes kept going in batches for `seconds` (defaults: one
    full frame, the probe's in-flight limit). -> {"in_mb_s", "out_mb_s", "size", "inflight"} (in = probe to host).
    LookupError when the probe offers no oep.probe.link."""
    import time
    fn = link_fn(hst)
    limits = confirm(hst)
    size = size or link_size(limits["max_frame"])
    inflight = min(inflight or limits["max_inflight"], limits["max_inflight"])
    first = link_source_data(hst.call(fn, LINK_SOURCE, link_source_request(size), locked=False).payload)
    if len(first) > size or any(b != (k & 0xFF) for k, b in enumerate(first[:256])):
        raise h.ProtocolError(f"source answered {len(first)} bytes for the {size} asked (or a wrong pattern)")
    size = len(first)                                     # what one frame carries (oep-if-link §2)
    out = {"size": size, "inflight": inflight}
    for key, op, body in (("in_mb_s", LINK_SOURCE, link_source_request(size)),
                          ("out_mb_s", LINK_SINK, link_sink_request(bytes(size)))):
        moved, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            for r in hst.pipeline_calls([(fn, op, body)] * inflight, locked=False):
                moved += len(link_source_data(r.payload)) if op == LINK_SOURCE else size
        out[key] = moved / (time.perf_counter() - t0) / 1e6
    return out
