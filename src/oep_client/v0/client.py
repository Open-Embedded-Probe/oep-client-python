"""OEP v0 client core: confirmation, offered functions, windowed pipelined requests."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from . import codec
from .transport import FrameTransport


class RequestError(Exception):
    pass


@dataclass(frozen=True)
class Confirmation:
    revision: int
    max_frame: int
    window_bytes: int
    max_inflight: int
    flags: int


@dataclass(frozen=True)
class OfferedFunction:
    function: int
    owner: int
    id: int
    revision: int
    flags: int

    @property
    def definition(self) -> tuple[int, int]:
        return (self.owner, self.id)


@dataclass(frozen=True)
class Response:
    correlation: int
    resolution: int
    detail: int
    payload: bytes

    @property
    def rejected(self) -> bool:
        return self.resolution == codec.RESOLUTION_REJECTED

    @property
    def completed(self) -> bool:
        return self.resolution == codec.RESOLUTION_COMPLETED

    @property
    def succeeded(self) -> bool:
        return self.completed and self.detail == codec.OUTCOME_SUCCESS

    def expect_success(self, what: str = "request") -> bytes:
        if self.rejected:
            raise RequestError(f"{what} rejected: reason 0x{self.detail:02x}")
        if not self.succeeded:
            raise RequestError(f"{what} failed: outcome {self.detail}")
        return self.payload


class Client:
    """Requests go out in order and results come back in order (v0). `call` is the
    one-at-a-time path; `pipeline` keeps up to the declared window in flight."""

    def __init__(self, transport: FrameTransport, timeout: float = 2.0):
        self.transport = transport
        self.timeout = timeout
        self._next_correlation = 1
        self.limits = Confirmation(0, 64, 64, 1, 0)  # before confirmation: minimal profile
        self.functions: list[OfferedFunction] = []

    # ---- low level -------------------------------------------------------
    def _correlation(self) -> int:
        value = self._next_correlation
        self._next_correlation = value % 0xFFFF + 1
        return value

    def _encode(self, function: int, operation: int, payload: bytes) -> tuple[int, bytes]:
        correlation = self._correlation()
        header = codec.RequestHeader(correlation=correlation, function=function, operation=operation).pack()
        message = header + payload
        if len(message) > self.limits.max_frame:
            raise ValueError(f"request {len(message)} bytes exceeds max_frame {self.limits.max_frame}")
        return correlation, message

    def _receive(self, correlation: int) -> Response:
        message = self.transport.recv(self.timeout)
        if message is None:
            raise TimeoutError(f"no result for correlation {correlation}")
        header = codec.ResultHeader.unpack(message)
        if header.correlation != correlation:
            raise RequestError(f"result correlation {header.correlation} does not match {correlation}; framing lost")
        return Response(header.correlation, header.resolution, header.detail, message[codec.ResultHeader.HEADER_LENGTH:])

    def call(self, function: int, operation: int, payload: bytes = b"") -> Response:
        correlation, message = self._encode(function, operation, payload)
        self.transport.send(message)
        return self._receive(correlation)

    def pipeline(self, requests) -> list[Response]:
        """requests: iterable of (function, operation, payload). Keeps outstanding bytes
        within window_bytes and count within max_inflight; results return in order."""
        responses: list[Response] = []
        pending: deque[tuple[int, int]] = deque()  # (correlation, message length)
        outstanding_bytes = 0
        for function, operation, payload in requests:
            correlation, message = self._encode(function, operation, payload)
            while pending and (len(pending) >= self.limits.max_inflight or
                               outstanding_bytes + len(message) + 2 > self.limits.window_bytes):
                corr, size = pending.popleft()
                responses.append(self._receive(corr))
                outstanding_bytes -= size
            self.transport.send(message)
            pending.append((correlation, len(message) + 2))
            outstanding_bytes += len(message) + 2
        while pending:
            corr, _ = pending.popleft()
            responses.append(self._receive(corr))
        return responses

    # ---- core ------------------------------------------------------------
    def confirm(self) -> Confirmation:
        request = codec.CoreConfirmRequest(magic=codec.CONST_CONFIRM_REQUEST_MAGIC,
                                           min_revision=codec.PROTOCOL_REVISION,
                                           max_revision=codec.PROTOCOL_REVISION).pack()
        response = self.call(codec.DEF_CORE_FUNCTION, codec.CORE_OP_CONFIRM, request)
        result = codec.CoreConfirmResult.unpack(response.expect_success("confirm"))
        if result.magic != codec.CONST_CONFIRM_RESULT_MAGIC:
            raise RequestError("confirmation magic mismatch")
        self.limits = Confirmation(result.revision, result.max_frame, result.window_bytes,
                                   result.max_inflight, result.flags)
        self.transport.max_frame = result.max_frame
        return self.limits

    def list_functions(self) -> list[OfferedFunction]:
        functions: list[OfferedFunction] = []
        first = 0
        while True:
            response = self.call(codec.DEF_CORE_FUNCTION, codec.CORE_OP_LIST, codec.CoreListRequest(first=first).pack())
            result = codec.CoreListResult.unpack(response.expect_success("list"))
            functions.extend(OfferedFunction(e.function, e.owner, e.id, e.revision, e.flags) for e in result.entries)
            first += len(result.entries)
            if not result.entries or first >= result.total:
                break
        self.functions = functions
        return functions

    def find(self, owner: int, id: int) -> OfferedFunction | None:
        for function in self.functions or self.list_functions():
            if (function.owner, function.id) == (owner, id):
                return function
        return None

    def describe(self, function: int, first: int = 0) -> bytes:
        response = self.call(codec.DEF_CORE_FUNCTION, codec.CORE_OP_DESCRIBE,
                             codec.CoreDescribeRequest(function=function, first=first).pack())
        return codec.CoreDescribeResult.unpack(response.expect_success("describe")).tlv

    def ping(self, data: bytes = b"") -> bytes:
        response = self.call(codec.DEF_CORE_FUNCTION, codec.CORE_OP_PING, codec.CorePingRequest(data=data).pack())
        return codec.CorePingResult.unpack(response.expect_success("ping")).data
