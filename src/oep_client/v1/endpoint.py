"""A fake probe endpoint that speaks OEP v1, answering whole messages (no hardware).

It wraps a `fake.FakeProbe` (which answers list / describe) and adds what oep-spec docs/session-and-exclusivity.ja.md
and docs/v1-core-wire-delta.ja.md define:

- confirm with a revision range (§5); `revision=0` makes a v0 probe that answers in the v0 shape and drops role 0x81
  requests unanswered (§2)
- a lock held by a host-chosen session id, extended by every request of its holder and counted from when
  that request completed (watchdog); when it lapses or ends, the last id is remembered and may resume
- rejects: no session, locked (+ remaining ms, never the holder's id), session required, busy, no connection,
  unsupported (+ the critical tag), malformed (short fixed part, tag 0xFF)
- §0 tails: a request's TLVs after its fixed part - unknown critical -> rejected unsupported, unknown non-critical ->
  listed in the result's ignored TLV (0x7F); `tail=` appends TLVs to every result that may carry them, so hosts can
  be checked to skip what they do not know
- one long operation at a time: accepted + an activity number, polled with core status
- the plan (plan_apply / plan_release), and simulations of the revision 1 interfaces the profiles offer:
  oep.wire.rvswd / swio (attach, existing connection, max_speed), oep.target.riscv-dm on a `FakeTarget`,
  oep.target.console streams, oep.fixture.gpio and oep.fixture.uart

Every other non-core fn gets three stand-in operations so the session rules can be exercised - FAKE ONLY, they mean
nothing on a real probe:  0x01 write(u32) changes state, 0x02 read -> u32 needs no lock,
0x03 long(ms u32) runs for that many clock milliseconds and completes with the value.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from . import fake, message as m, registry as reg

TOY_WRITE, TOY_READ, TOY_LONG = 0x01, 0x02, 0x03
OK, WAIT, LINE, FAULT, TIMEOUT, STATE = (reg.STATUS[k] for k in ("ok", "wait", "line", "fault", "timeout", "state"))
_RV, _CON, _GPIO, _UART = reg.TARGET_RISCV_DM, reg.TARGET_CONSOLE, reg.FIXTURE_GPIO, reg.FIXTURE_UART
STEP = _RV.enum["dmi_step"]
STEP_ARGS = {STEP["write"]: "BI", STEP["read"]: "B", STEP["poll_reads"]: "BIIH", STEP["wait_us"]: "I",
             STEP["poll_us"]: "BIII"}
MARK = _CON.enum["mark_kind"]


class Reject(Exception):
    def __init__(self, reason: int, payload: bytes = b""):
        self.reason, self.payload = reason, payload


class Take:
    """Reads a request's fixed part; too short -> rejected malformed. `tail(known)` applies the §0 request-tail rule."""

    def __init__(self, payload: bytes):
        self.data, self.at = payload, 0

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        if self.at + size > len(self.data):
            raise Reject(m.MALFORMED)
        v = struct.unpack_from("<" + fmt, self.data, self.at)
        self.at += size
        return v if len(v) > 1 else v[0]

    def bytes(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise Reject(m.MALFORMED)
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def tail(self, known: set[int] = frozenset()) -> tuple[dict[int, bytes], list[int]]:
        """-> (known tags without the critical bit -> value, ignored non-critical tags)."""
        rest, at, got, ignored = self.data[self.at:], 0, {}, []
        self.critical = set()                                      # known tags that came with the critical bit
        while at < len(rest):
            if at + 2 > len(rest) or at + 2 + rest[at + 1] > len(rest):
                raise Reject(m.MALFORMED)
            tag, value = rest[at], rest[at + 2:at + 2 + rest[at + 1]]
            at += 2 + rest[at + 1]
            if tag == m.TAG_INVALID or tag == m.TAG_IGNORED:
                raise Reject(m.MALFORMED)
            if tag & 0x7F in known:
                got[tag & 0x7F] = value
                if tag & m.TAG_CRITICAL:
                    self.critical.add(tag & 0x7F)
            elif tag & m.TAG_CRITICAL:
                raise Reject(m.UNSUPPORTED, bytes([tag]))
            else:
                ignored.append(tag)
        self.at = len(self.data)
        return got, ignored

    def refuse(self, tag: int, got: dict[int, bytes], ignored: list[int]) -> None:
        """A known TLV whose value cannot be honoured (§0): critical -> unsupported with the tag as received, else it
        is dropped and listed as ignored."""
        if tag in self.critical:
            raise Reject(m.UNSUPPORTED, bytes([tag | m.TAG_CRITICAL]))
        got.pop(tag, None)
        ignored.append(tag)


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


@dataclass
class FakeTarget:
    """A RISC-V hart behind a debug module, as far as the riscv-dm operations see it."""
    halted: bool = False
    dpc: int = 0x100
    reset_vector: int = 0
    mem: dict = field(default_factory=dict)            # word address -> value
    dmi: dict = field(default_factory=dict)            # DMI address -> value (what a read returns)
    dmi_reads: dict = field(default_factory=dict)      # DMI address -> list of values the next reads return
    fail_write: set = field(default_factory=set)       # DMI addresses whose write fails on the line
    fault_at: set = field(default_factory=set)         # word addresses a block access faults on
    regs: dict = field(default_factory=dict)           # regno -> value
    havereset: bool = True
    # run(pc, regs) -> (stopped, dpc, elapsed_us); default: halts 0x10 past the start
    run_hook: Callable | None = None

    def dmstatus(self) -> int:
        return 0x82 | ((0x300 if self.halted else 0xC00)) | (0xC0000 if self.havereset else 0)

    def read_dmi(self, address: int) -> int:
        queue = self.dmi_reads.get(address)
        if queue:
            self.dmi[address] = queue.pop(0)
        return self.dmi.get(address, 0)


@dataclass
class Stream:
    """A position stream (console §5.7, fixture.uart §5.8): bytes from position `base`, marks with serials."""
    data: bytearray = field(default_factory=bytearray)
    base: int = 0
    marks: list = field(default_factory=list)          # (serial, position, kind, time_ms, detail)
    serial: int = 0
    closed: bool = False
    written: bytearray = field(default_factory=bytearray)

    @property
    def end(self) -> int:
        return self.base + len(self.data)

    def add_mark(self, kind: int, time_ms: int, detail: int = 0) -> None:
        self.marks.append((self.serial, self.end, kind, time_ms, detail))
        self.serial += 1

    def drop_oldest(self, n: int) -> None:
        del self.data[:n]
        self.base += n


class Endpoint:
    MARKS_PER_ANSWER = 3                     # small, so hosts must follow `more`

    def __init__(self, probe: fake.FakeProbe, now_ms: Callable[[], int], boot_id: int = 0x1234ABCD,
                 lease_default_ms: int = 3000, lease_max_ms: int = 60000, revision: int = 1, tail: bytes = b"",
                 window: int = 1 << 18, max_inflight: int = 4):
        self.probe = probe
        self.now = now_ms
        self.boot_id = boot_id
        self.lease_default_ms = lease_default_ms
        self.lease_max_ms = lease_max_ms
        self.revision = revision
        self.tail = tail
        self.window, self.max_inflight = window, max_inflight
        self.holder: int | None = None
        self.last: int | None = None
        self.lease_ms = lease_default_ms
        self.expires_ms = 0
        self.values: dict[int, int] = {}
        self.activity: Activity | None = None
        self._next_ref = 1
        self.dropped = 0                     # requests a v0 endpoint dropped (role 0x81)
        self.requests: list[m.Request] = []
        self.subscribed: set[int] = set()
        self.plan: set[tuple[int, int, int]] = set()   # (fn, role, channel)
        self.names = {o.fn: o.name for o in probe.offered}
        self.labels = {}
        for o in probe.offered:
            if o.fn == 0:
                for t in o.tlvs:
                    if t[0] == fake.CORE_LABEL:
                        self.labels[t[4:2 + t[1]].decode()] = struct.unpack_from("<H", t, 2)[0]
        self.target = FakeTarget()
        self.connections: dict[int, int] = {}          # connection -> wire fn
        self._next_conn = 1
        self.streams: dict[int, Stream] = {}           # console stream id -> stream
        self.stream_keys: dict[tuple[int, int], int] = {}   # (connection, mechanism) -> stream id
        self._next_stream = 1
        self.console_accept = 64
        self.gpio_modes: dict[int, int] = {}
        self.gpio_inputs: dict[int, int] = {}
        self.gpio_log: list[tuple[int, int]] = []
        self.uarts: dict[int, Stream] = {}             # fn -> stream (configured)
        self.uart_baud: dict[int, tuple[int, int]] = {}
        self.uart_accept = 256

    # ---- the one entry point: a request message in, a result message out ------------------------
    def handle(self, data: bytes) -> bytes | None:
        if self.revision == 0 and data and data[0] & m.ROLE_SESSION:
            self.dropped += 1                                     # a v0 probe: unknown role, no answer
            return None
        req = m.Request.unpack(data)
        self.requests.append(req)
        try:
            res, detail, payload = self._dispatch(req)
        except Reject as r:
            res, detail, payload = m.REJECTED, r.reason, r.payload
        if res != m.REJECTED and not self._closed_tail(req.fn, req.op) and self.revision >= 1:
            payload += self.tail
        if req.session is not None and req.session == self.holder:
            self.expires_ms = self.now() + self.lease_ms          # watchdog, counted from completion
        return m.Result(req.corr, res, detail, payload).pack()

    def _interface(self, fn: int):
        return reg.INTERFACES.get(self.names.get(fn, ""))

    def _closed_tail(self, fn: int, op: int) -> bool:
        i = self._interface(fn)
        return bool(i and op in i.closed_tail)

    def _lock_free(self, fn: int, op: int) -> bool:
        i = self._interface(fn)
        if fn == m.CORE_FN:
            return op in reg.CORE.lock_free
        if i is None or self.names.get(fn) not in SIMS:
            return op == TOY_READ
        return op in i.lock_free

    @staticmethod
    def _answer(payload: bytes, ignored: list[int], detail: int = m.SUCCESS) -> tuple[int, int, bytes]:
        """A completed result; the request's ignored non-critical tags follow in TLV 0x7F (§0)."""
        return m.COMPLETED, detail, payload + (bytes([m.TAG_IGNORED, len(ignored)]) + bytes(ignored) if ignored else b"")

    def _dispatch(self, req: m.Request) -> tuple[int, int, bytes]:
        self._lapse()
        if req.fn == m.CORE_FN and req.op == m.OP_CONFIRM:
            return self._confirm(req.payload)
        if req.fn == m.CORE_FN and req.op == m.OP_OPEN:
            return self._open(req.payload)
        if req.fn != m.CORE_FN and req.fn not in self.names:
            return m.REJECTED, m.UNKNOWN_FUNCTION, b""
        if not self._lock_free(req.fn, req.op):
            refused = self._check(req.session)
            if refused:
                return refused
        if req.fn == m.CORE_FN:
            return self._core(req)
        if self.activity and not self.activity.finished(self.now()) and not self._lock_free(req.fn, req.op):
            return m.REJECTED, m.BUSY, b""
        sim = SIMS.get(self.names[req.fn])
        if sim is not None:
            handler = getattr(self, f"_{sim}", None)
            return handler(req.fn, req.op, Take(req.payload))
        return self._toy(req)

    # ---- the stand-in operations ----------------------------------------------------------------
    def _toy(self, req: m.Request) -> tuple[int, int, bytes]:
        t = Take(req.payload)
        if req.op == TOY_READ:
            _, ignored = t.tail()
            return self._answer(struct.pack("<I", self.values.get(req.fn, 0)), ignored)
        if req.op == TOY_WRITE:
            value = t.take("I")
            _, ignored = t.tail()
            self.values[req.fn] = value
            return self._answer(b"", ignored)
        if req.op == TOY_LONG:
            total = t.take("I")
            t.tail()
            self.activity = Activity(self._next_ref, self.now(), total, self.values.get(req.fn, 0))
            self._next_ref = self._next_ref % 0xFFFF + 1
            return m.ACCEPTED, 0, struct.pack("<H", self.activity.ref)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- the lock -------------------------------------------------------------------------------
    def _lapse(self) -> None:
        if self.holder is not None and self.now() >= self.expires_ms:
            self._release_lock()                                   # the lock goes, the last id stays

    def _release_lock(self) -> None:
        self.holder = None
        self.subscribed.clear()                                    # subscriptions end with the lock

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
        t = Take(payload)
        session, lease, force = t.take("IIB")
        _, ignored = t.tail()
        if self.holder is not None and self.holder != session and not force:
            return m.REJECTED, m.LOCKED, struct.pack("<I", self._remaining())
        resumed = session in (self.holder, self.last)
        if session != self.last:
            self._forget_result()                                  # a new session: the last result goes
        if session != self.holder:
            self.subscribed.clear()
        self.holder = self.last = session
        self.lease_ms = min(lease or self.lease_default_ms, self.lease_max_ms)
        self.expires_ms = self.now() + self.lease_ms
        return self._answer(struct.pack("<IIB", self.lease_ms, self.boot_id, int(resumed)), ignored)

    def _forget_result(self) -> None:
        if self.activity and self.activity.finished(self.now()):
            self.activity = None

    def reboot(self, boot_id: int) -> None:
        """The probe restarts: lock, last id, connections, streams and plan are gone."""
        self.boot_id = boot_id
        self.holder = self.last = None
        self.subscribed.clear()
        self.connections.clear()
        self.streams.clear()
        self.stream_keys.clear()
        self.plan.clear()
        self.uarts.clear()
        self.activity = None

    def lose_connections(self) -> None:
        """A wire or target reset drops every connection; their console streams close with a link-lost mark."""
        for sid, s in self.streams.items():
            if not s.closed:
                s.add_mark(MARK["link_lost"], self.now())
                s.closed = True
        self.connections.clear()

    # ---- core -----------------------------------------------------------------------------------
    def _confirm(self, payload: bytes) -> tuple[int, int, bytes]:
        t = Take(payload)
        magic, lo, hi = t.bytes(4), *t.take("BB")
        if magic != m.CONFIRM_REQUEST:
            return m.REJECTED, m.MALFORMED, b""
        if self.revision == 0:                                     # v0 shape: max_frame(16) window(16) inflight flags
            return m.COMPLETED, m.SUCCESS, struct.pack("<4sBHHBB", m.CONFIRM_RESULT, 0, self.probe.max_frame,
                                                       min(self.window, 0xFFFF), self.max_inflight, 0)
        _, ignored = t.tail()
        if not lo <= self.revision <= hi:
            return m.REJECTED, m.UNSUPPORTED, b""
        return self._answer(struct.pack("<4sBBHIB", m.CONFIRM_RESULT, self.revision, 0, self.probe.max_frame,
                                        self.window, self.max_inflight), ignored)

    def _core(self, req: m.Request) -> tuple[int, int, bytes]:
        op, t = req.op, Take(req.payload)
        if op in (m.OP_LIST, m.OP_DESCRIBE):
            try:
                return m.COMPLETED, m.SUCCESS, self.probe.call(m.CORE_FN, op, req.payload)
            except ValueError:
                return m.REJECTED, m.MALFORMED, b""
        if op == m.OP_LOCK_STATE:
            _, ignored = t.tail()
            return self._answer(struct.pack("<BI", int(self.holder is not None), self._remaining()), ignored)
        if op == m.OP_STATUS:
            return self._status(t)
        if op == m.OP_LINK_SOURCE:
            n = min(t.take("I"), self.probe.max_frame - m.RESULT_HEADER)
            return m.COMPLETED, m.SUCCESS, bytes(k & 0xFF for k in range(n))
        if op == m.OP_LINK_SINK:
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", len(req.payload))
        if op == m.OP_END:
            t.tail()
            self._release_lock()
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_KEEPALIVE:
            _, ignored = t.tail()
            return self._answer(b"", ignored)
        if op == m.OP_CANCEL:
            return self._cancel(t)
        if op == m.OP_SUBSCRIBE:
            fn = t.take("H")
            if t.at < len(t.data):
                t.take("HH")
            t.tail()
            if fn != 0 and fn not in self.names:
                return m.REJECTED, m.UNAVAILABLE, b""
            self.subscribed.add(fn)
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_UNSUBSCRIBE:
            fn = t.take("H")
            t.tail()
            self.subscribed.discard(fn)
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_PLAN_APPLY:
            got = []
            for tag, value in m.split_tlvs(req.payload) if req.payload else []:
                if tag == reg.CORE.tlv["plan_apply"]["role_assignment"] and len(value) >= 5:
                    got.append(struct.unpack_from("<HBH", value))
                elif tag & m.TAG_CRITICAL:
                    return m.REJECTED, m.UNSUPPORTED, bytes([tag])
            for fn, role, ch in got:
                name = self.names.get(fn, "")
                roles = {"oep.fixture.gpio": {1}, "oep.fixture.uart": {1, 2}}.get(name, set())
                if role not in roles:
                    return m.REJECTED, m.UNAVAILABLE, b""
            self.plan |= set(got)
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_PLAN_RELEASE:
            for fn, role, ch in self.plan:
                self.gpio_modes.pop(ch, None)
                self.uarts.pop(fn, None)
            self.plan.clear()
            return m.COMPLETED, m.SUCCESS, b""
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _status(self, t: Take) -> tuple[int, int, bytes]:
        ref = t.take("H")
        a = self.activity
        if a is None or a.ref != ref:
            return m.REJECTED, m.UNAVAILABLE, b""
        now = self.now()
        if a.cancelled:
            return m.COMPLETED, m.FAILED, b""
        if a.finished(now):
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", a.value)
        return m.ACCEPTED, 0, struct.pack("<II", a.done_ms(now), a.total_ms)

    def _cancel(self, t: Take) -> tuple[int, int, bytes]:
        ref = t.take("H")
        a = self.activity
        if a is None or a.ref != ref or a.finished(self.now()):
            return m.REJECTED, m.UNAVAILABLE, b""
        a.cancelled = True
        return m.COMPLETED, m.SUCCESS, b""

    # ---- oep.wire.rvswd / swio ------------------------------------------------------------------
    def _wire(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        tg = self.target
        if op == 0x01:                                             # scan
            t.tail()
            return m.COMPLETED, m.SUCCESS, struct.pack("<BBHHI", 1, 1, 2, 54, tg.dmstatus())
        if op == 0x02:                                             # attach
            method = t.take("B")
            got, ignored = t.tail({0x01})
            if method > 1:
                raise Reject(m.UNSUPPORTED)
            speed = min(4_000_000, struct.unpack("<I", got[0x01])[0]) if 0x01 in got else 4_000_000
            existing = next((c for c, w in self.connections.items() if w == fn), None)
            flags = 0
            if existing is None:
                conn = self._next_conn
                self._next_conn = self._next_conn % 255 + 1
                self.connections[conn] = fn
                if tg.havereset:
                    tg.havereset, flags = False, flags | 1
            else:
                conn, flags = existing, flags | 2
            if method == 1:
                tg.halted = True
            return self._answer(struct.pack("<BIBI", conn, tg.dmstatus(), flags, speed), ignored)
        if op == 0x03:                                             # detach
            conn = t.take("B")
            t.tail()
            if conn not in self.connections:
                raise Reject(m.NO_CONNECTION)
            del self.connections[conn]
            for (c, _), sid in list(self.stream_keys.items()):
                if c == conn and not self.streams[sid].closed:
                    self.streams[sid].add_mark(MARK["detach"], self.now())
                    self.streams[sid].closed = True
            return m.COMPLETED, m.SUCCESS, b""
        if op == 0x04:                                             # attach_under_reset
            channel, _hold = t.take("HH")
            got, ignored = t.tail({0x01})
            if channel not in (0xFFFF, self.labels.get("NRST")):
                raise Reject(m.UNAVAILABLE)
            self.connections = {c: w for c, w in self.connections.items() if w != fn}
            conn = self._next_conn
            self._next_conn = self._next_conn % 255 + 1
            self.connections[conn] = fn
            tg.halted, tg.dpc = True, tg.reset_vector
            return self._answer(struct.pack("<BII", conn, tg.dpc, 4_000_000), ignored)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.target.riscv-dm --------------------------------------------------------------------
    @staticmethod
    def _outcome(status: int, done: int) -> int:
        return m.SUCCESS if status == OK else (m.PARTIAL if done else m.FAILED)

    def _dm(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        conn = t.take("B")
        if conn not in self.connections:
            raise Reject(m.NO_CONNECTION)
        tg = self.target
        if op == _RV.op["dmi"]:
            n = t.take("H")
            steps = []
            for _ in range(n):
                kind = t.take("B")
                if kind not in STEP_ARGS:
                    raise Reject(m.MALFORMED)
                steps.append((kind, t.take(STEP_ARGS[kind])))
            t.tail()
            done, status, values = 0, OK, []
            for kind, args in steps:
                if kind == STEP["write"]:
                    address, value = args
                    if address in tg.fail_write:
                        status = LINE
                        break
                    tg.dmi[address] = value
                elif kind == STEP["read"]:
                    values.append(tg.read_dmi(args))
                elif kind in (STEP["poll_reads"], STEP["poll_us"]):
                    address, mask, want, limit = args
                    tries = limit if kind == STEP["poll_reads"] else max(1, limit // 100)
                    for _ in range(max(1, tries)):
                        v = tg.read_dmi(address)
                        if v & mask == want:
                            break
                    values.append(v)
                    if v & mask != want:
                        status = TIMEOUT
                        break
                done += 1
            return m.COMPLETED, self._outcome(status, done), struct.pack(f"<HB{len(values)}I", done, status, *values)
        if op == _RV.op["halt"]:
            t.tail()
            tg.halted = True
            return m.COMPLETED, m.SUCCESS, bytes([OK])
        if op == _RV.op["resume"]:
            t.tail()
            if tg.halted:
                tg.halted, tg.dpc = False, tg.dpc + 0x40
            return m.COMPLETED, m.SUCCESS, bytes([OK])
        if op == _RV.op["reset"]:
            mode = t.take("B")
            got, ignored = t.tail({_RV.tlv["reset"]["method"]})
            if mode > 2:
                raise Reject(m.UNSUPPORTED)
            method = got.get(_RV.tlv["reset"]["method"])
            if method is not None and (len(method) != 1 or method[0] > 2):
                t.refuse(_RV.tlv["reset"]["method"], got, ignored)
            tg.havereset = True
            tg.halted = mode == 2
            tg.dpc = tg.reset_vector if mode == 2 else tg.reset_vector + 0x200
            return self._answer(struct.pack("<BBBI", OK, 2 if mode == 1 else 0, 1, tg.dpc), ignored)
        if op == _RV.op["step"]:
            t.tail()
            if not tg.halted:
                return m.COMPLETED, m.FAILED, struct.pack("<BBII", STATE, 0, tg.dpc, tg.dpc)
            before = tg.dpc
            tg.dpc += 4
            return m.COMPLETED, m.SUCCESS, struct.pack("<BBII", OK, 1, before, tg.dpc)
        if op == _RV.op["read_block"]:
            address, count = t.take("IH")
            t.tail()
            words, status = [], OK
            for i in range(count):
                if address + 4 * i in tg.fault_at:
                    status = FAULT
                    break
                words.append(tg.mem.get(address + 4 * i, 0))
            return (m.COMPLETED, self._outcome(status, len(words)),
                    struct.pack(f"<HB{len(words)}I", len(words), status, *words))
        if op == _RV.op["write_block"]:
            address, count = t.take("IH")
            words = struct.unpack(f"<{count}I", t.bytes(4 * count))
            t.tail()
            done, status = 0, OK
            for i, w in enumerate(words):
                if address + 4 * i in tg.fault_at:
                    status = FAULT
                    break
                tg.mem[address + 4 * i] = w
                done += 1
            return m.COMPLETED, self._outcome(status, done), struct.pack("<HB", done, status)
        if op == _RV.op["run"]:
            pc, timeout_ms, n = t.take("IIB")
            regs = dict(t.take("HI") for _ in range(n))
            n_out = t.take("B")
            outs = [t.take("H") for _ in range(n_out)]
            t.tail()
            if not tg.halted:
                return m.COMPLETED, m.FAILED, struct.pack(f"<BBII{n_out}I", STATE, 0, tg.dpc, 0, *[0] * n_out)
            tg.regs.update(regs)
            stopped, dpc, us = tg.run_hook(pc, tg.regs) if tg.run_hook else (True, pc + 0x10, 50)
            tg.dpc = dpc
            status = OK if stopped else TIMEOUT
            values = [tg.regs.get(r, 0) for r in outs]
            return (m.COMPLETED, m.SUCCESS if stopped else m.FAILED,
                    struct.pack(f"<BBII{n_out}I", status, int(stopped), dpc, us, *values))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- position streams (console, fixture.uart) -----------------------------------------------
    def _stream_op(self, s: Stream, op: int, t: Take, accept: int) -> tuple[int, int, bytes]:
        if op == _CON.op["read"]:
            frm, arg, mx = t.take("BIH")
            t.tail()
            if frm == 0:
                pos = arg
            elif frm == 1:
                pos = s.base
            elif frm == 2:
                pos = s.end
            elif frm == 3:
                hits = [mk for mk in s.marks if arg == 0 or mk[2] == arg]
                pos = hits[-1][1] if hits else s.base
            else:
                raise Reject(m.UNSUPPORTED)
            flags = 0
            if m.serial_diff(pos, s.base) < 0:
                pos, flags = s.base, 2
            data = bytes(s.data[pos - s.base:pos - s.base + mx])
            if pos + len(data) < s.end:
                flags |= 1
            return m.COMPLETED, m.SUCCESS, struct.pack("<IB", pos, flags) + data
        if op == _CON.op["marks"]:
            frm = t.take("I")
            _, ignored = t.tail()
            hits = [mk for mk in s.marks if m.serial_diff(mk[0], frm) >= 0]
            page = hits[:self.MARKS_PER_ANSWER]
            body = struct.pack("<BB", int(len(hits) > len(page)), len(page))
            body += b"".join(struct.pack("<IIBIB", *mk) for mk in page)
            return self._answer(body, ignored)
        if s.closed:
            raise Reject(m.UNAVAILABLE)
        if op == _CON.op["clear"]:
            t.tail()
            s.drop_oldest(len(s.data))
            s.add_mark(MARK["clear"], self.now())
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["mark"]:
            value = t.take("B")
            t.tail()
            s.add_mark(MARK["host"], self.now(), value)
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["write"]:
            count = t.take("H")
            data = t.bytes(count)
            _, ignored = t.tail()
            took = min(count, accept)
            s.written += data[:took]
            return self._answer(struct.pack("<H", took), ignored, m.SUCCESS if took == count else m.PARTIAL)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.target.console ---------------------------------------------------------------------
    def _console(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _CON.op["open"]:
            conn, mech = t.take("BB")
            _, ignored = t.tail()
            if conn not in self.connections:
                raise Reject(m.NO_CONNECTION)
            if mech > 2:
                raise Reject(m.UNSUPPORTED)
            sid = self.stream_keys.get((conn, mech))
            if sid is not None and not self.streams[sid].closed:
                return self._answer(struct.pack("<BB", sid, 1), ignored)
            for key in [k for k in self.stream_keys if k[1] == mech]:    # a closed one of this mechanism goes
                self.streams.pop(self.stream_keys.pop(key), None)
            sid = self._next_stream
            self._next_stream = self._next_stream % 255 + 1
            self.streams[sid], self.stream_keys[(conn, mech)] = Stream(), sid
            self.streams[sid].add_mark(MARK["attach"], self.now())
            return self._answer(struct.pack("<BB", sid, 0), ignored)
        sid = t.take("B")
        s = self.streams.get(sid)
        if s is None:
            raise Reject(m.UNAVAILABLE)
        if op == _CON.op["close"]:
            t.tail()
            s.closed = True
            return m.COMPLETED, m.SUCCESS, b""
        return self._stream_op(s, op, t, self.console_accept)

    def emit(self, sid: int, data: bytes) -> None:
        """The target writes to its console stream `sid`."""
        self.streams[sid].data += data

    # ---- oep.fixture.gpio -----------------------------------------------------------------------
    def _gpio(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        mine = {ch for f, role, ch in self.plan if f == fn and role == 1}
        if op == _GPIO.op["set"]:
            n = t.take("B")
            pairs = [t.take("HB") for _ in range(n)]
            t.tail()
            for i, (ch, mode) in enumerate(pairs):
                if ch not in mine or mode > 6:
                    raise Reject(m.UNAVAILABLE, bytes([i]))
            for ch, mode in pairs:
                self.gpio_modes[ch] = mode
                self.gpio_log.append((ch, mode))
            return m.COMPLETED, m.SUCCESS, b""
        if op == _GPIO.op["read"]:
            n = t.take("B")
            chans = [t.take("H") for _ in range(n)]
            _, ignored = t.tail()
            for i, ch in enumerate(chans):
                if ch not in mine:
                    raise Reject(m.UNAVAILABLE, bytes([i]))
            levels = []
            for ch in chans:
                mode = self.gpio_modes.get(ch, 0)
                levels.append({1: 1, 3: 0, 4: 1, 5: 0, 6: 1}.get(mode, self.gpio_inputs.get(ch, 0)))
            return self._answer(bytes(levels), ignored)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.fixture.uart -----------------------------------------------------------------------
    def _uart(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _UART.op["configure"]:
            baud = t.take("I")
            got, ignored = t.tail({_UART.tlv["configure"]["format"]})
            if not any(f == fn for f, _, _ in self.plan) or baud == 0:
                raise Reject(m.UNAVAILABLE)
            fmt = got.get(_UART.tlv["configure"]["format"], b"\0")
            if len(fmt) != 1 or fmt[0] & ~0x1F or fmt[0] & 3 > 1 or (fmt[0] >> 2) & 3 > 2:
                t.refuse(_UART.tlv["configure"]["format"], got, ignored)
                fmt = bytes(1)
            actual = 80_000_000 // (80_000_000 // baud)
            self.uart_baud[fn] = (actual, fmt[0])
            self.uarts.setdefault(fn, Stream())
            return self._answer(struct.pack("<I", actual), ignored)
        s = self.uarts.get(fn)
        if s is None:
            raise Reject(m.UNAVAILABLE)
        return self._stream_op(s, op, t, self.uart_accept)

    def uart_rx(self, fn: int, data: bytes) -> None:
        """Bytes arrive on fixture UART `fn`'s RX."""
        self.uarts[fn].data += data


SIMS = {"oep.wire.rvswd": "wire", "oep.wire.swio": "wire", "oep.target.riscv-dm": "dm",
        "oep.target.console": "console", "oep.fixture.gpio": "gpio", "oep.fixture.uart": "uart"}
