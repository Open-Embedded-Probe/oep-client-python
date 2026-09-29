"""A fake probe endpoint that speaks OEP v1, answering whole messages (no hardware).

This is the spec side's "working spec": ch32rv, this client and the probe firmware are checked against it. It wraps
a `fake.FakeProbe` (the static declarations) and does what oep-spec docs/oep-core.ja.md and docs/oep-if-*.ja.md
define:

- confirm with a revision range; `revision=0` makes a v0 probe that answers in the v0 shape and drops role 0x81
  requests unanswered
- the lock (core §6): a host-chosen session id, extended by every request of its holder and counted from when that
  request completed; when it lapses or ends the last id is remembered and may resume; lease 0 = the probe default,
  1000-60000 ms taken as asked, longer ones cut to `lease_max_ms`; the open's owner TLV shown by lock_state and by
  rejected locked (never the session id)
- the resend table (core §5.2): the last session's recent requests with their results (results longer than
  `remember_max` bytes are not kept -> result_lost), corr_reused, result_lost for old requests, emptied by open
- rejects: no session, locked (+ remaining ms), session required, no connection, unsupported (+ the critical tag),
  malformed (short fixed part, tag 0xFF)
- request tails: unknown critical -> rejected unsupported, unknown non-critical -> listed in the result's ignored TLV
  (0x7F); `tail=` appends TLVs to every result that may carry them, so hosts can be checked to skip what they do not
  know
- the plan (plan_apply / plan_release; the session's plan goes when the lease lapses), and simulations of the
  interfaces the profiles offer: oep.wire.rvswd / swio (scan, attach on declared pin pairs, several connections up
  to max_connections, the seat rule, connections), oep.target.riscv-dm on one `FakeTarget` per pin pair,
  oep.target.console streams, oep.fixture.gpio, oep.fixture.uart, and oep.probe.config (plan / label / idle / slot /
  bind items, get / set / save / erase, slot_state and bind_state)
- the serial ports' raw side (core §3.4, probe.config §1.2): `port_input` / `port_output` carry the bytes outside
  the frames for each serial port by its bind; a port the lock holder's requests came in on is held until the
  session ends, then resumes from the session's last host reset. The byte framing itself is `fake_serial`.

Every other non-core fn gets two stand-in operations so the session rules can be exercised - FAKE ONLY, they mean
nothing on a real probe:  0x01 write(u32) changes state, 0x02 read -> u32 needs no lock.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from . import catalog, fake, message as m, registry as reg

TOY_WRITE, TOY_READ = 0x01, 0x02
OK, WAIT, LINE, FAULT, TIMEOUT, STATE = (reg.STATUS[k] for k in ("ok", "wait", "line", "fault", "timeout", "state"))
_RV, _CON, _GPIO, _UART, _CFG = (reg.TARGET_RISCV_DM, reg.TARGET_CONSOLE, reg.FIXTURE_GPIO, reg.FIXTURE_UART,
                                 reg.PROBE_CONFIG)
STEP = _RV.enum["dmi_step"]
STEP_ARGS = {STEP["write"]: "BI", STEP["read"]: "B", STEP["poll_reads"]: "BIIH", STEP["wait_us"]: "I",
             STEP["poll_us"]: "BIII"}
MARK = _CON.enum["mark_kind"]
ITEM = _CFG.tlv["item"]
CFG_DESCRIBE = _CFG.tlv["describe"]
SLOT_ATTACH = _CFG.enum["slot_attach"]
SLOT_STATE = _CFG.enum["slot_state"]
BIND_MODE = _CFG.enum["bind_mode"]
BIND_STREAM = _CFG.enum["bind_stream"]
BIND_FLOW = _CFG.enum["bind_flow"]
WIRES = ("oep.wire.rvswd", "oep.wire.swio")
OWNER = reg.CORE.tlv["open"]["owner"]
SLOT_NAME = re.compile(r"[a-z0-9_-]{1,32}")
NO_SLOT, NEVER = 0xFF, 0xFFFFFFFF


class Reject(Exception):
    def __init__(self, reason: int, payload: bytes = b""):
        self.reason, self.payload = reason, payload


class Take:
    """Reads a request's fixed part; too short -> rejected malformed. `tail(known)` applies the request-tail rule."""

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
        """A known TLV whose value cannot be honoured: critical -> unsupported with the tag as received, else it is
        dropped and listed as ignored."""
        if tag in self.critical:
            raise Reject(m.UNSUPPORTED, bytes([tag | m.TAG_CRITICAL]))
        got.pop(tag, None)
        ignored.append(tag)


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
    resume_misses: int = 0                             # resumes that do not take (status state, dpc unchanged)
    present: bool = True                               # something answers on this pin pair (scan, attach)
    target_id: int | None = None                       # the wch_dmi_7f target_id attach reports (None: none)
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
    """A position stream (common §1): bytes from position `base`, marks with serials."""
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


@dataclass
class Connection:
    """A debug connection (common §2): made by a wire's attach, open while anything uses it."""
    fn: int
    pair: tuple[int, int]
    order: int                                         # creation order (the seat rule closes the oldest)
    speed: int = 4_000_000
    tid: int | None = None
    users: set = field(default_factory=set)            # "host" and/or ("slot", n)


@dataclass(frozen=True)
class Slot:
    slot: int
    wire_fn: int
    pair: tuple[int, int]
    attach: int
    retry_s: int
    mechanism: int
    name: str
    lock: tuple[int, bytes, bytes] | None              # (scheme, mask, value)


@dataclass(frozen=True)
class Bind:
    port: int
    mode: int
    selected: int
    streams: tuple[tuple[int, int], ...]               # (kind, id)


@dataclass
class SlotRuntime:
    last_try_ms: int | None = None
    evicted: bool = False                              # the seat rule closed its connection: no retry until a new cue
    mismatch_tid: int | None = None                    # what the last automatic attach saw when the lock did not match
    no_tid: bool = False                               # ... or it saw no target_id to check a lock against


@dataclass
class Flow:
    """One stream as a serial port's bind carries it: the stream id and the port's position in it."""
    sid: object = None
    pos: int = 0
    line: bytearray = field(default_factory=bytearray)
    last_ms: int = 0


