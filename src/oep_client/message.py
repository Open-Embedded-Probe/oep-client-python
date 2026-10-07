"""v1 messages (oep-spec docs/oep-core.ja.md §2-§5): one request header, the result header, and the §2.3 rules for
what follows a payload's fixed part.

  request : role(0x01) corr(u16) fn(u16) op(u8) session_id(u32) payload     10 bytes; session_id 0 = no session
  result  : role(0x02) corr(u16) resolution(u8) detail(u8) payload

§2.3: every fixed form (a fixed part, a TLV's value, a sequence's element, a probe.config item) is fixed by (name,
revision) and never extended at its end; a sequence is count x element with no element length. After a result's fixed
part (and any counted list) come TLVs (tag u8, len u16, value - core §2.2, one form whatever the length); the host skips
tags it does not know. A request may end with TLVs too; tag bit 7 = critical: a probe that does not implement the tag
refuses the request rejected unsupported (the tag as received) when it is set, and ignores the TLV when it is not; a
TLV the probe implements is checked the same with or without the bit (core §2.3). Tags 0x00 and 0x7F are never TLV
tags. Numbers come from `registry` (generated from oep-spec registry/oep-v1.toml).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import registry as reg

ROLE_REQUEST, ROLE_RESULT = reg.ROLES["request"], reg.ROLES["result"]
ROLE_EVENT, ROLE_DATA = reg.ROLES["event"], reg.ROLES["data"]
REQUEST_HEADER, RESULT_HEADER = 10, 5           # core §4.1 / §4.2
NO_SESSION_ID = 0                                 # a request's session_id when it belongs to no session (core §4.1)
TLV_HEADER = 3                                    # tag(u8) len(u16) (core §2.2)

REJECTED, COMPLETED, ACCEPTED = (reg.RESOLUTIONS[k] for k in ("rejected", "completed", "accepted"))
SUCCESS, FAILED, PARTIAL = (reg.OUTCOMES[k] for k in ("success", "failed", "partial"))

_R = reg.REJECT_REASONS
UNKNOWN_FUNCTION = _R["unknown_function"]
UNKNOWN_OPERATION = _R["unknown_operation"]
MALFORMED = _R["malformed"]
UNAVAILABLE = _R["unavailable"]
BUSY = _R["busy"]                          # reserved: a host takes it as a failure (core §2.4)
WINDOW_EXCEEDED = _R["window_exceeded"]
NO_SESSION = _R["no_session"]              # a session_id while no session holds the lock: open a new session
LOCKED = _R["locked"]                      # another session holds the lock; payload = remaining ms (u32)
SESSION_REQUIRED = _R["session_required"]  # an op that needs the lock came with session_id 0
NO_CONNECTION = _R["no_connection"]        # the probe does not know the request's connection: attach again
UNSUPPORTED = _R["unsupported"]            # a critical TLV (payload: its tag) or a fixed-part value it cannot handle
RESULT_LOST = _R["result_lost"]            # a request sent again whose result the probe did not keep: read the state again

REJECT_NAMES = {v: k.replace("_", " ") for k, v in _R.items()}
REJECT_NAMES[MALFORMED] = "malformed payload"

TAG_CRITICAL = reg.TAG_CRITICAL
TAG_RESERVED = 0x7F                        # never a TLV tag, in every context (core §2.2, §2.5)
TAG_FIXED = reg.TAG_RESERVED_ZERO          # the rejected unsupported payload's first byte for a fixed-part value (core §4.3)
TAG_OPS = reg.DESCRIBE_COMMON["ops"]       # every fn's describe: base(u8) bitmap - the ops it offers (core §1.2, §7.4)

# the core's operations (fn 0: the core has no name and is never listed, core §0, §7.2)
CORE_FN = 0
_OP = reg.CORE.op
OP_CONFIRM, OP_LIST, OP_DESCRIBE, OP_CLOCK = _OP["confirm"], _OP["list"], _OP["describe"], _OP["clock"]
OP_OPEN, OP_END, OP_KEEPALIVE, OP_LOCK_STATE = _OP["open"], _OP["end"], _OP["keepalive"], _OP["lock_state"]
CORE_OPS = frozenset(_OP.values())           # every op fn 0 has: all mandatory (core §1.2, §12)
# subscribe / unsubscribe: ops of the interface that sends notifications, at the same numbers in every interface's op
# space (core §11.3) - sent to that fn itself, never to fn 0
OP_SUBSCRIBE, OP_UNSUBSCRIBE = reg.OP_SUBSCRIBE, reg.OP_UNSUBSCRIBE

CONFIRM_REQUEST = reg.CONFIRM_REQUEST_MAGIC.encode()
CONFIRM_RESULT = reg.CONFIRM_RESULT_MAGIC.encode()


class OepError(Exception):
    """Anything the probe or the link said no to."""


class ProtocolError(OepError, ValueError):
    """A result that does not fit: wrong correlation, too short, a value this host does not know."""


class ShortPayload(ProtocolError):
    """A payload shorter than its fixed part, or a truncated TLV (a broken result, core §2.3)."""


@dataclass(frozen=True)
class Request:
    """One request (core §4.1). `session`: the session_id in the header - 0 (None is taken as 0) for a request that
    belongs to no session, which only an op that needs no lock may be."""
    corr: int
    fn: int
    op: int
    payload: bytes = b""
    session: int = NO_SESSION_ID

    def __post_init__(self):
        if self.session is None:
            object.__setattr__(self, "session", NO_SESSION_ID)

    def pack(self) -> bytes:
        return struct.pack("<BHHBI", ROLE_REQUEST, self.corr, self.fn, self.op, self.session) + self.payload

    @classmethod
    def unpack(cls, data: bytes) -> Request:
        if len(data) < REQUEST_HEADER:
            raise ValueError("request shorter than its header")
        role, corr, fn, op, session = struct.unpack_from("<BHHBI", data)
        if role != ROLE_REQUEST:
            raise ValueError(f"not a request: role 0x{role:02x}")
        return cls(corr, fn, op, data[REQUEST_HEADER:], session)


@dataclass(frozen=True)
class Result:
    corr: int
    resolution: int
    detail: int
    payload: bytes = b""

    def pack(self) -> bytes:
        return struct.pack("<BHBB", ROLE_RESULT, self.corr, self.resolution, self.detail) + self.payload

    @classmethod
    def unpack(cls, data: bytes) -> Result:
        if len(data) < RESULT_HEADER:
            raise ShortPayload("result shorter than its header")
        role, corr, resolution, detail = struct.unpack_from("<BHBB", data)
        if role != ROLE_RESULT:
            raise ValueError(f"not a result: role 0x{role:02x}")
        return cls(corr, resolution, detail, data[RESULT_HEADER:])

    @property
    def succeeded(self) -> bool:
        return self.resolution == COMPLETED and self.detail == SUCCESS

    @property
    def ran(self) -> bool:
        """Completed with a known outcome (success, failed, partial): the payload has the op's result shape. An unknown
        resolution or outcome is a failure whose payload means nothing (§0)."""
        return self.resolution == COMPLETED and self.detail in (SUCCESS, FAILED, PARTIAL)

    def describe(self) -> str:
        if self.resolution == REJECTED:
            return f"rejected: {REJECT_NAMES.get(self.detail, f'reason 0x{self.detail:02x}')}"
        if self.resolution == ACCEPTED:
            return "accepted"
        if self.resolution != COMPLETED:
            return f"unknown resolution 0x{self.resolution:02x}"
        return {SUCCESS: "completed", FAILED: "failed", PARTIAL: "partial"}.get(self.detail,
                                                                               f"unknown outcome 0x{self.detail:02x}")


# ---- §0 TLV tails ------------------------------------------------------------------------------------------

def tlv(tag: int, value: bytes, critical: bool = False) -> bytes:
    """One TLV, `tag(u8) len(u16) value` (core §2.2: one form for every length). `critical` sets tag bit 7 (a request
    argument the probe must honour or refuse)."""
    if len(value) > 0xFFFF:
        raise ValueError(f"TLV 0x{tag:02x}: value of {len(value)} bytes does not fit a u16 length")
    if tag & 0x7F in (TAG_RESERVED, TAG_FIXED):
        raise ValueError("tags 0x00 and 0x7F are never TLV tags (core §2.2)")
    return struct.pack("<BH", tag | (TAG_CRITICAL if critical else 0), len(value)) + value


def split_tlvs(data: bytes) -> list[tuple[int, bytes]]:
    """TLVs in order (core §2.2). A truncated TLV - a header cut short, or a len past the end - raises ShortPayload
    (the result is broken)."""
    pos, out = 0, []
    while pos < len(data):
        if pos + TLV_HEADER > len(data):
            raise ShortPayload("TLV: truncated header")
        tag, n = struct.unpack_from("<BH", data, pos)
        pos += TLV_HEADER
        if pos + n > len(data):
            raise ShortPayload(f"TLV 0x{tag:02x}: truncated value")
        out.append((tag, data[pos:pos + n]))
        pos += n
    return out


@dataclass
class Tail:
    """The TLVs after a result's known part, in order (unknown tags included, for whoever knows them)."""
    tlvs: list[tuple[int, bytes]] = field(default_factory=list)

    def get(self, tag: int) -> bytes | None:
        """The first TLV of `tag` (core §2.3: a tag twice in an answer - the host uses the first)."""
        return next((v for t, v in self.tlvs if t == tag), None)

    @classmethod
    def parse(cls, data: bytes) -> Tail:
        return cls(split_tlvs(data))


