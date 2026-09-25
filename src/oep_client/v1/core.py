"""The probe's core (fn 0) as the other clients need it: finding interfaces by name, confirm, the probe's own labels,
the pin plan - and `Interface`, the base every interface client shares (its fn, and calls that raise unless they
worked)."""

from __future__ import annotations

import struct

from . import catalog, host as h, message as m

OP_PLAN_APPLY, OP_PLAN_RELEASE = 0x04, 0x05
TAG_ROLE_ASSIGNMENT = 0x90
CORE_LABEL = 0x46


def _list(hst: h.Host, name: str) -> list[int]:
    fns, first = [], 0
    while True:
        total, page = catalog.unpack_list_result(
            hst.request(m.CORE_FN, m.OP_LIST, catalog.pack_list_request(name, True, first), locked=False).payload)
        fns += [e.fn for e in page]
        first += len(page)
        if not page or first >= total:          # an empty page ends it too: no endless loop on a short answer
            return fns


def find_all(hst: h.Host, name: str) -> list[int]:
    """fns of every interface with exactly this name (instances of the same kind, e.g. two UARTs)."""
    return _list(hst, name)


def find(hst: h.Host, name: str) -> int:
    """fn of the first interface with exactly this name. Cached on the host until the probe reboots (boot_id), so
    building a client per connection costs no list request."""
    fn = hst._fns.get(name)
    if fn is None:
        fns = _list(hst, name)
        if not fns:
            raise LookupError(f"probe does not offer {name}")
        fn = hst._fns[name] = fns[0]
    return fn


def confirm(hst: h.Host) -> dict:
    p = hst.request(m.CORE_FN, m.OP_CONFIRM, locked=False).payload
    magic, revision, max_frame, window, inflight = struct.unpack("<4sBHHB", p[:10])
    return {"magic": magic, "revision": revision, "max_frame": max_frame, "window": window, "max_inflight": inflight}


def probe_labels(hst: h.Host) -> dict[str, int]:
    """Channel labels the probe declares in oep.core's describe (tag 0x46): {"NRST": 23, ...}."""
    data, first = b"", 0
    while True:
        p = hst.request(m.CORE_FN, m.OP_DESCRIBE, struct.pack("<HB", 0, first), locked=False).payload
        chunk = p[1:]
        data += chunk
        first += len(catalog.split_tlv(chunk))
        if not p[0] or not chunk:
            break
    return {value[2:].decode("ascii", "replace"): struct.unpack_from("<H", value)[0]
            for tag, value in catalog.split_tlv(data) if tag & 0x7F == CORE_LABEL}


def plan_apply(hst: h.Host, assignments: list[tuple[int, int, int]]) -> None:
    """assignments: (fn, role, channel). All interfaces accept their roles or none is applied. The plan is probe
    state: it stays until plan_release, whatever happens to the session."""
    tlv = b"".join(bytes([TAG_ROLE_ASSIGNMENT, 5]) + struct.pack("<HBH", fn, role, ch) for fn, role, ch in assignments)
    hst.call(m.CORE_FN, OP_PLAN_APPLY, tlv)


def plan_release(hst: h.Host) -> None:
    hst.call(m.CORE_FN, OP_PLAN_RELEASE)


class Interface:
    """One interface client: its fn (found by name, cached), and calls that raise Rejected / Failed unless the probe
    says it worked. `prefix` goes in front of every payload (the connection byte of a target interface)."""

    NAME = ""

    def __init__(self, hst: h.Host, name: str | None = None, prefix: bytes = b"", fn: int | None = None):
        self.host = hst
        self.fn = fn if fn is not None else find(hst, name or self.NAME)
        self.prefix = prefix

    def _call(self, op: int, body: bytes = b"", *, locked: bool = True) -> m.Result:
        return self.host.call(self.fn, op, self.prefix + body, locked=locked)

    def request(self, op: int, body: bytes = b"") -> tuple[int, int, bytes]:
        """The raw (fn, op, payload) of one operation, for Host.pipeline / pipeline_calls."""
        return self.fn, op, self.prefix + body


LINK_SOURCE, LINK_SINK = 0x40, 0x41   # core, lock-free (numbers draft)


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
                moved += len(r.payload) if op == LINK_SOURCE else struct.unpack("<I", r.payload)[0]
        out[key] = moved / (time.perf_counter() - t0) / 1e6
    return out