class Endpoint:
    MARKS_PER_ANSWER = 3                     # small, so hosts must follow `more`
    CHUNK = 64                               # raw bytes a serial port takes at a time (probe guide §3.6)
    MIXED_LINE_MAX, MIXED_QUIET_MS = 128, 100

    def __init__(self, probe: fake.FakeProbe, now_ms: Callable[[], int], boot_id: int = 0x1234ABCD,
                 lease_default_ms: int = 3000, lease_max_ms: int = 600000, revision: int = 1, tail: bytes = b"",
                 window: int = 1 << 18, max_inflight: int = 4, remember_max: int = 72):
        self.probe = probe
        self.now = now_ms
        self.boot_id = boot_id
        self.lease_default_ms = lease_default_ms
        self.lease_max_ms = lease_max_ms
        self.revision = revision
        self.tail = tail
        self.window, self.max_inflight = window, max_inflight
        self.remember_max = remember_max
        self.names = {o.fn: o.name for o in probe.offered}
        self.fns = {name: fn for fn, name in sorted(self.names.items(), reverse=True)}   # first fn of each name
        self.static = {o.fn: o.tlvs for o in probe.offered}
        self.static_labels: dict[int, str] = {}
        self.transports: list[int] = []
        for t in self.static.get(0, ()):
            if t[0] == fake.CORE_LABEL:
                self.static_labels[struct.unpack_from("<H", t, 2)[0]] = t[4:2 + t[1]].decode()
            if t[0] == fake.CORE_TRANSPORT:
                self.transports.append(t[3])
        self.serial_ports = {i for i, k in enumerate(self.transports) if k in fake.SERIAL_KINDS}
        self.pairs: dict[int, list[tuple[int, int]]] = {}  # wire fn -> allowed (swdio, swclk), declared order
        self.max_connections: dict[int, int] = {}
        for fn, name in self.names.items():
            if name in WIRES:
                self.pairs[fn] = [self._group_pair(name, t) for t in self.static[fn] if t[0] == catalog.CHANNEL_GROUP]
                self.max_connections[fn] = next((t[2] for t in self.static[fn] if t[0] == fake.MAX_CONNECTIONS), 1)
        self.targets: dict[tuple[int, tuple[int, int]], FakeTarget] = {
            (fn, p): FakeTarget() for fn in sorted(self.pairs) for p in self.pairs[fn]}
        self.target = next(iter(self.targets.values()), FakeTarget())
        self.mechanisms = set()
        for fn, name in self.names.items():
            if name == "oep.target.console":
                self.mechanisms |= {b for t in self.static[fn] if t[0] == fake.MECHANISMS for b in t[2:2 + t[1]]}
        self.block_max = {fn: next((struct.unpack_from("<H", t, 2)[0] for t in self.static[fn]
                                    if t[0] == catalog.MAX_LENGTH), 1 << 16)
                          for fn, name in self.names.items() if name == "oep.target.riscv-dm"}
        cfg_fn = self.fns.get("oep.probe.config")
        cfg = {t[0]: t[2:2 + t[1]] for t in self.static.get(cfg_fn, ())}
        self.slots_max = cfg[CFG_DESCRIBE["slots_max"]][0] if CFG_DESCRIBE["slots_max"] in cfg else 0
        self.bind_modes = cfg[CFG_DESCRIBE["bind_modes"]][0] if CFG_DESCRIBE["bind_modes"] in cfg else 0
        self.items = set(cfg.get(CFG_DESCRIBE["items"], b""))
        self.storage_max = struct.unpack_from("<I", cfg[CFG_DESCRIBE["storage"]])[0] if CFG_DESCRIBE["storage"] in cfg else 0
        self.console_accept = 64
        self.uart_accept = 256
        self._boot()

    def _boot(self) -> None:
        self.holder: int | None = None
        self.last: int | None = None
        self.owner: bytes | None = None
        self.lease_ms = self.lease_default_ms
        self.expires_ms = 0
        self.values: dict[int, int] = {}
        self.dropped = 0                     # requests a v0 endpoint dropped (role 0x81)
        self.requests: list[m.Request] = []
        self.subscribed: set[int] = set()
        self.plan: set[tuple[int, int, int]] = set()   # (fn, role, channel), from plan_apply and the config
        self.plan_from_config: set[int] = set()        # fns whose plan came from the config (not a session's)
        self.resend: OrderedDict[int, tuple[int, int, int, bytes | None]] = OrderedDict()
        self.newest_corr: int | None = None
        self.conns: dict[int, Connection] = {}
        self._next_conn = 1
        self._order = 0
        self.streams: dict[int, Stream] = {}           # console stream id -> stream
        self.stream_keys: dict[tuple[int, int], int] = {}   # (connection, mechanism) -> stream id
        self.stream_places: dict[int, tuple[int, tuple[int, int]]] = {}   # stream id -> (wire fn, pin pair) it was on
        self._next_stream = 1
        self.gpio_modes: dict[int, int] = {}
        self.gpio_inputs: dict[int, int] = {}
        self.gpio_log: list[tuple[int, int]] = []
        self.uarts: dict[int, Stream] = {}             # fn -> stream (configured)
        self.uart_baud: dict[int, tuple[int, int]] = {}
        self.uart_tx: dict[int, bytearray] = {}        # what a serial port's raw bytes sent out on a fixture UART
        self.config: dict[tuple[int, int], bytes | list[bytes]] = {}   # (item tag, key) -> value (plan: list)
        self.saved: dict | None = getattr(self, "saved", None)
        self.slots: dict[int, Slot] = {}
        self.binds: dict[int, Bind] = {}
        self.slot_rt: dict[int, SlotRuntime] = {}
        self.selected: dict[int, int] = {}            # port -> selected index (last-reset / manual)
        self.flows: dict[tuple[int, tuple[int, int]], Flow] = {}
        self.mixed_out: dict[int, bytearray] = {}
        self.held_ports: set[int] = set()
        self.session_resets: dict[tuple[int, int], tuple[object, int]] = {}   # stream key -> (sid, position)
        if self.saved is not None:
            self._apply_config(dict(self.saved), boot=True)

    @property
    def target_id(self) -> int | None:
        return self.target.target_id

    @target_id.setter
    def target_id(self, value: int | None) -> None:
        self.target.target_id = value

    @staticmethod
    def _group_pair(name: str, t: bytes) -> tuple[int, int]:
        roles = {t[3 + 3 * i]: struct.unpack_from("<H", t, 4 + 3 * i)[0] for i in range((t[1] - 1) // 3)}
        return roles.get(1, 0xFFFF), roles.get(2, 0xFFFF) if name != "oep.wire.swio" else 0xFFFF

    # ---- the one entry point: a request message in, a result message out ------------------------
    def handle(self, data: bytes, transport: int = 0) -> bytes | None:
        """A request from transport `transport` (the index in the describe's transport list) -> its result."""
        if self.revision == 0 and data and data[0] & m.ROLE_SESSION:
            self.dropped += 1                                     # a v0 probe: unknown role, no answer
            return None
        req = m.Request.unpack(data)
        self.requests.append(req)
        remembered = self._resent(req)
        if remembered is not None:
            return remembered
        try:
            res, detail, payload = self._dispatch(req)
        except Reject as r:
            res, detail, payload = m.REJECTED, r.reason, r.payload
        if res != m.REJECTED and not self._closed_tail(req.fn, req.op) and self.revision >= 1:
            payload += self.tail
        if req.session is not None and req.session == self.holder:
            self.expires_ms = self.now() + self.lease_ms          # watchdog, counted from completion
        took_lock = req.fn == m.CORE_FN and req.op == m.OP_OPEN and res == m.COMPLETED
        if self.holder is not None and transport in self.serial_ports and (
                took_lock or (req.session is not None and req.session == self.holder)):
            self.held_ports.add(transport)                         # core §3.4: the raw transfer holds here
        out = m.Result(req.corr, res, detail, payload).pack()
        if req.session is not None and req.session == self.last:
            self._remember(req, out)
        return out

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
        """A completed result; the request's ignored non-critical tags follow in TLV 0x7F."""
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
        sim = SIMS.get(self.names[req.fn])
        if sim is not None:
            return getattr(self, f"_{sim}")(req.fn, req.op, Take(req.payload))
        return self._toy(req)

    # ---- the resend table (core §5.2) -----------------------------------------------------------
    def _resent(self, req: m.Request) -> bytes | None:
        """A request of the last session seen before: its remembered result, corr_reused or result_lost; None = new."""
        if req.session is None or req.session != self.last:
            return None
        entry = self.resend.get(req.corr)
        if entry is not None:
            fn, op, crc, result = entry
            if (fn, op, crc) != (req.fn, req.op, zlib.crc32(req.payload)):
                return m.Result(req.corr, m.REJECTED, m.CORR_REUSED).pack()
            return result if result is not None else m.Result(req.corr, m.REJECTED, m.RESULT_LOST).pack()
        if self.newest_corr is not None and m.serial_diff(req.corr, self.newest_corr, 16) <= 0:
            return m.Result(req.corr, m.REJECTED, m.RESULT_LOST).pack()
        return None

    def _remember(self, req: m.Request, result: bytes) -> None:
        self.resend[req.corr] = (req.fn, req.op, zlib.crc32(req.payload),
                                 result if len(result) <= self.remember_max else None)
        self.resend.move_to_end(req.corr)
        while len(self.resend) > 2 * max(self.max_inflight, 4):
            self.resend.popitem(last=False)
        if self.newest_corr is None or m.serial_diff(req.corr, self.newest_corr, 16) > 0:
            self.newest_corr = req.corr

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
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- the lock -------------------------------------------------------------------------------
    def _lapse(self) -> None:
        if self.holder is not None and self.now() >= self.expires_ms:
            self._release_lock(taken=True)                         # the lock goes, the last id stays

    def _release_lock(self, taken: bool) -> None:
        """end (taken False) keeps the session's resources for the next open; a lapse or force (taken True) drops
        them (core §9)."""
        self.holder = None
        self.subscribed.clear()                                    # subscriptions end with the lock
        if taken:
            for fn in {a[0] for a in self.plan} - self.plan_from_config:
                self._drop_plan(fn)
            for cid, c in list(self.conns.items()):
                c.users.discard("host")
                if not c.users:
                    self._close_conn(cid, MARK["detach"])
            self._refresh()
        self._session_over()

    def _remaining(self) -> int:
        return max(0, self.expires_ms - self.now()) if self.holder is not None else 0

    def _locked(self) -> tuple[int, int, bytes]:
        owner = m.tlv(reg.CORE.tlv["locked_payload"]["owner"], self.owner) if self.owner else b""
        return m.REJECTED, m.LOCKED, struct.pack("<I", self._remaining()) + owner

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
        return self._locked()

    def _open(self, payload: bytes) -> tuple[int, int, bytes]:
        t = Take(payload)
        session, lease, force = t.take("IIB")
        got, ignored = t.tail({OWNER})
        owner = got.get(OWNER)
        if owner is not None and not 1 <= len(owner) <= 32:
            t.refuse(OWNER, got, ignored)
            owner = None
        if self.holder is not None and self.holder != session:
            if not force:
                return self._locked()
            self._release_lock(taken=True)                         # force: the old session is cleaned up first
        resumed = session in (self.holder, self.last)
        if session != self.holder:
            self.subscribed.clear()
        if session != self.last:
            self.owner = None
        if owner is not None:
            self.owner = owner
        self.holder = self.last = session
        self.resend.clear()
        self.newest_corr = None
        if lease == 0:
            self.lease_ms = self.lease_default_ms
        else:
            self.lease_ms = min(lease, max(self.lease_max_ms, 60000))
        self.expires_ms = self.now() + self.lease_ms
        return self._answer(struct.pack("<IIB", self.lease_ms, self.boot_id, int(resumed)), ignored)

    def reboot(self, boot_id: int) -> None:
        """The probe restarts: lock, last id, connections, streams, the unsaved config and the plan are gone; the
        saved config comes back."""
        self.boot_id = boot_id
        self._boot()

    def lose_connections(self) -> None:
        """A wire drops every connection; their console streams close with a link-lost mark."""
        for cid in list(self.conns):
            self._close_conn(cid, MARK["link_lost"])
        self._refresh()

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
        if op == m.OP_LIST:
            try:
                return m.COMPLETED, m.SUCCESS, self.probe.call(m.CORE_FN, op, req.payload)
            except ValueError:
                return m.REJECTED, m.MALFORMED, b""
        if op == m.OP_DESCRIBE:
            fn, first = t.take("HH")
            if fn not in self.names:
                return m.REJECTED, m.MALFORMED, b""
            return m.COMPLETED, m.SUCCESS, self._page(self._declarations(fn), first)
        if op == m.OP_LOCK_STATE:
            _, ignored = t.tail()
            owner = (m.tlv(reg.CORE.tlv["lock_state_answer"]["owner"], self.owner)
                     if self.owner and self.holder is not None else b"")
            return self._answer(struct.pack("<BI", int(self.holder is not None), self._remaining()) + owner, ignored)
        if op == m.OP_LINK_SOURCE:
            n = min(t.take("I"), self.probe.max_frame - m.RESULT_HEADER)
            return m.COMPLETED, m.SUCCESS, bytes(k & 0xFF for k in range(n))
        if op == m.OP_LINK_SINK:
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", len(req.payload))
        if op == m.OP_END:
            t.tail()
            self._release_lock(taken=False)
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_KEEPALIVE:
            _, ignored = t.tail()
            return self._answer(b"", ignored)
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
            self._check_plan(got)
            named = {fn for fn, _, _ in got}
            self.plan = {a for a in self.plan if a[0] not in named} | set(got)
            self.plan_from_config -= named
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_PLAN_RELEASE:                                 # n(u8) n x fn(u16); n = 0: every fn
            n = t.take("B")
            fns = {t.take("H") for _ in range(n)}
            t.tail()
            for fn in {a[0] for a in self.plan if not fns or a[0] in fns}:
                self._drop_plan(fn)
            return m.COMPLETED, m.SUCCESS, b""
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _check_plan(self, got: list[tuple[int, int, int]]) -> None:
        """plan_apply's all-or-nothing check: the roles each fn has, and no pin another fn (or a slot) holds."""
        named = {fn for fn, _, _ in got}
        kept = {a for a in self.plan if a[0] not in named}
        slot_pins = {p for s in self.slots.values() for p in s.pair if p != 0xFFFF}
        for fn, role, ch in got:
            roles = {"oep.fixture.gpio": {1}, "oep.fixture.uart": {1, 2}}.get(self.names.get(fn, ""), set())
            if role not in roles or any(k[2] == ch for k in kept) or ch in slot_pins:
                raise Reject(m.UNAVAILABLE)

    def _drop_plan(self, fn: int) -> None:
        for a in [a for a in self.plan if a[0] == fn]:
            self.gpio_modes.pop(a[2], None)
            self.plan.discard(a)
        self.uarts.pop(fn, None)
        self.plan_from_config.discard(fn)

    # ---- describe: the static declarations plus the live ones -----------------------------------
    def _declarations(self, fn: int) -> list[bytes]:
        tlvs = list(self.static[fn])
        if fn == m.CORE_FN:
            labels = dict(self.static_labels)
            for (tag, key), value in self.config.items():
                if tag == ITEM["label"]:
                    labels[key] = value[2:].decode("utf-8", "replace")
            tlvs = [t for t in tlvs if t[0] != fake.CORE_LABEL]
            tlvs += [catalog.tlv(fake.CORE_LABEL, struct.pack("<H", ch) + name.encode()) for ch, name in sorted(labels.items())]
        elif self.names[fn] == "oep.probe.config":
            tlvs = [t for t in tlvs if t[0] != CFG_DESCRIBE["storage"]]
            saved_hash = self._hash(self.saved) if self.saved is not None else 0
            tlvs.insert(0, catalog.tlv(CFG_DESCRIBE["storage"], struct.pack(
                "<IBII", self.storage_max, 0 if self.saved is None else 1, saved_hash, 20)))
            tlvs += [catalog.tlv(CFG_DESCRIBE["slot_state"], self._slot_state(n)) for n in sorted(self.slots)]
            tlvs += [catalog.tlv(CFG_DESCRIBE["bind_state"], self._bind_state(p)) for p in sorted(self.binds)]
        return tlvs

    def _page(self, tlvs: list[bytes], first: int) -> bytes:
        budget = self.probe.max_frame - m.RESULT_HEADER - 1
        out, sent = b"", 0
        for t in tlvs[first:]:
            if out and len(out) + len(t) > budget:
                break
            out += t
            sent += 1
        return bytes([1 if first + sent < len(tlvs) else 0]) + out

    # ---- oep.wire.rvswd / swio ------------------------------------------------------------------
    def _wire(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        allowed = self.pairs.get(fn, [])
        if op == 0x01:                                             # scan: count(u8) pairs -> tried count found...
            count = t.take("B")
            pairs = [t.take("HH") for _ in range(count)]
            t.tail()
            if any(p not in allowed for p in pairs):
                raise Reject(m.UNAVAILABLE)
            pairs = pairs or allowed
            found = []
            for p in pairs:
                tg = self.targets[(fn, p)]
                if tg.present:                                     # a live connection's pair: read over it, no restart
                    found.append(struct.pack("<BHHI", 1, *p, tg.dmstatus()))
            return m.COMPLETED, m.SUCCESS, struct.pack("<BB", len(pairs), len(found)) + b"".join(found)
        if op in (0x02, 0x04):                                     # attach, attach_under_reset
            if op == 0x02:
                method = t.take("B")
                if method > 1:
                    raise Reject(m.UNSUPPORTED)
            else:
                channel, _hold = t.take("HH")
                if channel not in (0xFFFF, self._channel_named("NRST")):
                    raise Reject(m.UNAVAILABLE)
            got, ignored = t.tail({0x01, 0x03})
            pair = self._pick_pair(fn, got)
            tg = self.targets[(fn, pair)]
            speed = min(4_000_000, struct.unpack("<I", got[0x01])[0]) if 0x01 in got else 4_000_000
            cid = self._conn_at(fn, pair)
            flags = 0
            if cid is None:
                if not tg.present:
                    return m.COMPLETED, m.FAILED, bytes([LINE])
                cid = self._seat(fn, pair, tg, speed)
                if tg.havereset and op == 0x02:
                    tg.havereset, flags = False, flags | 1
            else:
                flags |= 2
                self.conns[cid].speed = min(self.conns[cid].speed, speed)
            c = self.conns[cid]
            c.users.add("host")
            for n, s in self.slots.items():                        # a new connection for an evicted slot: a new cue
                if s.wire_fn == fn and s.pair == pair:
                    self.slot_rt[n].evicted = False
            tid = b"" if c.tid is None else m.tlv(0x10, bytes([1]) + struct.pack("<I", c.tid))
            if op == 0x04:
                tg.halted, tg.dpc = True, tg.reset_vector
                self._host_reset(cid)
                self._refresh()
                return self._answer(struct.pack("<HII", cid, tg.dpc, c.speed) + tid, ignored)
            if method == 1:
                tg.halted = True
            self._refresh()
            return self._answer(struct.pack("<HIBI", cid, tg.dmstatus(), flags, c.speed) + tid, ignored)
        if op == 0x03:                                             # detach
            cid = t.take("H")
            got, _ = t.tail({0x01})
            c = self.conns.get(cid)
            if c is None or c.fn != fn:
                raise Reject(m.NO_CONNECTION)
            c.users.discard("host")
            if 0x01 in got or not c.users:
                self._close_conn(cid, MARK["detach"])
            self._refresh()
            return m.COMPLETED, m.SUCCESS, b""
        if op == 0x05:                                             # connections (lock-free)
            t.tail()
            mine = sorted((c.order, cid) for cid, c in self.conns.items() if c.fn == fn)
            out = bytearray([len(mine)])
            for _, cid in mine:
                c = self.conns[cid]
                users = (1 if "host" in c.users else 0) | (2 if any(u != "host" for u in c.users) else 0)
                slot = next((n for n, s in self.slots.items() if s.wire_fn == fn and s.pair == c.pair), NO_SLOT)
                tid = b"" if c.tid is None else struct.pack("<I", c.tid)
                out += struct.pack("<HHHIBBBB", cid, *c.pair, c.speed, users, slot, 1 if tid else 0, len(tid)) + tid
            return m.COMPLETED, m.SUCCESS, bytes(out)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _pick_pair(self, fn: int, got: dict[int, bytes]) -> tuple[int, int]:
        allowed = self.pairs.get(fn, [])
        if 0x03 in got:
            if len(got[0x03]) != 4:
                raise Reject(m.MALFORMED)
            pair = struct.unpack("<HH", got[0x03])
            if pair not in allowed:
                raise Reject(m.UNAVAILABLE)                        # a pair this probe does not allow
            return pair
        if len(allowed) != 1:
            raise Reject(m.UNAVAILABLE)                            # the host chooses among several
        return allowed[0]

    def _channel_named(self, name: str) -> int | None:
        labels = dict(self.static_labels)
        for (tag, key), value in self.config.items():
            if tag == ITEM["label"]:
                labels[key] = value[2:].decode("utf-8", "replace")
        return next((ch for ch, n in labels.items() if n == name), None)

    def _conn_at(self, fn: int, pair: tuple[int, int]) -> int | None:
        return next((cid for cid, c in self.conns.items() if c.fn == fn and c.pair == pair), None)

    def _seat(self, fn: int, pair: tuple[int, int], tg: FakeTarget, speed: int, evict: bool = True) -> int:
        """A new connection; a full wire gives up its oldest slot-only connection (debug §1), if `evict`."""
        mine = [(c.order, cid) for cid, c in self.conns.items() if c.fn == fn]
        if len(mine) >= self.max_connections.get(fn, 1):
            slot_only = sorted((o, cid) for o, cid in mine if "host" not in self.conns[cid].users)
            if not evict or not slot_only:
                raise Reject(m.UNAVAILABLE)
            gone = self.conns[slot_only[0][1]]
            for u in gone.users:
                if u != "host":
                    self.slot_rt[u[1]].evicted = True
            self._close_conn(slot_only[0][1], MARK["link_lost"])
        if self._next_conn > 0xFFFF:
            raise Reject(m.UNAVAILABLE)                            # every number used this boot (core §9)
        cid, self._next_conn = self._next_conn, self._next_conn + 1
        self._order += 1
        self.conns[cid] = Connection(fn, pair, self._order, speed, tg.target_id)
        return cid

    def _close_conn(self, cid: int, mark: int) -> None:
        self.conns.pop(cid, None)
        for (c, _), sid in list(self.stream_keys.items()):
            if c == cid and not self.streams[sid].closed:
                self.streams[sid].add_mark(mark, self.now())
                self.streams[sid].closed = True

    def _target_of(self, cid: int) -> FakeTarget:
        c = self.conns[cid]
        return self.targets[(c.fn, c.pair)]

    # ---- oep.target.riscv-dm --------------------------------------------------------------------
    @staticmethod
    def _outcome(status: int, done: int) -> int:
        return m.SUCCESS if status == OK else (m.PARTIAL if done else m.FAILED)

    def _dm(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        conn = t.take("H")
        if conn not in self.conns:
            raise Reject(m.NO_CONNECTION)
        tg = self._target_of(conn)
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
                    if address == 0x17 and value & 0xFFFF == 0x07B1:   # access register dpc: into DATA0, not busy
                        tg.dmi[0x04], tg.dmi[0x16] = tg.dpc, 0
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
            if tg.resume_misses:                                  # a CH32V006 now and then: the request does not take
                tg.resume_misses -= 1
                return m.COMPLETED, m.FAILED, bytes([STATE])
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
            # debug §4.3: bit0 reached the mode's state, bit1 confirmed by the pc (mode 1), attempts 1
            flags, pc = (0b01, 0) if mode == 0 else ((0b11, tg.dpc) if mode == 1 else (0b01, tg.dpc))
            self._host_reset(conn)
            return self._answer(struct.pack("<BBBI", OK, flags, 1, pc), ignored)
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
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise Reject(m.MALFORMED)                          # past the declared max_length (bytes)
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
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise Reject(m.MALFORMED)
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

    def _host_reset(self, cid: int) -> None:
        """A reset last-reset counts (probe.config §1.2): riscv-dm reset or attach_under_reset on connection `cid`."""
        now = self.now()
        slots = [n for n, s in self.slots.items() if (s.wire_fn, s.pair) == (self.conns[cid].fn, self.conns[cid].pair)]
        for (c, _), sid in self.stream_keys.items():
            if c == cid and not self.streams[sid].closed:
                self.streams[sid].add_mark(MARK["reset"], now)
        for port, b in self.binds.items():
            if b.mode == BIND_MODE["last_reset"]:
                for i, key in enumerate(b.streams):
                    if key[0] == BIND_STREAM["slot_console"] and key[1] in slots:
                        self.selected[port] = i
        if self.holder is not None:
            for n in slots:
                key = (BIND_STREAM["slot_console"], n)
                sid, s = self._stream_for(key)
                if s is not None:
                    self.session_resets[key] = (sid, s.end)
            for fn, s in self.uarts.items():
                self.session_resets[(BIND_STREAM["fixture_uart"], fn)] = (("uart", fn), s.end)

    # ---- position streams (console, fixture.uart) -----------------------------------------------
    def _stream_op(self, s: Stream, op: int, t: Take, accept: int) -> tuple[int, int, bytes]:
        if op == _CON.op["read"]:
            frm, arg, mx = t.take("BQH")
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
            if pos < s.base:
                pos, flags = s.base, 2
            data = bytes(s.data[pos - s.base:pos - s.base + mx])
            if pos + len(data) < s.end:
                flags |= 1
            return m.COMPLETED, m.SUCCESS, struct.pack("<QB", pos, flags) + data
        if op == _CON.op["marks"]:
            frm = t.take("I")
            _, ignored = t.tail()
            hits = [mk for mk in s.marks if m.serial_diff(mk[0], frm) >= 0]
            page = hits[:self.MARKS_PER_ANSWER]
            body = struct.pack("<BB", int(len(hits) > len(page)), len(page))
            body += b"".join(struct.pack("<IQBIB", *mk) for mk in page)
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
            conn, mech = t.take("HB")
            _, ignored = t.tail()
            if conn not in self.conns:
                raise Reject(m.NO_CONNECTION)
            if mech not in self.mechanisms:
                raise Reject(m.UNSUPPORTED)
            sid, existing = self._open_stream(conn, mech)
            return self._answer(struct.pack("<HB", sid, int(existing)), ignored)
        sid = t.take("H")
        s = self.streams.get(sid)
        if s is None:
            raise Reject(m.UNAVAILABLE)
        if op == _CON.op["close"]:
            t.tail()
            s.closed = True
            return m.COMPLETED, m.SUCCESS, b""
        return self._stream_op(s, op, t, self.console_accept)

    def _open_stream(self, conn: int, mech: int) -> tuple[int, bool]:
        sid = self.stream_keys.get((conn, mech))
        if sid is not None and not self.streams[sid].closed:
            return sid, True
        place = (self.conns[conn].fn, self.conns[conn].pair)
        for key in [k for k, v in self.stream_keys.items()          # a closed one of this mechanism on this place goes
                    if k[1] == mech and self.streams[v].closed and self.stream_places.get(v) == place]:
            sid = self.stream_keys.pop(key)
            self.streams.pop(sid, None)
            self.stream_places.pop(sid, None)
        sid = self._next_stream
        self._next_stream += 1                                     # never reused within a boot (core §9)
        self.streams[sid], self.stream_keys[(conn, mech)] = Stream(), sid
        self.stream_places[sid] = place
        self.streams[sid].add_mark(MARK["attach"], self.now())
        return sid, False

    def emit(self, sid: int, data: bytes) -> None:
        """The target writes to its console stream `sid`."""
        self.streams[sid].data += data

    def target_says(self, data: bytes, target: FakeTarget | None = None) -> None:
        """The target (the first one by default) writes to its console: every open stream on a connection to it."""
        target = target or self.target
        for (cid, _), sid in self.stream_keys.items():
            if cid in self.conns and self._target_of(cid) is target and not self.streams[sid].closed:
                self.streams[sid].data += data

    # ---- oep.fixture.gpio -----------------------------------------------------------------------
    def _gpio(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        mine = {ch for f, role, ch in self.plan if f == fn and role == 1}
        if op == _GPIO.op["set"]:
            n = t.take("B")
            pairs = [t.take("HB") for _ in range(n)]
            t.tail()
            for i, (ch, mode) in enumerate(pairs):
                if ch not in mine or mode > 7:
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

    # ---- oep.probe.config -----------------------------------------------------------------------
    def _config_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _CFG.op["get"]:
            first = t.take("H")
            t.tail()
            items = self._canonical(self.config)
            budget = self.probe.max_frame - m.RESULT_HEADER - 5
            out, sent = b"", 0
            for item in items[first:]:
                if out and len(out) + len(item) > budget:
                    break
                out += item
                sent += 1
            more = 1 if first + sent < len(items) else 0
            return m.COMPLETED, m.SUCCESS, struct.pack("<BI", more, self._hash(self.config)) + out
        if op == _CFG.op["set"]:
            new = dict(self.config)
            seen, plan_roles, plans = set(), set(), {}
            for tag, value in m.split_tlvs(t.data) if t.data else []:
                tag &= 0x7F                                        # kept without the critical bit
                if tag not in self.items:
                    raise Reject(m.UNSUPPORTED, bytes([tag]))
                key = self._item_key(tag, value)
                if tag == ITEM["plan"]:                            # one item per assignment; fn alone = no plan
                    plans.setdefault(key, [])
                    if len(value) != 2:
                        if len(value) != 5 or (key, value[2]) in plan_roles:
                            raise Reject(m.MALFORMED)
                        plan_roles.add((key, value[2]))
                        plans[key].append(value)
                    continue
                if (tag, key) in seen:
                    raise Reject(m.MALFORMED)                      # the same key twice in one set
                seen.add((tag, key))
                if len(value) == self._key_len(tag):
                    new.pop((tag, key), None)                      # the key alone removes the item
                else:
                    new[(tag, key)] = value
            for fn, values in plans.items():
                new.pop((ITEM["plan"], fn), None)
                if values:
                    new[(ITEM["plan"], fn)] = values
            self._apply_config(new, changed_slots={k for t_, k in seen if t_ == ITEM["slot"]})
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self._hash(self.config))
        if op == _CFG.op["save"]:
            t.tail()
            if len(b"".join(self._canonical(self.config))) > self.storage_max:
                raise Reject(m.UNAVAILABLE)
            self.saved = dict(self.config)
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self._hash(self.config))
        if op == _CFG.op["erase"]:
            t.tail()
            self.saved = None
            return m.COMPLETED, m.SUCCESS, b""
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    @staticmethod
    def _key_len(tag: int) -> int:
        return 1 if tag in (ITEM["slot"], ITEM["bind"]) else 2

    def _item_key(self, tag: int, value: bytes) -> int:
        if len(value) < self._key_len(tag):
            raise Reject(m.MALFORMED)
        return value[0] if self._key_len(tag) == 1 else struct.unpack_from("<H", value)[0]

    @staticmethod
    def _canonical(config: dict) -> list[bytes]:
        """probe.config §2: tag order, then key order (plan by (fn, role)); TLVs without the critical bit."""
        rows = []
        for (tag, key), value in config.items():
            for v in (value if isinstance(value, list) else [value]):
                rows.append((tag, key, v[2] if isinstance(value, list) else 0, bytes([tag, len(v)]) + v))
        return [r[3] for r in sorted(rows)]

    def _hash(self, config: dict | None) -> int:
        return zlib.crc32(b"".join(self._canonical(config or {})))

    def load_config(self, items: list[bytes], saved: bool = True) -> None:
        """Put `items` (item TLVs) in as the config, as a set would; with `saved` they are also the saved config
        (a probe that booted with them)."""
        new: dict = {}
        for tag, value in m.split_tlvs(b"".join(items)):
            key = self._item_key(tag, value)
            if tag == ITEM["plan"]:
                new.setdefault((tag, key), []).append(value)
            else:
                new[(tag, key)] = value
        self._apply_config(new, changed_slots=None)
        if saved:
            self.saved = dict(self.config)

    def _apply_config(self, new: dict, changed_slots: set[int] | None = None, boot: bool = False) -> None:
        """Check the whole config, then make it the current one (set is all-or-nothing up to reserving resources);
        automatic attaches and console opens follow (and are not rolled back)."""
        slots, binds = {}, {}
        for (tag, key), value in new.items():
            if tag == ITEM["slot"]:
                slots[key] = self._parse_slot(value)
        names = [s.name for s in slots.values()]
        if len(set(names)) != len(names):
            raise Reject(m.MALFORMED)
        places = [(s.wire_fn, s.pair) for s in slots.values()]
        if len(set(places)) != len(places):
            raise Reject(m.UNAVAILABLE)
        for fn in self.pairs:
            if sum(1 for s in slots.values() if s.wire_fn == fn and s.attach == SLOT_ATTACH["at_boot"]) > \
                    self.max_connections.get(fn, 1):
                raise Reject(m.UNAVAILABLE)
        for (tag, key), value in new.items():
            if tag == ITEM["bind"]:
                binds[key] = self._parse_bind(value, slots)
        plans: dict[int, list[tuple[int, int, int]]] = {}
        for (tag, key), value in new.items():
            if tag == ITEM["plan"]:
                plans[key] = [struct.unpack_from("<HBH", v) for v in value if len(v) >= 5]
                if any(len(v) != 5 for v in value):
                    raise Reject(m.MALFORMED)
            elif tag in (ITEM["label"], ITEM["idle"]):
                if tag == ITEM["idle"] and (len(value) != 3 or value[2] > 2):
                    raise Reject(m.MALFORMED)
        old_plan_fns = {k[1] for k in self.config if k[0] == ITEM["plan"]}
        want = [a for fn in plans for a in plans[fn]]
        self.slots = slots                                         # the pin check below sees the new slots
        try:
            self._check_plan(want)
        except Reject:
            self.slots = {k: self._parse_slot(v) for (t, k), v in self.config.items() if t == ITEM["slot"]}
            raise
        # accepted: make it current
        for fn in old_plan_fns - set(plans):
            self._drop_plan(fn)
        for fn, assigned in plans.items():
            self.plan = {a for a in self.plan if a[0] != fn} | set(assigned)
            self.plan_from_config.add(fn)
        self.config = new
        for n in list(self.slot_rt):
            if n not in slots:
                del self.slot_rt[n]
        for n in slots:
            self.slot_rt.setdefault(n, SlotRuntime())
        for port, b in binds.items():
            if port not in self.binds or self.binds[port] != b:
                self.selected[port] = b.selected if b.mode == BIND_MODE["manual"] else 0
                for key in b.streams:
                    self.flows.pop((port, key), None)
                self.mixed_out.pop(port, None)
        self.binds = binds
        for n, s in slots.items():
            if s.attach == SLOT_ATTACH["at_boot"] and (boot or changed_slots is None or n in changed_slots):
                self.slot_rt[n].evicted = False
                self._auto_attach(n)
        self._refresh()

    def _parse_slot(self, v: bytes) -> Slot:
        t = Take(v)
        n, wire_fn, swdio, swclk, attach, retry_s, mech, name_len = t.take("BHHHBHBB")
        name = t.bytes(name_len)
        scheme = t.take("B")
        rest = v[t.at:]
        if n >= self.slots_max or attach not in SLOT_ATTACH.values():
            raise Reject(m.MALFORMED)
        if retry_s and attach != SLOT_ATTACH["at_boot"]:
            raise Reject(m.MALFORMED)
        if not SLOT_NAME.fullmatch(name.decode("ascii", "replace")):
            raise Reject(m.MALFORMED)
        if self.names.get(wire_fn) not in WIRES or (swdio, swclk) not in self.pairs.get(wire_fn, []):
            raise Reject(m.UNAVAILABLE)
        if mech not in self.mechanisms:
            raise Reject(m.UNSUPPORTED)
        lock = None
        if scheme:
            if not rest or len(rest) % 2:
                raise Reject(m.MALFORMED)
            lock = (scheme, rest[:len(rest) // 2], rest[len(rest) // 2:])
        elif rest:
            raise Reject(m.MALFORMED)
        return Slot(n, wire_fn, (swdio, swclk), attach, retry_s, mech, name.decode(), lock)

    def _parse_bind(self, v: bytes, slots: dict[int, Slot]) -> Bind:
        t = Take(v)
        port, mode, selected, n = t.take("BBBB")
        streams = tuple(t.take("BH") for _ in range(n))
        if t.at != len(v) or n == 0:
            raise Reject(m.MALFORMED)
        if port not in self.serial_ports:
            raise Reject(m.UNAVAILABLE)
        if mode not in BIND_MODE.values() or not self.bind_modes & (1 << mode):
            raise Reject(m.UNSUPPORTED)
        if mode == BIND_MODE["manual"] and selected >= n:
            raise Reject(m.MALFORMED)
        for kind, i in streams:
            if kind == BIND_STREAM["slot_console"]:
                if i not in slots:
                    raise Reject(m.UNAVAILABLE)
            elif kind == BIND_STREAM["fixture_uart"]:
                if self.names.get(i) != "oep.fixture.uart":
                    raise Reject(m.UNAVAILABLE)
            else:
                raise Reject(m.MALFORMED)
        return Bind(port, mode, selected if mode == BIND_MODE["manual"] else 0, streams)

    # ---- slots: automatic attach, the lock check, what uses a connection ------------------------
    @staticmethod
    def _lock_ok(s: Slot, tid: int | None) -> bool | None:
        """True / False; None = the slot has a lock and there is no target_id to check."""
        if s.lock is None:
            return True
        if tid is None:
            return None
        scheme, mask, value = s.lock
        raw = struct.pack("<I", tid)
        if scheme != 1 or len(mask) != len(raw):
            return False
        return bytes(a & b for a, b in zip(raw, mask)) == value

    def _bound(self, n: int) -> bool:
        return any((BIND_STREAM["slot_console"], n) in b.streams for b in self.binds.values())

    def _auto_attach(self, n: int) -> None:
        s = self.slots[n]
        rt = self.slot_rt[n]
        rt.last_try_ms = self.now()
        tg = self.targets[(s.wire_fn, s.pair)]
        cid = self._conn_at(s.wire_fn, s.pair)
        if cid is None:
            if not tg.present:
                return
            try:
                cid = self._seat(s.wire_fn, s.pair, tg, 4_000_000, evict=False)   # automatic: never evicts
            except Reject:
                return
            tg.havereset = False
        c = self.conns[cid]
        ok = self._lock_ok(s, c.tid)
        rt.no_tid = ok is None
        if ok is True:
            c.users.add(("slot", n))
            rt.mismatch_tid = None
        else:
            rt.mismatch_tid = c.tid                                # found, wrong chip (or no id): let go of it
            if not c.users:
                self._close_conn(cid, MARK["detach"])

    def _refresh(self) -> None:
        """Make what the slots use match the config: a bound slot rides any connection on its place (lock
        permitting) with its console open; an at-boot slot keeps its automatic connection; nothing else."""
        for cid, c in list(self.conns.items()):
            for u in [u for u in c.users if u != "host"]:
                s = self.slots.get(u[1])
                if (s is None or (s.wire_fn, s.pair) != (c.fn, c.pair) or self._lock_ok(s, c.tid) is not True
                        or not (s.attach == SLOT_ATTACH["at_boot"] or self._bound(u[1]))):
                    c.users.discard(u)
            if not c.users:
                self._close_conn(cid, MARK["detach"])
        for n, s in self.slots.items():
            cid = self._conn_at(s.wire_fn, s.pair)
            if cid is None or self._lock_ok(s, self.conns[cid].tid) is not True or not self._bound(n):
                continue
            self.conns[cid].users.add(("slot", n))
            sid, existing = self._open_stream(cid, s.mechanism)
            if not existing:
                for port, b in self.binds.items():
                    key = (BIND_STREAM["slot_console"], n)
                    if key in b.streams:
                        self.flows[(port, key)] = Flow(sid, 0)

    def tick(self) -> None:
        """Time passes: the lease, at-boot retries, mixed lines closed by quiet."""
        self._lapse()
        now = self.now()
        for n, s in self.slots.items():
            rt = self.slot_rt[n]
            if (s.attach == SLOT_ATTACH["at_boot"] and s.retry_s and not rt.evicted
                    and self._conn_at(s.wire_fn, s.pair) is None
                    and (rt.last_try_ms is None or now - rt.last_try_ms >= 1000 * s.retry_s)):
                self._auto_attach(n)
                self._refresh()

    def _slot_state(self, n: int) -> bytes:
        s, rt = self.slots[n], self.slot_rt[n]
        cid = self._conn_at(s.wire_fn, s.pair)
        tid = self.conns[cid].tid if cid is not None else rt.mismatch_tid
        if cid is not None:
            ok = self._lock_ok(s, tid)
            state = SLOT_STATE["connected"] if ok else (SLOT_STATE["no_target_id"] if ok is None
                                                        else SLOT_STATE["lock_mismatch"])
        else:
            state = (SLOT_STATE["no_target_id"] if rt.no_tid else
                     SLOT_STATE["lock_mismatch"] if rt.mismatch_tid is not None else SLOT_STATE["absent"])
        age = NEVER if rt.last_try_ms is None else min(NEVER - 1, self.now() - rt.last_try_ms)
        raw = b"" if tid is None else struct.pack("<I", tid)
        return struct.pack("<BBHIBB", n, state, cid or 0, age, 1 if raw else 0, len(raw)) + raw

    def _bind_state(self, port: int) -> bytes:
        b = self.binds[port]
        mixed = b.mode == BIND_MODE["mixed"]
        selected = 0xFF if mixed else self.selected.get(port, 0)
        if port in self.held_ports and self.holder is not None:
            flow = BIND_FLOW["held"]
        else:
            keys = b.streams if mixed else (b.streams[selected],)
            flow = BIND_FLOW["streaming"] if any(self._stream_for(k)[1] is not None for k in keys) else BIND_FLOW["idle"]
        return struct.pack("<BBBB", port, b.mode, selected, flow)

    # ---- serial ports: the raw bytes outside the frames (core §3.4, probe.config §1.2) ----------
    def _stream_for(self, key: tuple[int, int]) -> tuple[object, Stream | None]:
        kind, i = key
        if kind == BIND_STREAM["fixture_uart"]:
            return ("uart", i), self.uarts.get(i)
        s = self.slots.get(i)
        if s is None:
            return None, None
        cid = self._conn_at(s.wire_fn, s.pair)
        sid = self.stream_keys.get((cid, s.mechanism)) if cid is not None else None
        if sid is None or self.streams[sid].closed:
            return None, None
        return sid, self.streams[sid]

    def _flow(self, port: int, key: tuple[int, int]) -> tuple[Flow, Stream | None]:
        sid, s = self._stream_for(key)
        f = self.flows.get((port, key))
        if s is None:
            return f or Flow(), None
        if f is None or f.sid != sid:
            f = self.flows[(port, key)] = Flow(sid, s.end, last_ms=self.now())   # first seen: from now
        if f.pos < s.base:
            f.pos = s.base                                         # overflowed past the port: the oldest left
        return f, s

    def port_held(self, port: int) -> bool:
        return self.holder is not None and port in self.held_ports

    def port_output(self, port: int, room: int | None = None) -> bytes:
        """The raw bytes serial port `port` sends now by its bind (empty while a session holds it)."""
        room = self.CHUNK if room is None else room
        b = self.binds.get(port)
        if b is None or self.port_held(port) or room <= 0:
            return b""
        if b.mode != BIND_MODE["mixed"]:
            f, s = self._flow(port, b.streams[self.selected.get(port, 0)])
            if s is None:
                return b""
            out = bytes(s.data[f.pos - s.base:f.pos - s.base + room])
            f.pos += len(out)
            return out
        pending = self.mixed_out.setdefault(port, bytearray())
        now = self.now()
        for key in b.streams:
            f, s = self._flow(port, key)
            if s is None:
                continue
            new = bytes(s.data[f.pos - s.base:])
            f.pos += len(new)
            if new:
                f.line += new
                f.last_ms = now
            while True:
                cut = f.line.find(b"\n")
                if cut < 0 and len(f.line) < self.MIXED_LINE_MAX and not (f.line and now - f.last_ms >= self.MIXED_QUIET_MS):
                    break
                if not f.line:
                    break
                end = cut + 1 if cut >= 0 else min(len(f.line), self.MIXED_LINE_MAX)
                line = bytes(f.line[:end])
                del f.line[:end]
                pending += b"[" + self._mixed_name(key).encode() + b"] " + line + (b"" if line.endswith(b"\n") else b"\n")
        out = bytes(pending[:room])
        del pending[:room]
        return out

    def _mixed_name(self, key: tuple[int, int]) -> str:
        kind, i = key
        if kind == BIND_STREAM["slot_console"]:
            return self.slots[i].name
        rx = next((ch for fn, role, ch in self.plan if fn == i and role == 1), None)
        label = None
        if rx is not None:
            label = self.static_labels.get(rx)
            v = self.config.get((ITEM["label"], rx))
            if v is not None:
                label = v[2:].decode("utf-8", "replace")
        if not label:
            return f"uart{i}"
        return "".join("_" if c == "]" or ord(c) < 0x20 else c for c in label)

    def port_input(self, port: int, data: bytes) -> None:
        """Raw bytes that came in on serial port `port` outside any frame."""
        b = self.binds.get(port)
        if b is None or self.port_held(port) or b.mode == BIND_MODE["mixed"] or not data:
            return
        key = b.streams[self.selected.get(port, 0)]
        sid, s = self._stream_for(key)
        if s is None:
            return
        if key[0] == BIND_STREAM["fixture_uart"]:
            self.uart_tx.setdefault(key[1], bytearray()).extend(data)
        else:
            s.written += data

    def _session_over(self) -> None:
        """The session ended (end, lapse, force): the ports it held resume from its last host reset (or now)."""
        for port in self.held_ports:
            b = self.binds.get(port)
            if b is None:
                continue
            for key in b.streams:
                sid, s = self._stream_for(key)
                if s is None:
                    continue
                rsid, pos = self.session_resets.get(key, (sid, s.end))
                self.flows[(port, key)] = Flow(sid, pos if rsid == sid else s.end, last_ms=self.now())
        self.held_ports.clear()
        self.session_resets.clear()


SIMS = {"oep.wire.rvswd": "wire", "oep.wire.swio": "wire", "oep.target.riscv-dm": "dm",
        "oep.target.console": "console", "oep.fixture.gpio": "gpio", "oep.fixture.uart": "uart",
        "oep.probe.config": "config_op"}