class Reader:
    """Reads a result payload's fixed part front to back; too short -> ShortPayload. `tail()` then reads the rest as
    core §2.3 TLVs; `rest()` takes the rest as bytes (an answer that is a TLV list itself: probe.config get)."""

    def __init__(self, payload: bytes):
        self.data, self.at = payload, 0

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        if self.at + size > len(self.data):
            raise ShortPayload(f"payload of {len(self.data)} bytes: needs {self.at + size} for its fixed part")
        values = struct.unpack_from("<" + fmt, self.data, self.at)
        self.at += size
        return values if len(values) > 1 else values[0]

    def u8(self) -> int:
        return self.take("B")

    def u16(self) -> int:
        return self.take("H")

    def u32(self) -> int:
        return self.take("I")

    def u64(self) -> int:
        return self.take("Q")

    def bytes(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise ShortPayload(f"payload of {len(self.data)} bytes: needs {self.at + n}")
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def words(self, n: int) -> list[int]:
        return list(struct.unpack(f"<{n}I", self.bytes(4 * n)))

    def counted(self, fmt: str) -> bytes:
        """A byte string with its length in front (`fmt` = the length's struct code: H for u16, I for u32): what every
        answer carrying data has since core §2.3 put a length on every container."""
        return self.bytes(self.take(fmt))

    def rest(self) -> bytes:
        out = self.data[self.at:]
        self.at = len(self.data)
        return out

    def tail(self) -> Tail:
        return Tail.parse(self.rest())


def shown(raw: bytes) -> str:
    """Text from an answer, made safe to show (core §2.1): invalid UTF-8 replaced, and every C0 control character
    (0x00-0x1F) and 0x7F replaced by U+FFFD - an owner or a label can never move a terminal's cursor or colour it."""
    text = bytes(raw).decode("utf-8", "replace")
    return "".join("\ufffd" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in text)


def valid_text(raw: bytes) -> bool:
    """Text this host puts in a request: valid UTF-8 without C0 control characters or 0x7F (what a host shows after
    replacing them, core §2.1; the probe does not refuse other text)."""
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def serial_diff(a: int, b: int, bits: int = 32) -> int:
    """a - b for values that wrap (serials u32, seq u16, resource numbers u16): the difference as a signed number of
    `bits` (core §2.6)."""
    d = (a - b) & ((1 << bits) - 1)
    return d - (1 << bits) if d >> (bits - 1) else d
