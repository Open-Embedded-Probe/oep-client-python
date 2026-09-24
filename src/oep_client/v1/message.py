"""Draft v1 messages: the v0 request/result headers plus the session flag (oep-spec docs/v1-core-wire-delta.ja.md).

  request : role(0x01) corr(u16) fn(u16) op(u8) payload
            role(0x81) corr(u16) fn(u16) op(u8) session_id(u32) payload     role bit 7 = session_id present
  result  : role(0x02) corr(u16) resolution(u8) detail(u8) payload
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

ROLE_REQUEST, ROLE_RESULT, ROLE_SESSION = 0x01, 0x02, 0x80
REQUEST_HEADER, RESULT_HEADER, SESSION_BYTES = 6, 5, 4

REJECTED, COMPLETED, ACCEPTED = 0x00, 0x01, 0x02
SUCCESS, FAILED, PARTIAL = 0, 1, 2

UNKNOWN_FUNCTION = 0x01
UNKNOWN_OPERATION = 0x02
MALFORMED = 0x03
UNAVAILABLE = 0x04
BUSY = 0x05                  # a long operation is running: answered at once
WINDOW_EXCEEDED = 0x06
NO_SESSION = 0x07            # lock free, but this is not the last session id: open again
LOCKED = 0x08                # another session holds the lock; payload = remaining ms (u32)
SESSION_REQUIRED = 0x09      # a state-changing request came without a session id

REJECT_NAMES = {UNKNOWN_FUNCTION: "unknown function", UNKNOWN_OPERATION: "unknown operation",
                MALFORMED: "malformed payload", UNAVAILABLE: "unavailable", BUSY: "busy",
                WINDOW_EXCEEDED: "window exceeded", NO_SESSION: "no session", LOCKED: "locked",
                SESSION_REQUIRED: "session required"}

# core (fn 0) operations
CORE_FN = 0
OP_CONFIRM, OP_LIST, OP_DESCRIBE = 0x01, 0x02, 0x03
OP_OPEN, OP_END, OP_KEEPALIVE, OP_LOCK_STATE = 0x10, 0x11, 0x12, 0x13
OP_STATUS, OP_CANCEL = 0x20, 0x21


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
        role, corr, resolution, detail = struct.unpack_from("<BHBB", data)
        if role != ROLE_RESULT:
            raise ValueError(f"not a result: role 0x{role:02x}")
        return cls(corr, resolution, detail, data[RESULT_HEADER:])

    @property
    def succeeded(self) -> bool:
        return self.resolution == COMPLETED and self.detail == SUCCESS

    def describe(self) -> str:
        if self.resolution == REJECTED:
            return f"rejected: {REJECT_NAMES.get(self.detail, f'0x{self.detail:02x}')}"
        if self.resolution == ACCEPTED:
            return "accepted"
        return {SUCCESS: "completed", FAILED: "failed", PARTIAL: "partial"}.get(self.detail, f"outcome {self.detail}")
