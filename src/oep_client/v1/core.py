"""The probe's core (fn 0) as the other clients need it: finding interfaces by name, confirm, the probe's own labels,
the pin plan - and `Interface`, the base every interface client shares (its fn, its revision checked against the list
entry, and calls that raise unless they worked)."""

from __future__ import annotations

import struct

from . import catalog, host as h, message as m, registry as reg

OP_PLAN_APPLY, OP_PLAN_RELEASE = m.OP_PLAN_APPLY, m.OP_PLAN_RELEASE
TAG_ROLE_ASSIGNMENT = reg.CORE.tlv["plan_apply"]["role_assignment"]     # already critical (0x90)
CORE_LABEL = reg.CORE.tlv["describe"]["label"]


class UnsupportedRevision(h.OepError):
    """The probe offers the interface in a revision whose payload shapes this client does not speak (v1 wire §0:
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


def probe_labels(hst: h.Host) -> dict[str, int]:
    """Channel labels the probe declares in oep.core's describe (tag 0x46): {"NRST": 23, ...}."""
    data, first = b"", 0
    while True:
        p = hst.request(m.CORE_FN, m.OP_DESCRIBE, catalog.pack_describe_request(0, first), locked=False).payload
        more, chunk = m.Reader(p).u8(), p[1:]
        data += chunk
        first += len(catalog.split_tlv(chunk))
        if not more or not chunk:
            break
    return {value[2:].decode("ascii", "replace"): struct.unpack_from("<H", value)[0]
            for tag, value in catalog.split_tlv(data) if tag & 0x7F == CORE_LABEL and len(value) >= 2}


def plan_apply(hst: h.Host, assignments: list[tuple[int, int, int]]) -> None:
    """assignments: (fn, role, channel). All interfaces accept their roles or none is applied. The plan is probe
    state: it stays until plan_release, whatever happens to the session."""
    tlv = b"".join(bytes([TAG_ROLE_ASSIGNMENT, 5]) + struct.pack("<HBH", fn, role, ch) for fn, role, ch in assignments)
    hst.call(m.CORE_FN, OP_PLAN_APPLY, tlv)


def plan_release(hst: h.Host) -> None:
    hst.call(m.CORE_FN, OP_PLAN_RELEASE)


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

    def _call(self, op: int, body: bytes = b"", *, locked: bool = True) -> m.Result:
        return self.host.call(self.fn, op, self.prefix + body, locked=locked)

    def _request(self, op: int, body: bytes = b"", *, locked: bool = True) -> m.Result:
        """Rejections raise; completed results of any outcome come back for the caller to decode."""
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
