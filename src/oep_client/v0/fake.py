"""In-process fake endpoint implementing the v0 core, for client unit tests without hardware."""

from __future__ import annotations

import struct

from . import codec


class FakeStream:
    """pyserial-like loopback: writes are framed requests, reads return framed results."""

    def __init__(self, limits=(1024, 4096, 8), functions=()):
        self.max_frame, self.window_bytes, self.max_inflight = limits
        self.functions = list(functions)  # (owner, id, revision, handler)
        self._rx = bytearray()
        self._out = bytearray()
        self.timeout = 0.05
        self.requests = 0

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def write(self, data: bytes) -> int:
        self._rx += data
        while len(self._rx) >= 2:
            length = struct.unpack_from("<H", self._rx)[0]
            if len(self._rx) < 2 + length:
                break
            message = bytes(self._rx[2:2 + length])
            del self._rx[:2 + length]
            self._handle(message)
        return len(data)

    def read(self, n: int) -> bytes:
        data = bytes(self._out[:n])
        del self._out[:n]
        return data

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def _respond(self, correlation: int, resolution: int, detail: int, payload: bytes = b"") -> None:
        message = codec.ResultHeader(correlation=correlation, resolution=resolution, detail=detail).pack() + payload
        self._out += struct.pack("<H", len(message)) + message

    def _handle(self, message: bytes) -> None:
        if codec.message_role(message) != codec.ROLE_REQUEST:
            return
        header = codec.RequestHeader.unpack(message)
        payload = message[codec.RequestHeader.HEADER_LENGTH:]
        self.requests += 1
        reject = lambda reason: self._respond(header.correlation, codec.RESOLUTION_REJECTED, reason)
        ok = lambda data=b"": self._respond(header.correlation, codec.RESOLUTION_COMPLETED, codec.OUTCOME_SUCCESS, data)
        if header.function == codec.DEF_CORE_FUNCTION:
            op = header.operation
            if op == codec.CORE_OP_CONFIRM:
                req = codec.CoreConfirmRequest.unpack(payload)
                if req.magic != codec.CONST_CONFIRM_REQUEST_MAGIC:
                    return reject(codec.REJECT_UNAVAILABLE)
                return ok(codec.CoreConfirmResult(magic=codec.CONST_CONFIRM_RESULT_MAGIC, revision=codec.PROTOCOL_REVISION,
                                                  max_frame=self.max_frame, window_bytes=self.window_bytes,
                                                  max_inflight=self.max_inflight, flags=0).pack())
            if op == codec.CORE_OP_LIST:
                req = codec.CoreListRequest.unpack(payload)
                entries = [codec.OfferedFunction(0, 0, 0, 0, 0)] + [
                    codec.OfferedFunction(i + 1, f[0], f[1], f[2], 0) for i, f in enumerate(self.functions)]
                page = entries[req.first:req.first + 16]
                return ok(codec.CoreListResult(total=len(entries), entries=page).pack())
            if op == codec.CORE_OP_PING:
                return ok(codec.CorePingResult(data=codec.CorePingRequest.unpack(payload).data).pack())
            if op == codec.CORE_OP_DESCRIBE:
                return ok(codec.CoreDescribeResult(tlv=b"").pack())
            if op in (codec.CORE_OP_PLAN_APPLY, codec.CORE_OP_PLAN_RELEASE, codec.CORE_OP_STOP):
                return reject(codec.REJECT_UNAVAILABLE)
            return reject(codec.REJECT_UNKNOWN_OPERATION)
        if header.function == 0 or header.function > len(self.functions):
            return reject(codec.REJECT_UNKNOWN_FUNCTION)
        handler = self.functions[header.function - 1][3]
        result = handler(header.operation, payload)
        if isinstance(result, int):
            return reject(result)
        return ok(result)
