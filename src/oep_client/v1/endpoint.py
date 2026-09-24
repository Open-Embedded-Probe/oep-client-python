"""A fake probe endpoint with the draft v1 session rules, answering whole messages (no hardware).

It wraps a `fake.FakeProbe` (which answers confirm / list / describe) and adds what
oep-spec docs/session-and-exclusivity.ja.md and docs/v1-core-wire-delta.ja.md define:

- a lock held by a host-chosen session id, extended by every request of its holder and counted from when
  that request completed (watchdog); when it lapses or ends, the last id is remembered and may resume
- rejects: no session, locked (+ remaining ms, never the holder's id), session required, busy
- one long operation at a time: accepted + an activity number, polled with core status; it keeps running
  without a host, and its last result lives until a new session id takes the lock

Every non-core fn gets three stand-in operations so the rules can be exercised - FAKE ONLY, they mean
nothing on a real probe:  0x01 write(u32) changes state, 0x02 read -> u32 needs no lock,
0x03 long(ms u32) runs for that many clock milliseconds and completes with the value.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable

from . import fake, message as m

TOY_WRITE, TOY_READ, TOY_LONG = 0x01, 0x02, 0x03
LOCK_FREE_CORE = {m.OP_CONFIRM, m.OP_LIST, m.OP_DESCRIBE, m.OP_LOCK_STATE, m.OP_STATUS}


@dataclass
class Activity:
    ref: int
    started_ms: int
    total_ms: int
    value: int
    cancelled: bool = False

    def done_ms(self, now: int) -> int:
        return min(self.total_ms, now - self.started_ms)

    def finished(self, now: int) -> bool:
        return self.cancelled or self.done_ms(now) >= self.total_ms


class Endpoint:
    def __init__(self, probe: fake.FakeProbe, now_ms: Callable[[], int], boot_id: int = 0x1234ABCD,
                 lease_default_ms: int = 3000, lease_max_ms: int = 60000):
        self.probe = probe
        self.now = now_ms
        self.boot_id = boot_id
        self.lease_default_ms = lease_default_ms
        self.lease_max_ms = lease_max_ms
        self.holder: int | None = None
        self.last: int | None = None
        self.lease_ms = lease_default_ms
        self.expires_ms = 0
        self.values: dict[int, int] = {}
        self.activity: Activity | None = None
        self._next_ref = 1

    # ---- the one entry point: a request message in, a result message out ------------------------
    def handle(self, data: bytes) -> bytes:
        req = m.Request.unpack(data)
        result = self._dispatch(req)
        if req.session is not None and req.session == self.holder:
            self.expires_ms = self.now() + self.lease_ms          # watchdog, counted from completion
        return m.Result(req.corr, *result).pack()

    def _dispatch(self, req: m.Request) -> tuple[int, int, bytes]:
        self._lapse()
        if req.fn == m.CORE_FN:
            if req.op in LOCK_FREE_CORE:
                return self._core_lock_free(req)
            if req.op == m.OP_OPEN:
                return self._open(req.payload)
        elif req.op == TOY_READ:
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self.values.get(req.fn, 0))
        refused = self._check(req.session)
        if refused:
            return refused
        if req.fn == m.CORE_FN:
            if req.op == m.OP_END:
                self.holder = None
                return m.COMPLETED, m.SUCCESS, b""
            if req.op == m.OP_KEEPALIVE:
                return m.COMPLETED, m.SUCCESS, b""
            if req.op == m.OP_CANCEL:
                return self._cancel(req.payload)
            return m.REJECTED, m.UNKNOWN_OPERATION, b""
        if self.activity and not self.activity.finished(self.now()):
            return m.REJECTED, m.BUSY, b""
        if req.op == TOY_WRITE:
            self.values[req.fn] = struct.unpack("<I", req.payload)[0]
            return m.COMPLETED, m.SUCCESS, b""
        if req.op == TOY_LONG:
            (total,) = struct.unpack("<I", req.payload)
            self.activity = Activity(self._next_ref, self.now(), total, self.values.get(req.fn, 0))
            self._next_ref = self._next_ref % 0xFFFF + 1
            return m.ACCEPTED, 0, struct.pack("<H", self.activity.ref)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- the lock -------------------------------------------------------------------------------
    def _lapse(self) -> None:
        if self.holder is not None and self.now() >= self.expires_ms:
            self.holder = None                                     # the lock goes, the last id stays

    def _remaining(self) -> int:
        return max(0, self.expires_ms - self.now())

    def _check(self, session: int | None) -> tuple[int, int, bytes] | None:
        if session is None:
            return m.REJECTED, m.SESSION_REQUIRED, b""
        if self.holder is None:
            if session == self.last:
                self.holder = session                              # resume: nobody else came in between
                return None
            return m.REJECTED, m.NO_SESSION, b""
        if session == self.holder:
            return None
        return m.REJECTED, m.LOCKED, struct.pack("<I", self._remaining())

    def _open(self, payload: bytes) -> tuple[int, int, bytes]:
        session, lease, force = struct.unpack("<IIB", payload)
        if self.holder is not None and self.holder != session and not force:
            return m.REJECTED, m.LOCKED, struct.pack("<I", self._remaining())
        resumed = session in (self.holder, self.last)
        if session != self.last:
            self._forget_result()                                  # a new session: the last result goes
        self.holder = self.last = session
        self.lease_ms = min(lease or self.lease_default_ms, self.lease_max_ms)
        self.expires_ms = self.now() + self.lease_ms
        return m.COMPLETED, m.SUCCESS, struct.pack("<IIB", self.lease_ms, self.boot_id, int(resumed))

    def _forget_result(self) -> None:
        if self.activity and self.activity.finished(self.now()):
            self.activity = None

    # ---- lock-free core -------------------------------------------------------------------------
    def _core_lock_free(self, req: m.Request) -> tuple[int, int, bytes]:
        if req.op == m.OP_LOCK_STATE:
            return m.COMPLETED, m.SUCCESS, struct.pack("<BI", int(self.holder is not None), self._remaining())
        if req.op == m.OP_STATUS:
            return self._status(req.payload)
        try:
            return m.COMPLETED, m.SUCCESS, self.probe.call(m.CORE_FN, req.op, req.payload)
        except ValueError:
            return m.REJECTED, m.MALFORMED, b""

    def _status(self, payload: bytes) -> tuple[int, int, bytes]:
        (ref,) = struct.unpack("<H", payload)
        a = self.activity
        if a is None or a.ref != ref:
            return m.REJECTED, m.UNAVAILABLE, b""
        now = self.now()
        if a.cancelled:
            return m.COMPLETED, m.FAILED, b""
        if a.finished(now):
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", a.value)
        return m.ACCEPTED, 0, struct.pack("<II", a.done_ms(now), a.total_ms)

    def _cancel(self, payload: bytes) -> tuple[int, int, bytes]:
        (ref,) = struct.unpack("<H", payload)
        a = self.activity
        if a is None or a.ref != ref or a.finished(self.now()):
            return m.REJECTED, m.UNAVAILABLE, b""
        a.cancelled = True
        return m.COMPLETED, m.SUCCESS, b""
