"""v1 messages (oep-spec docs/v1-core-wire-delta.ja.md §0-§3): the v0 request/result headers plus the session flag,
and the §0 rules for what follows a payload's fixed part.

  request : role(0x01) corr(u16) fn(u16) op(u8) payload
            role(0x81) corr(u16) fn(u16) op(u8) session_id(u32) payload     role bit 7 = session_id present
  result  : role(0x02) corr(u16) resolution(u8) detail(u8) payload

§0 tails: after a result's fixed part (and any counted list) come TLVs (tag u8, len u8, value); the host skips tags it
does not know and never rejects a longer result. A request may end with TLVs too; tag bit 7 = critical (the probe
honours it or answers rejected unsupported with the tag), and the probe lists the non-critical tags it ignored in a
result TLV 0x7F. Numbers come from `registry` (generated from oep-spec registry/oep-v1.toml).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import registry as reg

ROLE_REQUEST, ROLE_RESULT = reg.ROLES["request"], reg.ROLES["result"]
ROLE_EVENT, ROLE_DATA = reg.ROLES["event"], reg.ROLES["data"]
ROLE_SESSION = reg.ROLE_SESSION_FLAG
REQUEST_HEADER, RESULT_HEADER, SESSION_BYTES = 6, 5, 4

REJECTED, COMPLETED, ACCEPTED = (reg.RESOLUTIONS[k] for k in ("rejected", "completed", "accepted"))
SUCCESS, FAILED, PARTIAL = (reg.OUTCOMES[k] for k in ("success", "failed", "partial"))

_R = reg.REJECT_REASONS
UNKNOWN_FUNCTION = _R["unknown_function"]
UNKNOWN_OPERATION = _R["unknown_operation"]
MALFORMED = _R["malformed"]
UNAVAILABLE = _R["unavailable"]
BUSY = _R["busy"]                          # a long operation is running: answered at once
WINDOW_EXCEEDED = _R["window_exceeded"]
NO_SESSION = _R["no_session"]              # lock free, but this is not the last session id: open again
LOCKED = _R["locked"]                      # another session holds the lock; payload = remaining ms (u32)
SESSION_REQUIRED = _R["session_required"]  # a state-changing request came without a session id
NO_CONNECTION = _R["no_connection"]        # the probe does not know the request's connection: attach again
UNSUPPORTED = _R["unsupported"]            # a critical TLV (payload: its tag) or a fixed-part value it cannot handle

REJECT_NAMES = {v: k.replace("_", " ") for k, v in _R.items()}
REJECT_NAMES[MALFORMED] = "malformed payload"

TAG_CRITICAL, TAG_IGNORED, TAG_INVALID = reg.TAG_CRITICAL, reg.TAG_IGNORED, reg.TAG_INVALID

# core (fn 0) operations
CORE_FN = 0
_OP = reg.CORE.op
OP_CONFIRM, OP_LIST, OP_DESCRIBE = _OP["confirm"], _OP["list"], _OP["describe"]
OP_PLAN_APPLY, OP_PLAN_RELEASE = _OP["plan_apply"], _OP["plan_release"]
OP_OPEN, OP_END, OP_KEEPALIVE, OP_LOCK_STATE = _OP["open"], _OP["end"], _OP["keepalive"], _OP["lock_state"]
OP_STATUS, OP_CANCEL = _OP["status"], _OP["cancel"]
OP_SUBSCRIBE, OP_UNSUBSCRIBE = _OP["subscribe"], _OP["unsubscribe"]
OP_LINK_SOURCE, OP_LINK_SINK = _OP["link_source"], _OP["link_sink"]

CONFIRM_REQUEST = reg.CONFIRM_REQUEST_MAGIC.encode()
CONFIRM_RESULT = reg.CONFIRM_RESULT_MAGIC.encode()


class OepError(Exception):
    """Anything the probe or the link said no to."""


class ProtocolError(OepError, ValueError):
    """A result that does not fit: wrong correlation, too short, a value this host does not know."""


class ShortPayload(ProtocolError):
    """A payload shorter than its fixed part, or a truncated TLV (a broken result, §0)."""


@dataclass(frozen=True)
class Request:
    corr: int
    fn: int
    op: int
    payload: bytes = b""
    session: int | None = None

    def pack(self) -> bytes:
        if self.session is None:
            return struct.pack("<BHHB", ROLE_REQUEST, self.corr, self.fn, self.op) + self.payload
        return (struct.pack("<BHHBI", ROLE_REQUEST | ROLE_SESSION, self.corr, self.fn, self.op, self.session)
                + self.payload)

    @classmethod
    def unpack(cls, data: bytes) -> Request:
        if len(data) < REQUEST_HEADER:
            raise ValueError("request shorter than its header")
        role, corr, fn, op = struct.unpack_from("<BHHB", data)
        if role & ~ROLE_SESSION != ROLE_REQUEST:
            raise ValueError(f"not a request: role 0x{role:02x}")
        if role & ROLE_SESSION:
            if len(data) < REQUEST_HEADER + SESSION_BYTES:
                raise ValueError("session flag set but no session id")
            (session,) = struct.unpack_from("<I", data, REQUEST_HEADER)
            return cls(corr, fn, op, data[REQUEST_HEADER + SESSION_BYTES:], session)
        return cls(corr, fn, op, data[REQUEST_HEADER:], None)


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
    """One TLV; `critical` sets tag bit 7 (a request argument the probe must honour or refuse)."""
    if len(value) > 255:
        raise ValueError(f"TLV 0x{tag:02x}: value of {len(value)} bytes does not fit")
    if tag & 0x7F == TAG_IGNORED:
        raise ValueError("tag 0x7F is reserved for the ignored list")
    return bytes((tag | (TAG_CRITICAL if critical else 0), len(value))) + value


def split_tlvs(data: bytes) -> list[tuple[int, bytes]]:
    """TLVs in order. A truncated TLV raises ShortPayload (the result is broken)."""
    pos, out = 0, []
    while pos < len(data):
        if pos + 2 > len(data):
            raise ShortPayload("TLV: truncated header")
        tag, n = data[pos], data[pos + 1]
        if pos + 2 + n > len(data):
            raise ShortPayload(f"TLV 0x{tag:02x}: truncated value")
        out.append((tag, data[pos + 2:pos + 2 + n]))
        pos += 2 + n
    return out


@dataclass
class Tail:
    """The TLVs after a result's known part: `tlvs` in order (unknown tags included, for whoever knows them), and
    `ignored`, the non-critical request tags the probe said it ignored (TLV 0x7F)."""
    tlvs: list[tuple[int, bytes]] = field(default_factory=list)
    ignored: list[int] = field(default_factory=list)

    def get(self, tag: int) -> bytes | None:
        return next((v for t, v in self.tlvs if t == tag), None)

    @classmethod
    def parse(cls, data: bytes) -> Tail:
        t = cls()
        for tag, value in split_tlvs(data):
            if tag == TAG_IGNORED:
                t.ignored += list(value)
            else:
                t.tlvs.append((tag, value))
        return t


class Reader:
    """Reads a result payload's fixed part front to back; too short -> ShortPayload. `tail()` then reads the rest as
    §0 TLVs; `rest()` takes the rest as bytes (a result ending with a list of unknown length: nothing may follow)."""

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

    def bytes(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise ShortPayload(f"payload of {len(self.data)} bytes: needs {self.at + n}")
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def words(self, n: int) -> list[int]:
        return list(struct.unpack(f"<{n}I", self.bytes(4 * n)))

    def rest(self) -> bytes:
        out = self.data[self.at:]
        self.at = len(self.data)
        return out

    def tail(self) -> Tail:
        return Tail.parse(self.rest())


def serial_diff(a: int, b: int, bits: int = 32) -> int:
    """a - b for values that wrap (positions u32, seq u16, µs u32): the difference as a signed number of `bits` (§0)."""
    d = (a - b) & ((1 << bits) - 1)
    return d - (1 << bits) if d >> (bits - 1) else d
