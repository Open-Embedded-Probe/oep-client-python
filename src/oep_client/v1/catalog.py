"""Draft wire forms for capability discovery by name (oep-spec docs/capability-declaration-model.ja.md).

list request : flags(u8) first(u16) prefix_len(u8) prefix     flags bit0 = exact
list result  : total(u16) count(u8) entries [TLV tail]         oep.core (fn 0) is the first entry
list entry   : fn(u16) instance(u16) revision(u8) flags(u8) name_len(u8) name
describe     : request fn(u16) first(u16); result more(u8) then TLV bytes (tag u8, len u8, value;
               tag bit 7 = critical). more = 1: TLVs remain after this page, ask again from first + count
(oep-spec v1-core-wire-delta §5; the entry revision decides the interface's payload shapes, §0)

Common TLV tags 0x01..0x3F mean the same for every interface; 0x40..0x7F belong to the interface.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .message import Reader, split_tlvs

LIST_EXACT = 0x01

# Common describe tags.
ROLE_CHANNELS = 0x01     # role(u8) base(u16) bitmap(bytes): channels usable for that role
MAX_CLOCK_HZ = 0x02      # u32
MAX_LENGTH = 0x03        # u16
EXCLUSIVE_GROUP = 0x04   # u16, repeated
MIN_CLOCK_HZ = 0x05      # u32
FEATURES = 0x06          # u32, bits defined by the interface
IMPLEMENTATION = 0x07    # u8: 0 unspecified, 1 software, 2 peripheral, 3 peripheral + DMA/PIO
CHANNEL_GROUP = 0x08     # group(u8) then (role(u8), channel(u16)) repeated: a fixed pin set
CRITICAL = 0x80
INTERFACE_TAG_FIRST = 0x40

IMPLEMENTATIONS = {0: "unspecified", 1: "software (bit-bang)", 2: "peripheral", 3: "peripheral + DMA/PIO"}


# ---- list ----------------------------------------------------------------

@dataclass(frozen=True)
class ListEntry:
    fn: int
    instance: int
    revision: int
    flags: int
    name: str


def pack_list_request(prefix: str = "", exact: bool = False, first: int = 0) -> bytes:
    raw = prefix.encode("ascii")
    return struct.pack("<BHB", LIST_EXACT if exact else 0, first, len(raw)) + raw


def unpack_list_request(payload: bytes) -> tuple[str, bool, int, bytes]:
    """-> (prefix, exact, first, the request's TLV tail)."""
    if len(payload) < 4:
        raise ValueError("list request shorter than its fixed part")
    flags, first, n = struct.unpack_from("<BHB", payload)
    if len(payload) < 4 + n:
        raise ValueError("list request: prefix length does not match")
    return payload[4:4 + n].decode("ascii"), bool(flags & LIST_EXACT), first, payload[4 + n:]


def pack_entry(e: ListEntry) -> bytes:
    raw = e.name.encode("ascii")
    return struct.pack("<HHBBB", e.fn, e.instance, e.revision, e.flags, len(raw)) + raw


def pack_list_result(total: int, entries: list[ListEntry]) -> bytes:
    return struct.pack("<HB", total, len(entries)) + b"".join(pack_entry(e) for e in entries)


def unpack_list_result(payload: bytes) -> tuple[int, list[ListEntry]]:
    """-> (total, this page's entries). What follows the counted entries is a §0 TLV tail: skipped."""
    rd = Reader(payload)
    total, count = rd.take("HB")
    out = []
    for _ in range(count):
        fn, instance, revision, flags, n = rd.take("HHBBB")
        out.append(ListEntry(fn, instance, revision, flags, rd.bytes(n).decode("ascii", "replace")))
    rd.tail()
    return total, out


def pack_describe_request(fn: int, first: int) -> bytes:
    return struct.pack("<HH", fn, first)


# ---- describe TLVs -------------------------------------------------------

def tlv(tag: int, value: bytes) -> bytes:
    if len(value) > 255:
        raise ValueError(f"TLV 0x{tag:02x}: value of {len(value)} bytes does not fit")
    return bytes((tag, len(value))) + value


def split_tlv(data: bytes) -> list[tuple[int, bytes]]:
    return split_tlvs(data)


def channels_to_bitmap(channels) -> tuple[int, bytes]:
    chans = sorted(set(channels))
    if not chans:
        return 0, b""
    base = chans[0]
    bits = bytearray((chans[-1] - base) // 8 + 1)
    for c in chans:
        bits[(c - base) // 8] |= 1 << ((c - base) % 8)
    return base, bytes(bits)


def bitmap_to_channels(base: int, bitmap: bytes) -> list[int]:
    return [base + i * 8 + b for i, byte in enumerate(bitmap) for b in range(8) if byte >> b & 1]


def role_channels(role: int, channels) -> bytes:
    base, bits = channels_to_bitmap(channels)
    return tlv(ROLE_CHANNELS, struct.pack("<BH", role, base) + bits)


def channel_group(group: int, pins: list[tuple[int, int]]) -> bytes:
    return tlv(CHANNEL_GROUP, bytes([group]) + b"".join(struct.pack("<BH", r, c) for r, c in pins))


def u8(tag: int, v: int) -> bytes:
    return tlv(tag, struct.pack("<B", v))


def u16(tag: int, v: int) -> bytes:
    return tlv(tag, struct.pack("<H", v))


def u32(tag: int, v: int) -> bytes:
    return tlv(tag, struct.pack("<I", v))


def text(tag: int, s: str) -> bytes:
    return tlv(tag, s.encode("ascii"))


@dataclass
class Description:
    """Common tags decoded; interface-specific and unknown tags kept raw, in order."""

    roles: dict[int, list[int]] = field(default_factory=dict)
    groups: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    max_clock_hz: int | None = None
    min_clock_hz: int | None = None
    max_length: int | None = None
    exclusive_groups: list[int] = field(default_factory=list)
    features: int | None = None
    implementation: int | None = None
    specific: list[tuple[int, bytes]] = field(default_factory=list)
    unknown_critical: list[int] = field(default_factory=list)


def decode_description(data: bytes) -> Description:
    d = Description()
    for tag, value in split_tlv(data):
        t = tag & ~CRITICAL
        if t == ROLE_CHANNELS:
            role, base = struct.unpack_from("<BH", value)
            d.roles.setdefault(role, []).extend(bitmap_to_channels(base, value[3:]))
        elif t == CHANNEL_GROUP:
            group = value[0]
            d.groups[group] = [struct.unpack_from("<BH", value, 1 + 3 * i) for i in range((len(value) - 1) // 3)]
        elif t == MAX_CLOCK_HZ:
            d.max_clock_hz = struct.unpack("<I", value)[0]
        elif t == MIN_CLOCK_HZ:
            d.min_clock_hz = struct.unpack("<I", value)[0]
        elif t == MAX_LENGTH:
            d.max_length = struct.unpack("<H", value)[0]
        elif t == EXCLUSIVE_GROUP:
            d.exclusive_groups.append(struct.unpack("<H", value)[0])
        elif t == FEATURES:
            d.features = struct.unpack("<I", value)[0]
        elif t == IMPLEMENTATION:
            d.implementation = value[0]
        elif t >= INTERFACE_TAG_FIRST:
            d.specific.append((tag, value))
        elif tag & CRITICAL:
            d.unknown_critical.append(tag)
        # an unknown non-critical common tag is skipped
    for role in d.roles:
        d.roles[role] = sorted(set(d.roles[role]))
    return d
