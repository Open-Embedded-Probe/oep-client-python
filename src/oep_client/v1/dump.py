"""Collect a probe's declared capabilities (list + describe, paged) and render them.

Talks only through `call(fn, op, payload) -> payload`, so the same code runs against the in-process
fake now and a real transport later.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

from . import catalog, interfaces, message as m, names

CORE_FN, OP_CONFIRM, OP_LIST, OP_DESCRIBE = 0, 0x01, 0x02, 0x03


@dataclass
class Offer:
    entry: catalog.ListEntry
    description: catalog.Description


@dataclass
class Capabilities:
    revision: int
    max_frame: int
    offers: list[Offer] = field(default_factory=list)
    requests: dict[str, int] = field(default_factory=dict)


def collect(call, prefix: str = "", exact: bool = False) -> Capabilities:
    p = call(CORE_FN, OP_CONFIRM, m.CONFIRM_REQUEST + bytes([0, 1]))     # v0 and v1 both answer
    magic, revision = struct.unpack_from("<4sB", p)
    if magic != m.CONFIRM_RESULT:
        raise ValueError("not an OEP endpoint")
    # revision 0: max_frame(u16) follows; revision 1: flags(u8), then max_frame(u16)
    max_frame = struct.unpack_from("<H", p, 5 if revision == 0 else 6)[0]
    caps = Capabilities(revision, max_frame, requests={"confirm": 1, "list": 0, "describe": 0})
    entries: list[catalog.ListEntry] = []
    while True:
        total, page = catalog.unpack_list_result(call(CORE_FN, OP_LIST, catalog.pack_list_request(prefix, exact, len(entries))))
        caps.requests["list"] += 1
        entries += page
        if len(entries) >= total or not page:
            break
    for e in entries:
        data, first = b"", 0
        while True:
            result = call(CORE_FN, OP_DESCRIBE, catalog.pack_describe_request(e.fn, first))
            caps.requests["describe"] += 1
            more, chunk = result[0], result[1:]
            data += chunk
            first += len(catalog.split_tlv(chunk))
            if not more or not chunk:
                break
        caps.offers.append(Offer(e, catalog.decode_description(data)))
    return caps


# ---- rendering -----------------------------------------------------------

def _hz(v: int) -> str:
    for unit, div in (("MHz", 1_000_000), ("kHz", 1_000)):
        if v >= div and v % (div // 1000 or 1) == 0:
            return f"{v / div:g} {unit}"
    return f"{v} Hz"


def _features(bits: int, known: dict[int, str]) -> list[str]:
    return [known.get(b, f"bit{b}") for b in range(32) if bits >> b & 1]


def describe_offer(o: Offer) -> dict:
    """One offer as plain data: what JSON carries and what text renders."""
    name = o.entry.name
    known = interfaces.KNOWN.get(name)
    d = o.description
    out = {"fn": o.entry.fn, "instance": o.entry.instance, "name": name, "revision": o.entry.revision,
           "namespace": names.kind(name), "known": known is not None}
    if known:
        out["summary"] = known.summary
    if d.roles:
        out["roles"] = {(known.roles.get(r, f"role{r}") if known else f"role{r}"): interfaces.ranges(ch)
                        for r, ch in sorted(d.roles.items())}
    if d.groups:
        out["pin_groups"] = {str(g): {(known.roles.get(r, f"role{r}") if known else f"role{r}"): c for r, c in pins}
                             for g, pins in sorted(d.groups.items())}
    for key in ("max_clock_hz", "min_clock_hz", "max_length"):
        if getattr(d, key) is not None:
            out[key] = getattr(d, key)
    if d.exclusive_groups:
        out["exclusive_groups"] = d.exclusive_groups
    if d.features is not None:
        out["features"] = _features(d.features, known.features if known else {})
    if d.implementation is not None:
        out["implementation"] = catalog.IMPLEMENTATIONS.get(d.implementation, str(d.implementation))
    specific = {}
    for tag, value in d.specific:
        label, decode = (known.tags.get(tag & 0x7F) if known else None) or (f"tag 0x{tag:02x}", lambda v: v.hex())
        # a tag may repeat (one label per channel): keep every value
        specific[label] = f"{specific[label]}; {decode(value)}" if label in specific else decode(value)
    if specific:
        out["declares"] = specific
    if d.unknown_critical:
        out["unusable"] = f"unknown critical tags {[hex(t) for t in d.unknown_critical]}"
    return out


def to_json(caps: Capabilities) -> str:
    return json.dumps({"revision": caps.revision, "max_frame": caps.max_frame, "requests": caps.requests,
                       "interfaces": [describe_offer(o) for o in caps.offers]}, indent=2)


def to_text(caps: Capabilities) -> str:
    rows = [describe_offer(o) for o in caps.offers]
    lines = [f"OEP revision {caps.revision}, max frame {caps.max_frame} bytes; "
             f"{len(rows)} interfaces in {caps.requests['list']} list and "
             f"{caps.requests['describe']} describe requests", ""]
    by_instance: dict[int, list[dict]] = {}
    for r in rows:
        by_instance.setdefault(r["instance"], []).append(r)
    for inst, group in by_instance.items():
        for i, r in enumerate(group):
            head = f"instance {inst:<3}" if i == 0 else " " * 12
            tag = "" if r["known"] else "   (not known to this host)"
            lines.append(f"{head} fn {r['fn']:<3} {r['name']}  rev {r['revision']}{tag}")
        for r in group:
            pad = " " * 14
            if len(group) > 1:
                lines.append(f"{pad}[{r['name']}]")
            if "summary" in r:
                lines.append(f"{pad}{r['summary']}")
            if "roles" in r:
                width = max(len(k) for k in r["roles"])
                for role, chans in r["roles"].items():
                    lines.append(f"{pad}  {role:<{width}}  channels {chans}")
            if "pin_groups" in r:
                for g, pins in r["pin_groups"].items():
                    lines.append(f"{pad}  pin set {g}: " + ", ".join(f"{k}={v}" for k, v in pins.items()))
            limits = []
            if "max_clock_hz" in r:
                limits.append(f"max {_hz(r['max_clock_hz'])}")
            if "min_clock_hz" in r:
                limits.append(f"min {_hz(r['min_clock_hz'])}")
            if "max_length" in r:
                limits.append(f"max length {r['max_length']}")
            if limits:
                lines.append(f"{pad}  " + ", ".join(limits))
            if r.get("features"):
                lines.append(f"{pad}  features: " + ", ".join(r["features"]))
            if "implementation" in r:
                lines.append(f"{pad}  implementation: {r['implementation']}")
            if r.get("exclusive_groups"):
                lines.append(f"{pad}  exclusive group {', '.join(map(str, r['exclusive_groups']))}")
            for k, v in r.get("declares", {}).items():
                lines.append(f"{pad}  {k}: {v}")
            if "unusable" in r:
                lines.append(f"{pad}  UNUSABLE: {r['unusable']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
