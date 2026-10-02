"""A fake probe endpoint that speaks OEP v1, answering whole messages (no hardware).

This is the spec side's "working spec": ch32rv, this client and the probe firmware are checked against it. It wraps
a `fake.FakeProbe` (the static declarations) and does what oep-spec docs/oep-core.ja.md and docs/oep-if-*.ja.md
define:

- confirm with a revision range (its answer carries the boot_id, core §7.1); `revision=0` makes a v0 probe that answers
  in the v0 shape and drops role 0x81 requests unanswered
- the lock (core §6): a host-chosen session id, extended by every request of its holder and counted from when that
  request completed; the last id is remembered with how its lock ended: after an end it resumes (resumed 1), after a
  lapse its first request is rejected expired and its open answers resumed 2 (the resources were swept, core §9; an id
  forced out sees locked, then no_session: the last id is the forcing one); lease 0 = the probe default, 1000-60000 ms taken as asked, longer ones cut to `lease_max_ms`; the open's
  owner TLV shown by lock_state and by rejected locked (never the session id); subscriptions survive a same-id open
  while held
- the resend table (core §5.2): the last session's recent requests with their results (results longer than
  `remember_max` bytes are not kept -> result_lost), corr_reused, result_lost for old requests, emptied by open
- rejects in core §4.3's order: no session / expired / locked (+ remaining ms), session required, malformed (short
  fixed part, tag 0xFF, a TLV not in the one encoding), unsupported (always `tag [TLV]`, 0x00 for a fixed-part value),
  unavailable (+ cause), no connection; a resource number of the wrong kind (a stream where a connection goes) is
  unavailable cause 6 - connections and streams share one u16 space that wraps (core §9)
- request tails: unknown critical -> rejected unsupported, unknown non-critical -> listed in the result's ignored TLV
  (0x7F) for every op, on every completed answer, failed and partial ones too (`Take` keeps them, `_dispatch` appends
  them); a TLV in a describe or probe.config get request is malformed (core §7.3); `tail=` appends TLVs to every result that may carry them, so hosts can be checked to skip what they do not
  know. Every answer ending in data or a list carries its length (core §2.3), so every answer may carry TLVs
- the plan (plan_apply / plan_release; the session's plan goes when the lease lapses; plan_roles), subscriptions
  (fn 0's heartbeat `boot_id uptime_ns`; an fn that emits nothing is unsupported), and simulations of the
  interfaces the profiles offer: oep.wire.rvswd / swio (scan with its TLVs, attach on declared pin pairs with the
  reset TLV, several connections up to max_connections, the seat rule, connections), oep.target.riscv-dm on one
  `FakeTarget` per pin pair (dmi / run answers count their values; run's stopped 2), oep.target.console streams (one
  live stream per connection, lifetime by its users, the streams list), oep.fixture.gpio (the lines outside through
  the test hook `gpio_world`; a target's `reset_line` makes the other reset channels reset nothing), oep.fixture.uart (its stream
  made by the plan, status, the settings' uart item), oep.fixture.i2c-target / spi-target (fixture §3 / §4: the plan's
  SDA / SCL and SCK / MOSI / MISO / CS, role_channels and an exact channel_group; configure, arm, preload, read_rx,
  status, reset, stretch when declared; no bus controller of its own - the test hooks `i2c_write` / `i2c_read` /
  `spi_transfer` are one transaction on the bus), and oep.probe.config (plan / label / idle / slot / bind / uart
  / disable items, get / set / unset / save / erase, the state op - describe is declarations only; a disabled channel
  is refused everywhere with unavailable cause 5 and never parked: `parked` records the free pins' states it set -
  idle modes 3 / 4 drive their level, refused unsupported on `input_only` channels; every release goes there, and a
  gpio line taken keeps that state until its first set)
- the output strength (fixture §1.1): the profiles' gpio declare drive_levels (`fake.DRIVE_LEVELS_MA`, default
  `fake.DRIVE_DEFAULT`; `fake.without_drive_levels` makes a probe without them); set's drive TLVs per element (the
  malformed and ignored rules), the effective strength (`gpio_drive`: the set's drive, else the idle item's, else the
  default; taken and released lines at the idle state's, `parked_drive`), read's drive TLV; the idle item's drive
- the slot's boot_reset and the retry with reset (probe.config §1.1 / §3.1): after an automatic attach got no answer
  (a `FakeTarget.silent_until_reset` target answers nothing until a reset through its line), once per boot and only
  before any session took the lock, through the `nrst` line found by §1.3 (`line_for`), hold
  slot_retry_reset_hold_ms; `slot_reset_log`, slot_state's reset_at_ns
- the serial ports' raw side (core §3.4, probe.config §1.2): `port_input` / `port_output` carry the bytes outside
  the frames for each serial port by its bind; a port the lock holder's requests came in on is held until the
  session ends, then resumes from the session's last host reset. The byte framing itself is `fake_serial`.
- port_speed (core §3.5, optional): on when the profile's describe declares it (`port_speed_base`, the boot speed;
  None = off: the op is unknown_operation). try / commit / revert on the UART bridge the request came in on (else
  unavailable cause 6), a step that does not fit the port's state (commit at the boot speed or when committed,
  revert at the boot speed, try while trying or committed) unavailable cause 6, a step above 2 malformed, a rate the
  fake UART cannot make (outside 300..5000000) unsupported; a try reverts after verify_ms without a commit or at a
  broken candidate once a good frame came at the new speed, a commit after idle_ms (at most port_speed_idle_max_ms,
  3000; 0 and anything longer count as that maximum) with no good frame or at 3 broken candidates in a row with no
  good frame between, the session's end reverts after its answer. The line itself is modelled by `broken_rates`
  (rate -> BrokenRate): frames at such a rate break, from a size and in the directions given, only both ways at once
  (`duplex`), only every Nth (`every`), only once `after` bytes have passed at the rate since the switch to it
  (`fake_serial` applies it).

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

from . import catalog, config as cfgmod, fake, fake_capture, message as m, registry as reg

TOY_WRITE, TOY_READ = 0x01, 0x02
OK, WAIT, LINE, FAULT, TIMEOUT, STATE = (reg.STATUS[k] for k in ("ok", "wait", "line", "fault", "timeout", "state"))
_RV, _CON, _GPIO, _UART, _CFG = (reg.TARGET_RISCV_DM, reg.TARGET_CONSOLE, reg.FIXTURE_GPIO, reg.FIXTURE_UART,
                                 reg.PROBE_CONFIG)
STEP = _RV.enum["dmi_step"]
STEP_ARGS = {STEP["write"]: "BI", STEP["read"]: "B", STEP["poll_reads"]: "BIIH", STEP["wait_us"]: "I",
             STEP["poll_us"]: "BIII"}
MARK = reg.COMMON.enum["mark_kind"]
MARK_RESET, MARK_RESTART, MARK_CLOSED = (reg.COMMON.enum[k] for k in ("mark_detail_reset", "mark_detail_restart",
                                                                       "mark_detail_closed"))
MECHANISM_NONE = _CON.enum["mechanism"]["none"]
STREAM_STATE, STREAM_USERS = _CON.enum["stream_state"], _CON.enum["stream_users"]
ATTACH_FLAGS = reg.WIRE_RVSWD.enum["attach_flags"]
RUN_STOPPED = _RV.enum["run_stopped"]
ITEM = _CFG.tlv["item"]
IDLE_MODE = _CFG.enum["idle_mode"]
CFG_DESCRIBE = _CFG.tlv["describe"]
SLOT_ATTACH = _CFG.enum["slot_attach"]
SLOT_STATE = _CFG.enum["slot_state"]
BIND_MODE = _CFG.enum["bind_mode"]
BIND_STREAM = _CFG.enum["bind_stream"]
BIND_FLOW = _CFG.enum["bind_flow"]
WIRES = ("oep.wire.rvswd", "oep.wire.swio")
OWNER = reg.CORE.tlv["open"]["owner"]
SLOT_NAME = re.compile(r"[a-z0-9_-]{1,32}")
NO_SLOT, NEVER_NS = 0xFF, 0xFFFFFFFFFFFFFFFF
PIN_ROLE_RESET = reg.WIRE_RVSWD.enum["pin_role"]["reset"]   # the channels an attach's reset TLV may take
TARGET_ID_LEN = reg.WIRE_RVSWD.enum["target_id_len"]["wch_dmi_7f"]
TARGET_ID_SCHEMES = set(reg.WIRE_RVSWD.enum["target_id_scheme"].values()) | set(reg.WIRE_SWD.enum["target_id_scheme"].values())
_I2C, _SPI = reg.FIXTURE_I2C_TARGET, reg.FIXTURE_SPI_TARGET
I2C_MODE, I2C_FEATURES, SPI_FEATURES = _I2C.enum["mode"], _I2C.enum["features"], _SPI.enum["features"]
TARGET_ROLES = {"oep.fixture.gpio": {1}, "oep.fixture.uart": {1, 2}, _I2C.name: set(_I2C.enum["role"].values()),
                _SPI.name: set(_SPI.enum["role"].values())}   # the plan roles each fixture takes
UART_FORMAT_MASK = 0x1F                                    # the defined format bits (fixture §2)
UART_CONFIGURED = _UART.enum["uart_configured"]            # status's configured byte: default / session / item / item_fallback
_T_SCAN, _T_ATTACH, _T_DETACH, _T_ATTACH_ANSWER = (reg.WIRE_RVSWD.tlv[k] for k in ("scan", "attach", "detach", "attach_answer"))
GPIO_SET_DRIVE, GPIO_READ_DRIVE = _GPIO.tlv["set"]["drive"], _GPIO.tlv["read_answer"]["drive"]
DRIVE_KIND = _GPIO.enum["drive_kind"]                      # 0 a level number, 1 an mA ceiling (fixture §1.1)
NOT_DRIVEN = _GPIO.enum["drive_read"]["not_driven"]
OUTPUT_MODES = (_GPIO.enum["mode"]["output_low"], _GPIO.enum["mode"]["output_high"])   # the modes a strength applies to
SLOT_BOOT_RESET = _CFG.enum["slot_boot_reset"]
RETRY_RESET_HOLD_MS = reg.TIMING["slot_retry_reset_hold_ms"]   # the retry with reset's hold (probe.config §3.1)


class Reject(Exception):
    """A rejection. unsupported's payload is always `tag(u8) [TLV]` (core §4.3): without one, 0x00 = a fixed-part value."""

    def __init__(self, reason: int, payload: bytes = b""):
        if reason == m.UNSUPPORTED and not payload:
            payload = bytes([m.TAG_FIXED])
        self.reason, self.payload = reason, payload


def unsupported_fixed(*tlvs: bytes) -> Reject:
    """rejected unsupported for a fixed-part value, with TLVs saying which (channel, index: core §4.3)."""
    return Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]) + b"".join(tlvs))


_UNA = reg.CORE.tlv["unavailable_payload"]
UNAVAILABLE_CAUSE, HOLDER_KIND = reg.CORE.enum["unavailable_cause"], reg.CORE.enum["holder_kind"]


def unavailable(cause: str | None = None, channel: int | None = None, holder_fn: int | None = None,
                holder_kind: str | None = None, extra: bytes = b"") -> Reject:
    """rejected unavailable with core §4.3's payload: why, the channel, who holds it (each optional)."""
    body = b""
    if cause:
        body += m.tlv(_UNA["cause"], bytes([UNAVAILABLE_CAUSE[cause]]))
    if channel is not None:
        body += m.tlv(_UNA["channel"], struct.pack("<H", channel))
    if holder_fn is not None:
        body += m.tlv(_UNA["holder_fn"], struct.pack("<H", holder_fn))
    if holder_kind:
        body += m.tlv(_UNA["holder_kind"], bytes([HOLDER_KIND[holder_kind]]))
    return Reject(m.UNAVAILABLE, body + extra)


def wrong_kind() -> Reject:
    """A resource number of another kind (a stream where a connection goes): rejected unavailable cause 6 (core §9)."""
    return unavailable("wrong_state")


class Take:
    """Reads a request's fixed part; too short -> rejected malformed. `tail(known)` applies the request-tail rule and
    keeps the ignored tags in `ignored` (also what it returns, so `refuse` and the handlers add to the same list):
    `Endpoint._dispatch` lists them in the answer's ignored TLV (core §2.3), so no handler attaches them itself."""

    def __init__(self, payload: bytes):
        self.data, self.at = payload, 0
        self.ignored: list[int] = []

    def no_tail(self) -> None:
        """A request that takes no TLV (describe, probe.config get; core §7.3): anything after the fixed part is
        rejected malformed."""
        if self.at < len(self.data):
            raise Reject(m.MALFORMED)

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
        rest, at, got, ignored = self.data[self.at:], 0, {}, self.ignored
        self.critical = set()                                      # known tags that came with the critical bit
        self.repeated: list[tuple[int, bytes]] = []                # every known TLV in order, critical bit kept (got: the last)
        while at < len(rest):
            if at + 2 > len(rest):
                raise Reject(m.MALFORMED)
            tag, n = rest[at], rest[at + 1]
            at += 2
            if n == m.TLV_LEN_LONG:                                # the long form: a u16 length (core §2.2)
                if at + 2 > len(rest):
                    raise Reject(m.MALFORMED)
                n = struct.unpack_from("<H", rest, at)[0]
                at += 2
                if n <= m.TLV_SHORT_MAX:
                    raise Reject(m.MALFORMED)                      # not the one encoding
            if at + n > len(rest):
                raise Reject(m.MALFORMED)
            value = rest[at:at + n]
            at += n
            if tag in (m.TAG_INVALID, m.TAG_IGNORED, m.TAG_FIXED):
                raise Reject(m.MALFORMED)
            if tag & 0x7F in known:
                got[tag & 0x7F] = value
                self.repeated.append((tag, value))
                if tag & m.TAG_CRITICAL:
                    self.critical.add(tag & 0x7F)
            elif tag & m.TAG_CRITICAL:
                raise Reject(m.UNSUPPORTED, bytes([tag]))
            else:
                ignored.append(tag)
        self.at = len(self.data)
        return got, ignored

    def refuse(self, tag: int, got: dict[int, bytes]) -> None:
        """A known TLV whose value cannot be honoured: critical -> unsupported with the tag as received, else it is
        dropped and listed as ignored."""
        if tag in self.critical:
            raise Reject(m.UNSUPPORTED, bytes([tag | m.TAG_CRITICAL]))
        got.pop(tag, None)
        self.ignored.append(tag)


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
    unstoppable: bool = False                          # run: the limit passes and the hart cannot be halted (stopped 2)
    reset_line: int | None = None                      # the channel wired to its reset (None: any reset channel resets it)
    silent_until_reset: bool = False                   # answers nothing on the wire until a reset through its line

    @property
    def answers(self) -> bool:
        """Something answers on the wire now (scan, attach): present, and not stuck until a reset."""
        return self.present and not self.silent_until_reset

    def resets_through(self, channel: int) -> bool:
        return self.reset_line in (None, channel)

    def dmstatus(self) -> int:
        return 0x82 | ((0x300 if self.halted else 0xC00)) | (0xC0000 if self.havereset else 0)

    def read_dmi(self, address: int) -> int:
        queue = self.dmi_reads.get(address)
        if queue:
            self.dmi[address] = queue.pop(0)
        return self.dmi.get(address, 0)


@dataclass
class I2cState:
    """One oep.fixture.i2c-target (fixture §3): what configure made, the frames waiting for read_rx, the tx slots."""
    state: int = 0                       # 0 not configured, 1 running
    address: int = 0
    mode: int = 0
    armed: int = 0                       # mode 1: the length arm_rx waits for (0 = not armed)
    queue: list = field(default_factory=list)   # (frame, ns)
    rx_frames: int = 0
    errors: int = 0
    slots: int = 0                       # preload_tx's running count (u8, wraps)
    tx: list = field(default_factory=list)      # preloaded, unread
    stretch_us: int = 0


@dataclass
class SpiState:
    """One oep.fixture.spi-target (fixture §4): what configure made, the armed transaction, the finished ones."""
    state: int = 0
    mode: int = 0
    bit_order: int = 0
    armed: tuple[int, bytes] | None = None      # (length, MISO bytes) of the one transaction it waits for
    queue: list = field(default_factory=list)   # (bits, MOSI bytes, ns)
    transactions: int = 0
    errors: int = 0


@dataclass
class Stream:
    """A position stream (common §1): bytes from position `base`, marks with serials; a console stream's users
    ("host" and ("slot", n), console §2) and what it is on."""
    data: bytearray = field(default_factory=bytearray)
    base: int = 0
    marks: list = field(default_factory=list)          # (serial, position, kind, time_ns, detail)
    serial: int = 0
    closed: bool = False
    written: bytearray = field(default_factory=bytearray)
    users: set = field(default_factory=set)
    conn: int = 0                                      # console: the connection it is on
    mechanism: int = 0

    @property
    def end(self) -> int:
        return self.base + len(self.data)

    def add_mark(self, kind: int, time_ns: int, detail: int = 0) -> None:
        self.marks.append((self.serial, self.end, kind, time_ns, detail))
        self.serial = (self.serial + 1) & 0xFFFFFFFF

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
    idle_clock: int = 0                                # rvswd: SWCLK while the line rests, 0 high / 1 low


@dataclass(frozen=True)
class Slot:
    slot: int
    wire_fn: int
    pair: tuple[int, int]
    attach: int
    retry_ms: int
    max_speed: int                                     # Hz; 0: no ceiling (oep-if-debug §3: the target's, the host's to set)
    idle_clock: int
    mechanism: int
    name: str
    lock: tuple[int, bytes, bytes] | None              # (scheme, mask, value)
    boot_reset: int = 0                                # 1: the at-boot attach retries once with the reset line (§3.1)


def _bind_streams(v: bytes) -> list[int]:
    """Where each stream of a bind value starts (probe.config §1.2: n × (len, kind, id), len >= 3, its tail skipped)."""
    at, out = 4, []
    for _ in range(v[3]):
        if at >= len(v) or v[at] < 3 or at + 1 + v[at] > len(v):
            raise Reject(m.MALFORMED)
        out.append(at + 1)
        at += 1 + v[at]
    return out


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
    reset_at_ms: int | None = None                     # when the retry with reset started pulling the line (§3.1)


@dataclass
class Flow:
    """One stream as a serial port's bind carries it: the stream id and the port's position in it."""
    sid: object = None
    pos: int = 0
    line: bytearray = field(default_factory=bytearray)
    last_ms: int = 0


@dataclass
class BrokenRate:
    """How a line rate breaks frames in the fake (port_speed tests): frames of `min_size` bytes and more (on the wire)
    break, towards the host and / or towards the probe. A broken frame towards the probe is a candidate whose CRC does
    not match; towards the host its CRC is spoiled."""
    min_size: int = 0
    to_host: bool = True
    to_probe: bool = True
    duplex: bool = False       # only while both ways carry such frames at once (a request of min_size and more comes in
                               # while an answer of min_size and more is still unread): the request breaks (to_probe),
                               # the unread answer breaks (to_host)
    every: int = 1             # of the frames that would break, only every Nth does (1: all)
    seen: int = 0              # the frames that would have broken so far (`every` counts these)
    after: int = 0             # none breaks until this many bytes (every frame on the wire, both ways) have passed at
                               # the rate since the port last switched to it: a rate that passes a short verify and
                               # breaks later in use
    carried: int = 0           # the bytes passed at the rate since that switch (`after` counts these)

    def hit(self) -> bool:
        """One more frame that would break: True when this one does (`every`)."""
        self.seen += 1
        return self.seen % max(1, self.every) == 0


PORT_SPEED_TAG = reg.CORE.tlv["describe"]["port_speed"]
OP_PORT_SPEED = reg.CORE.op["port_speed"]
SPEED_STEP = reg.CORE.enum["port_speed_step"]
SPEED_IDLE_MAX_MS = reg.TIMING["port_speed_idle_max_ms"]   # committed: idle_ms at most this (0 and longer: this)
SPEED_BAD_MAX = 3                                  # committed: this many broken candidates in a row (no good frame between) revert
SPEED_RATES = (300, 5_000_000)                     # what the fake's UART makes (anything between, exactly)


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
        self.identity = {o.fn: (o.name, o.instance, o.revision) for o in probe.offered}   # what a saved item names
        self.fns = {name: fn for fn, name in sorted(self.names.items(), reverse=True)}   # first fn of each name
        self.static = {o.fn: o.tlvs for o in probe.offered}
        self.static_labels: dict[int, str] = {}
        # channels the probe can only read (a test sets them): an idle of mode 3 / 4 there is unsupported (probe.config §1)
        self.input_only: set[int] = set()
        self.transports: dict[int, int] = {}            # index -> kind, by the TLV's own index (core §7.5), not its order
        self.max_op_ms = reg.LIMITS["max_op_ms_reference"]
        self.plan_roles: int | None = None
        for t in self.static.get(0, ()):
            if t[0] == fake.CORE_LABEL:
                self.static_labels[struct.unpack_from("<H", t, 2)[0]] = t[4:2 + t[1]].decode()
            if t[0] == fake.CORE_TRANSPORT:
                self.transports[t[2]] = t[3]
            if t[0] == fake.CORE_MAX_OP_MS:
                self.max_op_ms = struct.unpack_from("<I", t, 2)[0]
            if t[0] == fake.CORE_PLAN_ROLES:
                self.plan_roles = struct.unpack_from("<I", t, 2)[0]
        self.serial_ports = {i for i, k in self.transports.items() if k in fake.SERIAL_KINDS}
        self.pairs: dict[int, list[tuple[int, int]]] = {}  # wire fn -> allowed (swdio, swclk), declared order
        self.max_connections: dict[int, int] = {}
        self.reset_channels: dict[int, set[int]] = {}     # wire fn -> channels an attach's reset TLV may take (role 3)
        self.pin_roles: dict[int, dict[int, set[int]]] = {}   # wire fn -> role -> candidates (role_channels wires)
        for fn, name in self.names.items():
            if name in WIRES:
                self.pairs[fn] = [self._group_pair(name, t) for t in self.static[fn] if t[0] == catalog.CHANNEL_GROUP]
                roles: dict[int, set[int]] = {}
                for t in self.static[fn]:
                    if t[0] == catalog.ROLE_CHANNELS and t[2] in (1, 2):
                        roles.setdefault(t[2], set()).update(
                            catalog.bitmap_to_channels(struct.unpack_from("<H", t, 3)[0], t[5:2 + t[1]]))
                if roles:
                    self.pin_roles[fn] = roles
                self.reset_channels[fn] = {
                    c for t in self.static[fn] if t[0] == catalog.ROLE_CHANNELS and t[2] == PIN_ROLE_RESET
                    for c in catalog.bitmap_to_channels(struct.unpack_from("<H", t, 3)[0], t[5:2 + t[1]])}
                self.max_connections[fn] = next((t[2] for t in self.static[fn] if t[0] == fake.MAX_CONNECTIONS), 1)
        self.targets: dict[tuple[int, tuple[int, int]], FakeTarget] = {
            (fn, p): FakeTarget() for fn in sorted(self.pairs) for p in self.pairs[fn]}
        for fn in sorted(self.pin_roles):                          # any pair: one target, on the first pair
            first = self._role_pairs(fn)[0]
            self.targets[(fn, first)] = FakeTarget()
        self.target = next(iter(self.targets.values()), FakeTarget())
        self.captures: dict[int, fake_capture.FakeCapture] = {
            fn: self._capture_from(self.static[fn]) for fn, name in self.names.items()
            if name in ("oep.fixture.logic", "oep.fixture.analog")}
        self.groups: dict[int, fake_capture.FakeGroup] = {
            fn: self._group_from(self.static[fn]) for fn, name in self.names.items() if name == "oep.fixture.capture-group"}
        self.mechanisms = set()
        for fn, name in self.names.items():
            if name == "oep.target.console":
                self.mechanisms |= {b for t in self.static[fn] if t[0] == fake.MECHANISMS for b in t[2:2 + t[1]]}
        self.block_max = {fn: next((struct.unpack_from("<H", t, 2)[0] for t in self.static[fn]
                                    if t[0] == catalog.MAX_LENGTH), 1 << 16)
                          for fn, name in self.names.items() if name == "oep.target.riscv-dm"}
        self.gpio_allowed = {fn: next((struct.unpack_from("<I", t, 2)[0] for t in self.static[fn]
                                       if t[0] == fake.GPIO_MODES), 0xFF)
                             for fn, name in self.names.items() if name == "oep.fixture.gpio"}
        # the output strengths (fixture §1.1, drive_levels): (default level, [approximate mA per level]) or None - one
        # declaration for the whole probe (every gpio fn declares the same)
        self.drive_levels: tuple[int, list[int]] | None = None
        for fn, name in sorted(self.names.items()):
            for t in self.static[fn] if name == "oep.fixture.gpio" else ():
                if t[0] == fake.GPIO_DRIVE_LEVELS:
                    default, n = t[2], t[3]
                    self.drive_levels = (default, list(struct.unpack_from(f"<{n}H", t, 4)))
        self.uart_formats = {fn: next((set(t[3:3 + t[2]]) for t in self.static[fn] if t[0] == fake.UART_FORMATS), {0})
                             for fn, name in self.names.items() if name == "oep.fixture.uart"}
        self.uart_max_hz = {fn: next((struct.unpack_from("<I", t, 2)[0] for t in self.static[fn] if t[0] == catalog.MAX_CLOCK_HZ),
                                     3_000_000) for fn, name in self.names.items() if name == "oep.fixture.uart"}
        def own(fn: int, tag: int, fmt: str, default: int) -> int:
            return next((struct.unpack_from("<" + fmt, t, 2)[0] for t in self.static[fn] if t[0] == tag), default)
        # the fixture targets' declarations (fixture §3 / §4): max_length, features, queue_depth, max_stretch_us
        # (i2c-target only; 0 when not declared)
        self.target_decl = {fn: (own(fn, catalog.MAX_LENGTH, "H", 1), own(fn, catalog.FEATURES, "I", 0),
                                 own(fn, _I2C.tlv["describe"]["queue_depth"], "B", 1),
                                 own(fn, _I2C.tlv["describe"]["max_stretch_us"], "I", 0) if name == _I2C.name else 0)
                            for fn, name in self.names.items() if name in (_I2C.name, _SPI.name)}
        cfg_fn = self.fns.get("oep.probe.config")
        cfg = {t[0]: t[2:2 + t[1]] for t in self.static.get(cfg_fn, ())}
        self.slots_max = cfg[CFG_DESCRIBE["slots_max"]][0] if CFG_DESCRIBE["slots_max"] in cfg else 0
        self.items = set(cfg.get(CFG_DESCRIBE["items"], b""))
        self.storage_max = struct.unpack_from("<I", cfg[CFG_DESCRIBE["storage"]])[0] if CFG_DESCRIBE["storage"] in cfg else 0
        self.bind_modes = struct.unpack_from("<I", cfg[CFG_DESCRIBE["bind_modes"]])[0] if CFG_DESCRIBE["bind_modes"] in cfg else 0
        self.console_accept = 64
        self.uart_accept = 256
        # port_speed (core §3.5): on when the profile declares it; the boot speed every revert goes back to
        self.port_speed_base: int | None = 115200 if any(t[0] == PORT_SPEED_TAG for t in self.static.get(0, ())) else None
        self.broken_rates: dict[int, BrokenRate] = {}   # the line: rates that break frames (fake_serial applies it)
        self._transport = 0                             # the transport the request being handled came in on
        # the lines outside (a test hook): gpio_world(channel, mode) -> the level an input mode reads (None: the
        # default - gpio_inputs, a pull-up 1); reads see the world as the modes set it (gpio_modes)
        self.gpio_world: Callable[[int, int], int | None] | None = None
        self._boot()

    def _boot(self) -> None:
        self.holder: int | None = None
        self.last: int | None = None
        self.last_swept = False              # the last id's lock lapsed: its next request is expired (core §6.2)
        self.owner: bytes | None = None
        self.lease_ms = self.lease_default_ms
        self.expires_ms = 0
        self.values: dict[int, int] = {}
        self.dropped = 0                     # requests a v0 endpoint dropped (role 0x81)
        self.requests: list[m.Request] = []
        self.subscribed: set[int] = set()
        self.heartbeat_ms = reg.TIMING["heartbeat_default_ms"]   # fn 0's subscription period
        self.next_heartbeat_ms = 0
        self.push_seq: dict[int, int] = {}              # fn -> the next event / data seq (core §11.2)
        self.outbox: list[bytes] = []                   # events and data frames waiting to go out (pushes())
        self.capture_slipped = False                    # every capture segment says flags bit2 (a pace that fell behind)
        self.plan: set[tuple[int, int, int]] = set()   # (fn, role, channel), from plan_apply and the config
        self.plan_from_config: set[int] = set()        # fns whose plan came from the config (not a session's)
        self.resend: OrderedDict[int, tuple[int, int, int, bytes | None]] = OrderedDict()
        self.newest_corr: int | None = None
        self.conns: dict[int, Connection] = {}
        self.resources: dict[int, str] = {}            # number -> "connection" / "stream": one u16 space (core §9)
        self._next_resource = 1
        self._order = 0
        self.streams: dict[int, Stream] = {}           # console stream id -> stream
        self.stream_keys: dict[tuple[int, int], int] = {}   # (connection, mechanism) -> stream id
        self.stream_places: dict[int, tuple[int, tuple[int, int]]] = {}   # stream id -> (wire fn, pin pair) it was on
        self.stream_order: dict[int, int] = {}         # stream id -> creation order (the streams list's order)
        self.gpio_modes: dict[int, int] = {}
        self.gpio_inputs: dict[int, int] = {}
        self.gpio_log: list[tuple[int, int]] = []
        self.gpio_drive: dict[int, int] = {}           # channel -> the level it is driven at in mode 3 / 4 (fixture §1.1)
        self.parked_drive: dict[int, int] = {}         # channel -> the level a free pin's output idle drives at
        self.lock_taken = False                        # a session has taken the lock since boot (probe.config §3.1)
        self.reset_retried: set[int] = set()           # slots that had their retry with reset this boot
        self.slot_reset_log: list[tuple[int, int, int]] = []   # (slot, channel, hold_ms) of every retry with reset
        self.uarts: dict[int, Stream] = {}             # fn -> stream (while its plan has RX or TX)
        self.uart_carry: dict[int, tuple[int, int]] = {}   # fn -> (position, mark serial) a released stream left off at
        self.uart_baud: dict[int, tuple[int, int, int]] = {}   # fn -> (baud, format, uart_configured) in force
        self.uart_clock_hz = 80_000_000                # the UARTs' divider clock (a test lowers it: the item's fallback)
        self.uart_session_cfg: set[int] = set()        # fns a session's configure set (it beats the uart item)
        self.uart_tx: dict[int, bytearray] = {}        # what a serial port's raw bytes sent out on a fixture UART
        self.i2c: dict[int, I2cState] = {fn: I2cState() for fn, n in self.names.items() if n == _I2C.name}
        self.spi: dict[int, SpiState] = {fn: SpiState() for fn, n in self.names.items() if n == _SPI.name}
        self.config: dict[tuple[int, int], bytes | list[bytes]] = {}   # (item tag, key) -> value (plan: list)
        self.saved: dict | None = getattr(self, "saved", None)
        self.saved_ids: dict[int, tuple] = getattr(self, "saved_ids", {})   # saved fn -> (name, instance, revision)
        self.saved_reason = 0                          # why the saved settings were not applied (probe.config §4)
        self.slots: dict[int, Slot] = {}
        self.binds: dict[int, Bind] = {}
        self.slot_rt: dict[int, SlotRuntime] = {}
        self.selected: dict[int, int] = {}            # port -> selected index (last-reset / manual)
        self.flows: dict[tuple[int, tuple[int, int]], Flow] = {}
        self.mixed_out: dict[int, bytearray] = {}
        self.held_ports: set[int] = set()
        self.session_resets: dict[tuple[int, int], tuple[object, int]] = {}   # stream key -> (sid, position)
        self.parked: dict[int, int] = {}               # channel -> the idle mode the probe put the free pin in
        self.speed_state = "base"                      # port_speed: "base", "try" or "committed" (core §3.5)
        self.speed_port: int | None = None             # the port off its boot speed
        self.speed_rate = 0                            # the rate it runs at
        self.speed_asked = 0                           # the baud the try asked (the commit names it again)
        self.speed_until_ms = 0                        # try: the commit's deadline (verify_ms)
        self.speed_idle_ms = 0                         # committed: revert after this long with no good frame
        self.speed_good_ms = 0                         # the last good frame on that port
        self.speed_heard = False                       # try: a good frame came at the new speed (before it, no broken counts)
        self.speed_bad = 0                             # committed: broken candidates in a row since the last good frame
        self.speed_pending: tuple | None = None        # ("switch", port, baud, verify_ms) / ("revert",): after the answer
        self.speed_log: list[tuple[int, int]] = []     # (port, rate) every switch, reverts included
        if self.saved is not None:
            self._apply_saved()                        # the saved disable items first: those pins are never parked
        self._park(self._all_channels())               # then the free pins' idle state (probe.config §2 boot order)

    @property
    def disabled(self) -> set[int]:
        """The channels the settings' disable items take away (probe.config §1): never used, driven or configured."""
        return {key for tag, key in self.config if tag == ITEM["disable"]}

    def _refuse_disabled(self, channels, extra: bytes = b"") -> None:
        """A request naming a disabled channel: rejected unavailable cause 5 (held by settings) with the channel."""
        for ch in channels:
            if ch != 0xFFFF and ch in self.disabled:
                raise unavailable("held_by_settings", ch, holder_kind="disabled", extra=extra)

    def _all_channels(self) -> set[int]:
        """Every channel some fn's describe offers (role_channels, channel_group, the wires' pairs and reset lines)."""
        out = {ch for fn in self.static if fn != m.CORE_FN for ch in self._declared_channels(fn)}
        out |= {p for pairs in self.pairs.values() for pair in pairs for p in pair}
        out |= {ch for chs in self.reset_channels.values() for ch in chs}
        out.discard(0xFFFF)
        return out

    def _park(self, channels) -> None:
        """Free pins go to their idle state (the idle item, else Hi-Z) at boot and whenever released (probe.config §1);
        a disabled channel stays as the reset left it (never parked), one a plan or a connection holds is not free (a
        slot's pins without a connection are: the idle is the state of a pin neither uses, probe.config §1)."""
        busy = {a[2] for a in self.plan if not self._listens(a[0])} | {p for c in self.conns.values() for p in c.pair}
        for ch in channels:
            if ch == 0xFFFF or ch in self.disabled or ch in busy:
                continue
            item = self.config.get((ITEM["idle"], ch))
            self.parked[ch] = item[2] if item else 0               # 0 = Hi-Z
            level = self._idle_level(ch)
            if level is None:
                self.parked_drive.pop(ch, None)
            else:
                self.parked_drive[ch] = level                      # an output idle drives at the idle's strength

    def _drive_level(self, kind: int, value: int) -> int | None:
        """The level a strength specification (fixture §1.1) picks: kind 0 the level number (None: not a level),
        kind 1 the strongest level of value mA or less (level 0 when every level is stronger)."""
        default, ma = self.drive_levels
        if kind == DRIVE_KIND["level"]:
            return value if value < len(ma) else None
        return max((i for i, x in enumerate(ma) if x <= value), default=0)

    def _idle_level(self, ch: int) -> int | None:
        """The level the idle state of `ch` drives at: None when it is not an output idle (or the probe declares no
        drive_levels); the idle's drive when it has one, else the default level (fixture §1.1)."""
        item = self.config.get((ITEM["idle"], ch))
        if self.drive_levels is None or not item or item[2] not in OUTPUT_MODES:
            return None
        if len(item) >= 6:
            level = self._drive_level(item[3], struct.unpack_from("<H", item, 4)[0])
            if level is not None:
                return level
        return self.drive_levels[0]

    @property
    def target_id(self) -> int | None:
        return self.target.target_id

    @target_id.setter
    def target_id(self, value: int | None) -> None:
        self.target.target_id = value

    @staticmethod
    def _capture_from(tlvs: list[bytes]) -> fake_capture.FakeCapture:
        """A capture as its describe declares it (modes, the w allowed, the rate range, max_read, the ring)."""
        d = reg.FIXTURE_ANALOG.tlv["describe"]                     # the logic's tags are the same numbers
        modes, widths, lo, hi, ring, most, fronts = set(), {8}, 1, 1_000_000, 1, 1024, {}
        for t in tlvs:
            tag, v = t[0], t[2:2 + t[1]]
            if tag == d["frontend"]:                               # analog: frontend min_mv max_mv attenuation_mdb
                fe, lo_mv, hi_mv, mdb = struct.unpack("<BiiI", v)
                fronts[fe] = (lo_mv, hi_mv, mdb)
            elif tag == d["mode"]:
                modes.add(v[0])
            elif tag == d["channels"]:
                layouts = struct.unpack_from("<I", v, 1)[0]        # max(u8) layouts(u32 bit set)
                widths = {1 << i for i in range(8) if layouts >> i & 1}
            elif tag == catalog.MIN_CLOCK_HZ:
                lo = struct.unpack("<I", v)[0]
            elif tag == catalog.MAX_CLOCK_HZ:
                hi = struct.unpack("<I", v)[0]
            elif tag == d["segment_ring"]:
                ring = struct.unpack("<H", v)[0]
            elif tag == d["max_read"]:
                most = struct.unpack("<I", v)[0]
        return fake_capture.FakeCapture(modes or {fake_capture.MODE["one_shot"]}, widths, lo, hi, ring, most, fronts)

    @staticmethod
    def _group_from(tlvs: list[bytes]) -> fake_capture.FakeGroup:
        d = reg.FIXTURE_CAPTURE_GROUP.tlv["describe"]
        tracks, most, budgets = [], 1, []
        for t in tlvs:
            tag, v = t[0], t[2:2 + t[1]]
            if tag == d["tracks"]:                                 # n(u8) n x fn(u16)
                tracks = list(struct.unpack_from(f"<{v[0]}H", v, 1))
            elif tag == d["max_tracks"]:
                most = v[0]
            elif tag == d["budget"]:                               # max_sps(u32) n(u8) n x fn(u16)
                budgets.append((struct.unpack_from("<I", v)[0], list(struct.unpack_from(f"<{v[4]}H", v, 5))))
        return fake_capture.FakeGroup(tracks, most, budgets)

    def _next_seq(self, fn: int) -> int:
        seq = self.push_seq.get(fn, 0)
        self.push_seq[fn] = (seq + 1) & 0xFFFF
        return seq

    def _events(self, fn: int, events: list[bytes]) -> None:
        """Events (kind(u8) payload) of `fn` to go out while it is subscribed; unsubscribed, they are not sent."""
        for e in events:
            if fn in self.subscribed:
                self.outbox.append(bytes([m.ROLE_EVENT]) + struct.pack("<HH", fn, self._next_seq(fn)) + e)

    def pushes(self) -> list[bytes]:
        """The frames the probe sends by itself now (core §11): fn 0's heartbeat, events, and a streaming capture's
        data. A serving loop frames and sends them; a test takes them from here."""
        self.tick()
        if m.CORE_FN in self.subscribed and self.now() >= self.next_heartbeat_ms:
            self.next_heartbeat_ms = self.now() + self.heartbeat_ms
            self._events(m.CORE_FN, [bytes([reg.CORE.event["heartbeat"]]) + struct.pack("<IQ", self.boot_id, self.now_ns())])
        for fn, cap in self.captures.items():
            if fn in self.subscribed:
                self.outbox += cap.pushes(fn, lambda fn=fn: self._next_seq(fn), self.probe.max_frame)
        out, self.outbox = self.outbox, []
        return out

    def _capture(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        cap, O = self.captures[fn], fake_capture.OP
        self._events(fn, cap.tick(self.now()))                     # what the clock captured up to this request
        roles = sorted(a[1] for a in self.plan if a[0] == fn)
        budget = self.probe.max_frame - m.RESULT_HEADER
        try:
            if op in (O["configure"], O["query"]):
                rest = t.data[t.at:]
                analog = self.names[fn] == "oep.fixture.analog"
                frontend_tag = fake_capture.ANA.tlv["configure"]["frontend"]
                known = set(fake_capture.TLV.values()) | ({frontend_tag} if analog else set())
                got, _ = t.tail(known)
                # the frontend TLV comes once per channel: read them all
                fronts = [tuple(v[:2]) for tag, v in m.split_tlvs(rest) if tag & 0x7F == frontend_tag] if analog else []
                settled = cap.settle(got, t.critical, len(roles), fronts)
                if op == O["configure"]:
                    cap.apply(settled)
                    cap.slipped = self.capture_slipped
                return self._answer(cap.answer(settled))
            if op in (O["start"], O["stop"], O["force"]) and cap.group is not None:
                raise unavailable("bound_in_group", holder_fn=next(g for g, grp in self.groups.items() if grp is cap.group))
            if op == O["start"]:
                t.tail()
                self._events(fn, cap.start(self.now(), subscribed=fn in self.subscribed))
                return m.COMPLETED, m.SUCCESS, struct.pack("<II", 0, cap.generation)   # blocking_ms generation
            if op == O["stop"]:
                t.tail()
                self._events(fn, cap.stop())
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["force"]:
                t.tail()
                return m.COMPLETED, m.SUCCESS, b""                  # nothing waits: a trigger is found at start
            if op == O["status"]:
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.status()
            if op == O["read"]:
                generation, position, most = t.take("IQI")           # generation position max (§3.2)
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.read(generation, position, most, budget)
            if op == O["segments"]:
                first = t.take("I")
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.segment_list(first, budget)
            if op == O["release"]:
                generation, serial = t.take("II")
                t.tail()
                cap.release(generation, serial, self.now())
                return m.COMPLETED, m.SUCCESS, b""
            if op == fake_capture.ANA.op["calibration"] and cap.analog:
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.calibration()
        except fake_capture.Reject as e:
            raise Reject(e.reason, e.payload)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _group(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        grp, O = self.groups[fn], fake_capture.GRP.op
        try:
            if op == O["bind"]:
                n = t.take("B")
                fns = [t.take("H") for _ in range(n)]
                got, _ = t.tail({fake_capture.GRP.tlv["bind"]["trigger_track"]})
                src = got.get(fake_capture.GRP.tlv["bind"]["trigger_track"])
                grp.bind(self.captures, fns, struct.unpack("<H", src)[0] if src else 0)
                return self._answer(b"")
            if op == O["start"]:
                t.tail()
                per, own = grp.start(self.captures, self.now(), lambda track: track in self.subscribed)
                for track, events in per:
                    self._events(track, events)
                self._events(fn, own)
                return m.COMPLETED, m.SUCCESS, struct.pack("<IQ", 0, grp.start_ns) + grp.generations(self.captures)
            if op == O["stop"]:
                t.tail()
                per, own = grp.stop(self.captures)
                for track, events in per:
                    self._events(track, events)
                self._events(fn, own)
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["force"]:
                t.tail()
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["status"]:
                t.tail()
                for track in grp.tracks:
                    self._events(track, self.captures[track].tick(self.now()))
                return m.COMPLETED, m.SUCCESS, grp.status(self.captures)
        except fake_capture.Reject as e:
            raise Reject(e.reason, e.payload)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _role_pairs(self, fn: int) -> list[tuple[int, int]]:
        """A role_channels wire's pairs in the count = 0 order (oep-if-debug §1): swdio ascending, then swclk."""
        roles = self.pin_roles[fn]
        if self.names[fn] == "oep.wire.swio":
            return [(d, 0xFFFF) for d in sorted(roles.get(1, ()))]
        return [(d, c) for d in sorted(roles.get(1, ())) for c in sorted(roles.get(2, ())) if d != c]

    def _allowed_pairs(self, fn: int) -> list[tuple[int, int]]:
        return self._role_pairs(fn) if fn in self.pin_roles else self.pairs.get(fn, [])

    def _allows(self, fn: int, pair: tuple[int, int]) -> bool:
        if fn in self.pin_roles:
            roles = self.pin_roles[fn]
            swio = self.names[fn] == "oep.wire.swio"
            return pair[0] in roles.get(1, ()) and (pair[1] == 0xFFFF if swio else
                                                    pair[1] in roles.get(2, ()) and pair[1] != pair[0])
        return pair in self.pairs.get(fn, [])

    def _held(self, fn: int | None = None, pair: tuple[int, int] | None = None) -> set[int]:
        """Channels something holds (core §8.1): the plan, the slots' pairs, the live connections' pairs - except
        wire `fn`'s own connection on `pair` (attaching there again, or scanning through it)."""
        held = {a[2] for a in self.plan if not self._listens(a[0])}   # a logic capture only listens
        held |= {p for s in self.slots.values() for p in s.pair if p != 0xFFFF and not (s.wire_fn == fn and s.pair == pair)}
        held |= {p for c in self.conns.values() for p in c.pair if p != 0xFFFF and not (c.fn == fn and c.pair == pair)}
        return held

    def _target(self, fn: int, pair: tuple[int, int]) -> FakeTarget:
        tg = self.targets.get((fn, pair))
        if tg is None:                                             # a pair nothing is wired to
            tg = self.targets[(fn, pair)] = FakeTarget(present=False)
        return tg

    @staticmethod
    def _group_pair(name: str, t: bytes) -> tuple[int, int]:
        roles = dict(catalog.unpack_channel_group(t[2:2 + t[1]])[1])
        return roles.get(1, 0xFFFF), roles.get(2, 0xFFFF) if name != "oep.wire.swio" else 0xFFFF

    def now_ns(self) -> int:
        """The probe's one clock (core §2.6a): ns since boot."""
        return self.now() * 1_000_000

    def _new_resource(self, kind: str) -> int:
        """The next free resource number (core §9): one u16 space for connections and streams, 1 .. 65535 then 1 again,
        never a number still in use."""
        for _ in range(0xFFFF):
            n = self._next_resource
            self._next_resource = n % 0xFFFF + 1
            if n not in self.resources:
                self.resources[n] = kind
                return n
        raise unavailable("limit")

    def _connection(self, cid: int) -> Connection:
        """The connection `cid`, or rejected: no_connection (unknown), unavailable cause 6 (a stream's number)."""
        c = self.conns.get(cid)
        if c is None:
            raise wrong_kind() if cid in self.resources else Reject(m.NO_CONNECTION)
        return c

    def _stream(self, sid: int) -> Stream:
        s = self.streams.get(sid)
        if s is None:
            raise wrong_kind() if sid in self.resources else Reject(m.NO_CONNECTION)
        return s

    # ---- the one entry point: a request message in, a result message out ------------------------
    def handle(self, data: bytes, transport: int = 0) -> bytes | None:
        """A request from transport `transport` (the index in the describe's transport list) -> its result."""
        if self.revision == 0 and data and data[0] & m.ROLE_SESSION:
            self.dropped += 1                                     # a v0 probe: unknown role, no answer
            return None
        req = m.Request.unpack(data)
        self.requests.append(req)
        self._transport = transport
        remembered = self._resent(req)
        if remembered is not None:
            return remembered
        try:
            res, detail, payload = self._dispatch(req)
        except Reject as r:
            res, detail, payload = m.REJECTED, r.reason, r.payload
        if res == m.REJECTED and detail == m.UNSUPPORTED and not payload:
            payload = bytes([m.TAG_FIXED])                         # core §4.3: unsupported is always tag [TLV]
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
        if transport not in self.serial_ports:
            self.speed_after_answer()                              # the answer is not on a port whose speed changes
        return out

    def _interface(self, fn: int):
        return reg.INTERFACES.get(self.names.get(fn, ""))

    def _closed_tail(self, fn: int, op: int) -> bool:
        """Answers the test `tail` is not appended to: link_source (the one closed tail, core §12), and the answers
        that are TLV lists themselves - describe and probe.config's get (their own meta TLVs are 0x3F / 0x7E)."""
        i = self._interface(fn)
        if fn == m.CORE_FN and op == m.OP_DESCRIBE:
            return True
        if self.names.get(fn) == "oep.probe.config" and op == _CFG.op["get"]:
            return True
        return bool(i and op in i.closed_tail)

    def _lock_free(self, fn: int, op: int) -> bool:
        i = self._interface(fn)
        if fn == m.CORE_FN:
            return op in reg.CORE.lock_free
        if i is None or self.names.get(fn) not in SIMS:
            return op == TOY_READ
        return op in i.lock_free

    @staticmethod
    def _answer(payload: bytes, detail: int = m.SUCCESS) -> tuple[int, int, bytes]:
        """A completed result (`_dispatch` appends the request's ignored tags)."""
        return m.COMPLETED, detail, payload

    def _dispatch(self, req: m.Request) -> tuple[int, int, bytes]:
        """Routes a request; a completed answer - failed and partial ones too - gets the ignored TLV (0x7F) of the tags
        its request's tail ignored (core §2.3), for every op in one place."""
        t = Take(req.payload)
        res, detail, payload = self._route(req, t)
        if res == m.COMPLETED and t.ignored:
            payload += bytes([m.TAG_IGNORED, len(t.ignored)]) + bytes(t.ignored)
        return res, detail, payload

    def _route(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        self._lapse()
        if req.fn == m.CORE_FN and req.op == m.OP_CONFIRM:
            return self._confirm(t)
        if req.fn == m.CORE_FN and req.op == m.OP_OPEN:
            return self._open(t)
        if req.fn != m.CORE_FN and req.fn not in self.names:
            return m.REJECTED, m.UNKNOWN_FUNCTION, b""
        if req.fn == m.CORE_FN and req.op == OP_PORT_SPEED and self.port_speed_base is None:
            return m.REJECTED, m.UNKNOWN_OPERATION, b""            # the optional feature off (core §3.5)
        if req.fn in self.i2c and req.op == _I2C.op["stretch"] and not self.target_decl[req.fn][1] & I2C_FEATURES["stretch"]:
            return m.REJECTED, m.UNKNOWN_OPERATION, b""            # stretch is features bit1's (fixture §3)
        if not self._lock_free(req.fn, req.op):
            refused = self._check(req.session)
            if refused:
                return refused
        if req.fn == m.CORE_FN:
            return self._core(req, t)
        sim = SIMS.get(self.names[req.fn])
        if sim is not None:
            return getattr(self, f"_{sim}")(req.fn, req.op, t)
        return self._toy(req, t)

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
    def _toy(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        if req.op == TOY_READ:
            t.tail()
            return self._answer(struct.pack("<I", self.values.get(req.fn, 0)))
        if req.op == TOY_WRITE:
            value = t.take("I")
            t.tail()
            self.values[req.fn] = value
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- the lock -------------------------------------------------------------------------------
    def _lapse(self) -> None:
        if self.holder is not None and self.now() >= self.expires_ms:
            self._release_lock(taken=True)                         # the lock goes, the last id stays

    def _release_lock(self, taken: bool) -> None:
        """end (taken False) keeps the session's resources for the next open; a lapse or force (taken True) sweeps
        them (core §9). After a lapse the id's next request is expired; after a force the forcing id is the last one,
        so the old id meets locked, then no_session (core §4.3 0x0E)."""
        self.holder = None
        self.last_swept = taken
        self.subscribed.clear()                                    # subscriptions end with the lock
        if taken:
            for fn in {a[0] for a in self.plan} - self.plan_from_config:
                self._drop_plan(fn)
            for sid, st in list(self.streams.items()):
                if "host" in st.users:
                    self._drop_stream_user(sid, "host", MARK_CLOSED["expired"])
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
                if self.last_swept:                                # its lease lapsed and swept it: open again (core §6.2)
                    return m.REJECTED, m.EXPIRED, b""
                self.holder = session                              # resume: nobody else came in between
                self.expires_ms = self.now() + self.lease_ms       # the lease of its last open
                return None
            return m.REJECTED, m.NO_SESSION, b""
        if session == self.holder:
            return None
        return self._locked()

    def _open(self, t: Take) -> tuple[int, int, bytes]:
        session, lease, force = t.take("IIB")
        got, _ = t.tail({OWNER})
        owner = got.get(OWNER)
        if owner is not None and not 1 <= len(owner) <= 32:
            t.refuse(OWNER, got)
            owner = None
        if self.holder is not None and self.holder != session:
            if not force:
                return self._locked()
            self._release_lock(taken=True)                         # force: the old session is cleaned up first
        resumed_codes = reg.CORE.enum["resumed"]
        if session == self.holder:
            resumed = resumed_codes["resumed"]                     # held: the lease is made anew, subscriptions stay
        elif session == self.last:
            resumed = resumed_codes["swept"] if self.last_swept else resumed_codes["resumed"]
        else:
            resumed = resumed_codes["new"]
        if session != self.holder:
            self.subscribed.clear()
        if session != self.last:
            self.owner = None
        if owner is not None:
            self.owner = owner
        self.holder = self.last = session
        self.last_swept = False
        self.lock_taken = True                                     # no retry with reset after this, this boot (§3.1)
        self.resend.clear()
        self.newest_corr = None
        if lease == 0:
            self.lease_ms = self.lease_default_ms
        else:
            self.lease_ms = min(lease, max(self.lease_max_ms, 60000))
        self.expires_ms = self.now() + self.lease_ms
        return self._answer(struct.pack("<IIB", self.lease_ms, self.boot_id, resumed))

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
    def _confirm(self, t: Take) -> tuple[int, int, bytes]:
        magic, lo, hi = t.bytes(4), *t.take("BB")
        if magic != m.CONFIRM_REQUEST:
            return m.REJECTED, m.MALFORMED, b""
        if self.revision == 0:                                     # v0 shape: max_frame(16) window(16) inflight flags
            return m.COMPLETED, m.SUCCESS, struct.pack("<4sBHHBB", m.CONFIRM_RESULT, 0, self.probe.max_frame,
                                                       min(self.window, 0xFFFF), self.max_inflight, 0)
        t.tail()
        if not lo <= self.revision <= hi:
            return m.REJECTED, m.UNSUPPORTED, b""
        return self._answer(struct.pack("<4sBBHIBI", m.CONFIRM_RESULT, self.revision, 0, self.probe.max_frame,
                                        self.window, self.max_inflight, self.boot_id))   # boot_id (core §7.1)

    def _core(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        op = req.op
        if op == m.OP_LIST:                                         # flags(u8) first(u16) prefix_len(u8) prefix [TLV]
            t.take("BH")
            t.bytes(t.take("B"))
            t.tail()
            try:
                return m.COMPLETED, m.SUCCESS, self.probe.call(m.CORE_FN, op, req.payload)
            except ValueError:
                return m.REJECTED, m.MALFORMED, b""
        if op == m.OP_DESCRIBE:
            fn, first = t.take("HH")
            t.no_tail()                                            # no TLV in a describe request (core §7.3)
            if fn not in self.names:
                return m.REJECTED, m.UNKNOWN_FUNCTION, b""         # an fn the probe does not offer (core §4.3)
            return m.COMPLETED, m.SUCCESS, self._page(self._declarations(fn), first)
        if op == m.OP_LOCK_STATE:
            t.tail()
            owner = (m.tlv(reg.CORE.tlv["lock_state_answer"]["owner"], self.owner)
                     if self.owner and self.holder is not None else b"")
            return self._answer(struct.pack("<BI", int(self.holder is not None), self._remaining()) + owner)
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
            t.tail()
            return self._answer(b"")
        if op == OP_PORT_SPEED:
            return self._port_speed(t)
        if op == m.OP_SUBSCRIBE:
            fn, _min_bytes, max_delay_ms = t.take("HHI")           # max_delay_ms u32 (core §11.3)
            t.tail()
            if fn != m.CORE_FN and fn not in self.names:
                return m.REJECTED, m.UNKNOWN_FUNCTION, b""
            if fn != m.CORE_FN and fn not in self.captures and fn not in self.groups:
                raise Reject(m.UNSUPPORTED)                        # an fn that emits nothing (core §11.3)
            self.subscribed.add(fn)
            self.push_seq[fn] = 0                                  # seq from 0 at every subscribe (core §11.2)
            if fn == m.CORE_FN:
                self.heartbeat_ms = max_delay_ms or reg.TIMING["heartbeat_default_ms"]
                self.next_heartbeat_ms = self.now() + self.heartbeat_ms
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_UNSUBSCRIBE:
            fn = t.take("H")
            t.tail()
            self.subscribed.discard(fn)
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_PLAN_APPLY:
            got = []
            try:
                tlvs = m.split_tlvs(req.payload) if req.payload else []
            except m.ProtocolError:
                raise Reject(m.MALFORMED) from None
            for tag, value in tlvs:
                if tag == reg.CORE.tlv["plan_apply"]["role_assignment"]:
                    if len(value) != 5:
                        raise Reject(m.MALFORMED)
                    got.append(struct.unpack_from("<HBH", value))
                elif tag in (m.TAG_INVALID, m.TAG_IGNORED, m.TAG_FIXED):
                    raise Reject(m.MALFORMED)                      # as in every tail (core §2.3)
                elif tag & m.TAG_CRITICAL:
                    return m.REJECTED, m.UNSUPPORTED, bytes([tag])
                else:
                    t.ignored.append(tag)                          # unknown non-critical: listed (core §2.3)
            if len(set(got)) != len(got) or any(fn == m.CORE_FN for fn, _, _ in got):
                raise Reject(m.MALFORMED)                          # the same (fn, role, channel) twice, or fn 0 (core §8)
            named = {fn for fn, _, _ in got}
            if any(fn not in self.names for fn in named):
                raise Reject(m.UNKNOWN_FUNCTION)
            self._check_target_roles(got)                          # malformed before held_by_settings (core §4.3)
            if named & self.plan_from_config:                       # the settings' plan is the settings' (core §8)
                raise unavailable("held_by_settings", holder_fn=min(named & self.plan_from_config),
                                  holder_kind="settings_plan")
            self._check_plan(got)
            self._refuse_disabled(ch for _, _, ch in got)          # a disabled channel (probe.config §1): cause 5
            if self.plan_roles is not None and len([a for a in self.plan if a[0] not in named]) + len(got) > self.plan_roles:
                raise unavailable("limit")                          # plan_roles (core §8)
            self._replace_plans(named, got)
            for fn in named:
                self._uart_plan_changed(fn)
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_PLAN_RELEASE:                                 # n(u8) n x fn(u16); n = 0: every fn
            n = t.take("B")
            fns = {t.take("H") for _ in range(n)}
            t.tail()
            for fn in {a[0] for a in self.plan if not fns or a[0] in fns} - self.plan_from_config:
                self._drop_plan(fn)                                 # the settings' plans stay, n = 0 too (core §8)
            return m.COMPLETED, m.SUCCESS, b""
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _listens(self, fn: int) -> bool:
        """A logic capture only listens: it shares pins with anything (core §8.1). The analog does not: its pads go to
        their analog function, cutting their digital input and output (oep-if-capture §1.2), as on this library's ESP32s."""
        return fn in self.captures and not self.captures[fn].analog

    def _check_target_roles(self, got: list[tuple[int, int, int]]) -> None:
        """An i2c-target / spi-target plan holds each of its roles exactly once, on distinct channels (fixture §3 /
        §4): a missing role, a role twice or two roles on one channel -> malformed. A role the fn does not define is
        left to the declaration check (unsupported)."""
        for fn in {f for f, _, _ in got if f in self.i2c or f in self.spi}:
            roles = TARGET_ROLES[self.names[fn]]
            mine = [(role, ch) for f, role, ch in got if f == fn and role in roles]
            if sorted(r for r, _ in mine) != sorted(roles) or len({ch for _, ch in mine}) != len(mine):
                raise Reject(m.MALFORMED)

    def _check_plan(self, got: list[tuple[int, int, int]]) -> None:
        """plan_apply's all-or-nothing check: the roles each fn has, and no pin another fn (or a slot) holds."""
        self._check_target_roles(got)
        named = {fn for fn, _, _ in got}
        others = {a for a in self.plan if a[0] not in named}
        kept = {a for a in others if not self._listens(a[0])}
        slot_pins = {p for s in self.slots.values() for p in s.pair if p != 0xFFFF}
        slot_pins |= {p for c in self.conns.values() for p in c.pair if p != 0xFFFF}   # a live connection's pins too
        analog = {fn for fn, cap in self.captures.items() if cap.analog}
        def not_declared(ch: int) -> Reject:
            # a role or channel the describe does not offer: unsupported, tag 0x90 + the channel (core §8)
            return Reject(m.UNSUPPORTED, bytes([reg.CORE.tlv["plan_apply"]["role_assignment"]])
                          + m.tlv(_UNA["channel"], struct.pack("<H", ch)))

        def held(ch: int, holder: int | None) -> Reject:
            if holder is not None:
                return unavailable("pin_in_use", ch, holder, "plan")
            slot = next((n for n, s in self.slots.items() if ch in s.pair), None)
            conn = next((c.fn for c in self.conns.values() if ch in c.pair), None)
            return unavailable("pin_in_use", ch, conn, "slot" if slot is not None else "connection")
        for fn, role, ch in got:
            beside = [f for f, _, c in got if c == ch and f != fn] + [a[0] for a in others if a[2] == ch]
            if fn in analog:   # the analog shares its pin with nothing: another fn's plan, a slot, a connection
                if role not in self._declared_roles(fn, ch):
                    raise not_declared(ch)
                if beside or ch in slot_pins:
                    raise held(ch, beside[0] if beside else None)
                continue
            if any(f in analog for f in beside):   # nor may anything come onto an analog pin
                raise held(ch, next(f for f in beside if f in analog))
            if fn in self.captures:
                if role not in self._declared_roles(fn, ch):
                    raise not_declared(ch)
                continue
            roles = TARGET_ROLES.get(self.names.get(fn, ""), set())
            if role not in roles or ch not in self._declared_channels(fn):
                raise not_declared(ch)
            if fn in self.i2c or fn in self.spi:                   # role_channels binds the roles it lists (core §7.4)
                listed = self._listed_roles(fn)
                if role in listed and role not in self._declared_roles(fn, ch):
                    raise not_declared(ch)
            if any(k[2] == ch for k in kept) or ch in slot_pins:
                raise held(ch, next((k[0] for k in kept if k[2] == ch), None))
        for fn in {f for f, _, _ in got if f in self.i2c or f in self.spi}:
            groups = [set(catalog.unpack_channel_group(t[2:2 + t[1]])[1]) for t in self.static[fn]
                      if t[0] == catalog.CHANNEL_GROUP]
            mine = {(role, ch) for f, role, ch in got if f == fn}
            if groups and mine not in groups:                      # one channel_group exactly (core §7.4)
                raise not_declared(min(ch for _, ch in mine))

    def _declared_roles(self, fn: int, channel: int) -> set[int]:
        """The roles fn's describe offers on `channel` (role_channels)."""
        return {t[2] for t in self.static[fn] if t[0] == catalog.ROLE_CHANNELS
                and channel in catalog.bitmap_to_channels(struct.unpack_from("<H", t, 3)[0], t[5:2 + t[1]])}

    def _listed_roles(self, fn: int) -> set[int]:
        """The roles fn's role_channels name (the only ones it binds, core §7.4)."""
        return {t[2] for t in self.static[fn] if t[0] == catalog.ROLE_CHANNELS}

    def _declared_channels(self, fn: int) -> set[int]:
        """Every channel fn's describe offers in any role (role_channels and channel_group)."""
        out = set()
        for t in self.static[fn]:
            if t[0] == catalog.ROLE_CHANNELS:
                out.update(catalog.bitmap_to_channels(struct.unpack_from("<H", t, 3)[0], t[5:2 + t[1]]))
            elif t[0] == catalog.CHANNEL_GROUP:
                out.update(c for _, c in catalog.unpack_channel_group(t[2:2 + t[1]])[1])
        return out

    def _replace_plans(self, fns: set[int], got) -> None:
        """The plans of `fns` become `got` as one change (core §8): a channel leaving goes to its idle state, one in
        both the old and the new plan of a gpio keeps its state and drive, and a new gpio line keeps the state it was
        in - an output idle keeps driving - until the first set (fixture §1); the idle modes 0-4 are the gpio modes of
        the same numbers."""
        old = {a for a in self.plan if a[0] in fns}
        kept = {ch for f, _, ch in old if self.names.get(f) == "oep.fixture.gpio"} & \
               {ch for f, _, ch in got if self.names.get(f) == "oep.fixture.gpio"}
        released = {a[2] for a in old} - {a[2] for a in got}
        for ch in {a[2] for a in old} - kept:
            self.gpio_modes.pop(ch, None)
            self.gpio_drive.pop(ch, None)
        self.plan = {a for a in self.plan if a[0] not in fns}
        self._park(released)
        self.plan |= set(got)
        for fn, _, ch in got:
            if ch not in kept and self.names.get(fn) == "oep.fixture.gpio" and self.parked.get(ch, 0):
                self.gpio_modes[ch] = self.parked[ch]
                if ch in self.parked_drive:                        # taken: the idle state's strength until a set
                    self.gpio_drive[ch] = self.parked_drive[ch]

    def _drop_plan(self, fn: int) -> None:
        released = set()
        for a in [a for a in self.plan if a[0] == fn]:
            self.gpio_modes.pop(a[2], None)
            self.gpio_drive.pop(a[2], None)
            self.plan.discard(a)
            released.add(a[2])
        self.plan_from_config.discard(fn)
        self._uart_plan_changed(fn)
        self._park(released)

    # ---- oep.fixture.uart's stream: made by the plan, gone with it (fixture §2) ---------------------
    def _uart_plan_changed(self, fn: int) -> None:
        """fn's plan moved: a fixture UART with RX or TX gets its stream (carrying on from where the last one ended,
        common §1.1) and the settings' uart item unless a session's configure holds; one without loses it. A fixture
        I2C / SPI target goes back to not configured (state 0): its configure is made on the plan's pins."""
        if fn in self.i2c:
            self.i2c[fn] = I2cState()
        if fn in self.spi:
            self.spi[fn] = SpiState()
        if self.names.get(fn) != "oep.fixture.uart":
            return
        planned = any(a[0] == fn for a in self.plan)
        s = self.uarts.get(fn)
        if planned and s is None:
            s = self.uarts[fn] = Stream()
            s.base, s.serial = self.uart_carry.get(fn, (0, 0))
            if fn not in self.uart_session_cfg:
                self._uart_apply_item(fn)
        elif not planned and s is not None:
            self.uart_carry[fn] = (s.end, s.serial)
            del self.uarts[fn]
            self.uart_baud.pop(fn, None)
            self.uart_session_cfg.discard(fn)

    def _uart_apply_item(self, fn: int) -> None:
        """The settings' uart item on a UART whose plan runs (fixture §2): the divider is made now; a baud it cannot
        make within 5 % falls back to the default 115200 8N1 with configured = item_fallback (3). No item: default."""
        item = self.config.get((ITEM["uart"], fn))
        if item is None:
            self.uart_baud.pop(fn, None)
            return
        _, baud, fmt = struct.unpack_from("<HIB", item)
        actual = self._uart_actual(baud)
        if abs(actual - baud) * 20 > baud:
            self.uart_baud[fn] = (115200, 0, UART_CONFIGURED["item_fallback"])
        else:
            self.uart_baud[fn] = (actual, fmt, UART_CONFIGURED["item"])

    def _uart_actual(self, baud: int) -> int:
        return self.uart_clock_hz // max(1, self.uart_clock_hz // baud)

    def _uart_check(self, baud: int, fmt: int, fn: int, fmt_tag: int | None, divide: bool = True) -> int:
        """fixture §2's refusals: baud 0 and undefined format bits -> malformed; a format the UART does not declare ->
        unsupported (the format TLV's tag as received, or 0x00 for the item); a baud off by more than 5 % ->
        unsupported 0x00. -> the actual baud. `divide` False (the settings' item at set time): the range alone
        (1 .. the UART's max_clock_hz), the divider is made when the plan runs the UART (-> item_fallback)."""
        if baud == 0 or fmt & ~UART_FORMAT_MASK or fmt & 3 > 1 or (fmt >> 2) & 3 > 2:
            raise Reject(m.MALFORMED)
        if fmt not in self.uart_formats.get(fn, {0}):
            raise Reject(m.UNSUPPORTED, bytes([fmt_tag if fmt_tag is not None else m.TAG_FIXED]))
        if not divide:
            if baud > self.uart_max_hz.get(fn, 3_000_000):
                raise Reject(m.UNSUPPORTED)
            return baud
        actual = self._uart_actual(baud)
        if abs(actual - baud) * 20 > baud:
            raise Reject(m.UNSUPPORTED)
        return actual

    # ---- describe: the static declarations plus the live ones -----------------------------------
    def _declarations(self, fn: int) -> list[bytes]:
        """describe: the profile's declarations as they are (core §7.3: nothing that changes - the firmware's labels
        only, the settings' are read by get; probe.config's state is its op state). fn 0's port_speed follows
        `port_speed_base` (a test turns the feature off or on)."""
        if fn == m.CORE_FN:
            out = [t for t in self.static[fn] if t[0] != PORT_SPEED_TAG]
            return out + [catalog.u8(PORT_SPEED_TAG, 1)] if self.port_speed_base is not None else out
        return list(self.static[fn])

    # ---- port_speed (core §3.5) -----------------------------------------------------------------
    def _port_speed(self, t: "Take") -> tuple[int, int, bytes]:
        """port(u8) baud(u32) step(u8) verify_ms(u16) idle_ms(u32) [TLV] -> baud(u32): the rate that applies."""
        port, baud, step, verify_ms, idle_ms = t.take("BIBHI")
        t.tail()
        if step not in SPEED_STEP.values():
            raise Reject(m.MALFORMED)                              # not a defined step (core §4.3 order 5)
        if port != self._transport or self.transports.get(port) != fake.TRANSPORT["uart_bridge"]:
            raise unavailable("wrong_state")                       # only the UART bridge the request came in on
        # the port's state (boot speed / trying / committed) decides which step fits (core §3.5): any other is cause 6
        if step == SPEED_STEP["try"]:
            if self.speed_state != "base":
                raise unavailable("wrong_state")                   # already trying or committed
            if not SPEED_RATES[0] <= baud <= SPEED_RATES[1]:
                raise unsupported_fixed()                          # a baud this UART cannot make
            self.speed_pending = ("switch", port, baud, verify_ms)
            return self._answer(struct.pack("<I", baud))
        if step == SPEED_STEP["commit"]:
            if self.speed_state != "try" or port != self.speed_port or baud != self.speed_asked:
                raise unavailable("wrong_state")                   # nothing tried, committed already, or another baud
            self.speed_state = "committed"
            self.speed_idle_ms = idle_ms if 0 < idle_ms <= SPEED_IDLE_MAX_MS else SPEED_IDLE_MAX_MS
            self.speed_good_ms = self.now()
            self.speed_bad = 0
            return self._answer(struct.pack("<I", self.speed_rate))
        if self.speed_state == "base":
            raise unavailable("wrong_state")                       # a revert at the boot speed: nothing to go back from
        self.speed_pending = ("revert",)
        return self._answer(struct.pack("<I", self.port_speed_base))

    def port_baud(self, port: int) -> int:
        """The rate serial port `port` runs at now (its boot speed, or what port_speed set)."""
        if self.speed_state != "base" and port == self.speed_port:
            return self.speed_rate
        return self.port_speed_base or 115200

    def breaks(self, port: int, size: int, to_host: bool, duplex: bool = False) -> bool:
        """The line model: a frame of `size` bytes on `port` at its rate now breaks (`broken_rates`). duplex: the other
        way carries a frame of the rate's min_size or more at the same time (a BrokenRate with `duplex` breaks only
        then)."""
        b = self.broken_rates.get(self.port_baud(port))
        if b is None:
            return False
        if b.carried < b.after:
            b.carried += size
            return False
        if size < b.min_size or not (b.to_host if to_host else b.to_probe) or (b.duplex and not duplex):
            return False
        return b.hit()

    def duplex_rate(self, port: int) -> BrokenRate | None:
        """The rate `port` runs at now when it breaks only both ways at once (fake_serial checks the unread answers)."""
        b = self.broken_rates.get(self.port_baud(port))
        return b if b is not None and b.duplex else None

    def speed_after_answer(self) -> None:
        """The answer that asked for a switch or a revert is out (at the old speed): now do it."""
        pending, self.speed_pending = self.speed_pending, None
        if pending is None:
            return
        if pending[0] == "revert":
            self._speed_revert()
            return
        _, port, baud, verify_ms = pending
        self.speed_state, self.speed_port, self.speed_rate, self.speed_asked = "try", port, baud, baud
        if baud in self.broken_rates:
            self.broken_rates[baud].carried = 0                    # `after` counts from this switch
        self.speed_until_ms = self.now() + verify_ms
        self.speed_good_ms = self.now()
        self.speed_heard = False
        self.speed_bad = 0
        self.speed_log.append((port, baud))

    def _speed_revert(self) -> None:
        if self.speed_state == "base":
            return
        self.speed_log.append((self.speed_port, self.port_speed_base))
        self.speed_state, self.speed_port, self.speed_rate, self.speed_asked = "base", None, 0, 0
        self.speed_heard, self.speed_bad = False, 0

    def speed_frame(self, port: int, good: bool) -> None:
        """A candidate closed on serial port `port`: a frame (good) or not (a broken candidate). core §3.5's conditions
        2 (trying: one broken candidate after the first good frame at the new speed; the ones before it are the
        switch-over's leftovers and do not count) and 4 (committed: SPEED_BAD_MAX broken candidates in a row with no
        good frame between)."""
        if self.speed_state == "base" or port != self.speed_port:
            return
        if good:
            self.speed_good_ms = self.now()
            self.speed_heard = True
            self.speed_bad = 0
            return
        if self.speed_state == "try":
            if self.speed_heard:
                self._speed_revert()                               # the new speed breaks frames: not this one
            return
        self.speed_bad += 1
        if self.speed_bad >= SPEED_BAD_MAX:
            self._speed_revert()

    def _speed_tick(self) -> None:
        # Condition 3 (committed, idle_ms with no good frame) is not counted while a request executes (core §3.5, as
        # the lease, §6.1). The fake handles every request within one call and its clock does not move meanwhile, so
        # there is nothing to pause here: a long-running request would need `speed_good_ms` set when its answer goes out.
        if self.speed_pending and self.speed_pending[0] == "revert":
            self.speed_after_answer()
        now = self.now()
        if self.speed_state == "try" and now >= self.speed_until_ms:
            self._speed_revert()                                   # no commit within verify_ms
        elif self.speed_state == "committed" and now - self.speed_good_ms >= self.speed_idle_ms:
            self._speed_revert()

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
        rvswd = self.names[fn] == "oep.wire.rvswd"
        if op == 0x01:                                             # scan: count(u8) pairs -> tried count found...
            count = t.take("B")
            pairs = [t.take("HH") for _ in range(count)]
            if not rvswd and any(p[1] != 0xFFFF for p in pairs):
                raise Reject(m.MALFORMED)                          # swio: one wire (debug §3)
            known = {_T_SCAN["max_speed"], _T_SCAN["skip"]} | ({_T_SCAN["idle_clock"]} if rvswd else set())
            got, _ = t.tail(known)
            skip_tlv = got.get(_T_SCAN["skip"])
            if skip_tlv is not None and (count or len(skip_tlv) != 2):
                raise Reject(m.MALFORMED)                          # skip goes with count 0 only
            if _T_SCAN["max_speed"] in got and len(got[_T_SCAN["max_speed"]]) != 4:
                raise Reject(m.MALFORMED)
            if _T_SCAN["idle_clock"] in got and (len(got[_T_SCAN["idle_clock"]]) != 1 or got[_T_SCAN["idle_clock"]][0] > 1):
                raise Reject(m.MALFORMED)
            live = [c.pair for c in self.conns.values() if c.fn == fn]
            full = len(live) >= self.max_connections.get(fn, 1)   # every seat taken: the live pairs only
            self._refuse_disabled(ch for p in pairs if self._allows(fn, p) for ch in p)   # cause 5 (probe.config §1)
            if any(not self._allows(fn, p) or set(p) & self._held(fn, p) or (full and p not in live) for p in pairs):
                raise Reject(m.UNAVAILABLE)                        # not allowed, held (§8.1), or no seat to try it on
            if not pairs:                                          # the count-0 list, from `skip` on (oep-if-debug §1)
                skip = struct.unpack("<H", skip_tlv)[0] if skip_tlv is not None else 0
                pairs = [p for p in self._allowed_pairs(fn) if not set(p) & self.disabled   # disabled: not listed
                         and not set(p) & self._held(fn, p) and (not full or p in live)][skip:]
            pairs = pairs[:255]                                    # tried is a u8
            found = []
            for p in pairs:
                tg = self._target(fn, p)
                if tg.answers or self._conn_at(fn, p) is not None:   # a live connection's pair: read over it, no restart
                    found.append(m.element(struct.pack("<BHHI", 1, *p, tg.dmstatus())))
            return m.COMPLETED, m.SUCCESS, struct.pack("<BB", len(pairs), len(found)) + b"".join(found)
        if op == 0x02:                                             # attach: method(u8) [TLV] (oep-if-debug §3)
            method = t.take("B")
            if method > 1:
                raise Reject(m.MALFORMED)                          # not a defined method
            known = {_T_ATTACH["max_speed"], _T_ATTACH["pins"], _T_ATTACH["reset"]} | ({_T_ATTACH["idle_clock"]} if rvswd else set())
            got, _ = t.tail(known)
            if _T_ATTACH["max_speed"] not in got:
                raise Reject(m.MALFORMED)                          # max_speed is required (§1)
            if len(got[_T_ATTACH["max_speed"]]) != 4:
                raise Reject(m.MALFORMED)
            idle_tlv = got.get(_T_ATTACH["idle_clock"])
            if idle_tlv is not None and (len(idle_tlv) != 1 or idle_tlv[0] > 1):
                raise Reject(m.MALFORMED)
            reset = got.get(_T_ATTACH["reset"])
            if reset is not None:
                if len(reset) != 4:
                    raise Reject(m.MALFORMED)
                channel, hold_ms = struct.unpack("<HH", reset)
                if channel not in self.reset_channels.get(fn, set()):
                    raise Reject(m.UNSUPPORTED, bytes([_T_ATTACH["reset"] | m.TAG_CRITICAL]))   # not a reset line here (§3)
                if hold_ms > self.max_op_ms:
                    raise Reject(m.UNSUPPORTED, bytes([_T_ATTACH["reset"] | m.TAG_CRITICAL]))   # longer than one request may take
                self._refuse_disabled([channel])                   # a disabled reset line: cause 5 (probe.config §1)
                if any(a[2] == channel for a in self.plan):
                    raise unavailable("pin_in_use", channel, next(a[0] for a in self.plan if a[2] == channel), "plan")
            idle_clock = idle_tlv[0] if idle_tlv is not None else 0
            pair = self._pick_pair(fn, got)
            tg = self._target(fn, pair)
            speed = min(4_000_000, struct.unpack("<I", got[_T_ATTACH["max_speed"]])[0])
            cid = self._conn_at(fn, pair)
            flags = 0
            if reset is not None and tg.resets_through(channel):
                tg.silent_until_reset = False                      # held in reset and let go: it answers again
            if cid is None:
                if not tg.answers:
                    return m.COMPLETED, m.FAILED, bytes([LINE])    # failed: status [TLV] (common §3)
                cid = self._seat(fn, pair, tg, speed)              # a failed attach consumed no number
                if tg.havereset:
                    tg.havereset, flags = False, flags | ATTACH_FLAGS["havereset_acked"]
            else:
                flags |= ATTACH_FLAGS["existing"]
                self.conns[cid].speed = min(self.conns[cid].speed, speed)
            c = self.conns[cid]
            c.idle_clock = idle_clock                              # an existing connection takes the new rest level
            c.users.add("host")
            for n, s in self.slots.items():                        # a new connection for an evicted slot: a new cue
                if s.wire_fn == fn and s.pair == pair:
                    self.slot_rt[n].evicted = False
            if reset is not None and tg.reset_line not in (None, channel):
                if method == 1:                                    # a line that resets nothing: halted where it ran
                    tg.halted, tg.dpc = True, tg.reset_vector + 0x2f8
            elif reset is not None:                                # the reset line held, then let go: a host reset
                tg.halted = method == 1
                tg.dpc = tg.reset_vector if method == 1 else tg.reset_vector + 0x200
                self._host_reset(cid, MARK_RESET["attach_reset"])
            elif method == 1:
                tg.halted = True
            tail = b"" if c.tid is None else m.tlv(_T_ATTACH_ANSWER["target_id"], bytes([1]) + struct.pack("<I", c.tid))
            if tg.halted:
                flags |= ATTACH_FLAGS["halted"]
                tail += m.tlv(_T_ATTACH_ANSWER["dpc"], struct.pack("<I", tg.dpc))
            self._refresh()
            return self._answer(struct.pack("<HIBI", cid, tg.dmstatus(), flags, c.speed) + tail)
        if op == 0x03:                                             # detach
            cid = t.take("H")
            got, _ = t.tail({_T_DETACH["force"]})
            c = self._connection(cid)
            if c.fn != fn:
                raise wrong_kind()                                 # another wire's connection
            c.users.discard("host")
            if _T_DETACH["force"] in got or not c.users:
                self._close_conn(cid, MARK["detach"])
            self._refresh()
            return m.COMPLETED, m.SUCCESS, b""
        if op == 0x05:                                             # connections (lock-free, paged by first: debug §2.1)
            first = t.take("B")
            t.tail()
            mine = sorted((c.order, cid) for cid, c in self.conns.items() if c.fn == fn)
            rows = []
            for _, cid in mine:
                c = self.conns[cid]
                users = (1 if "host" in c.users else 0) | (2 if any(u != "host" for u in c.users) else 0)
                slot = next((n for n, s in self.slots.items() if s.wire_fn == fn and s.pair == c.pair), NO_SLOT)
                tid = b"" if c.tid is None else struct.pack("<I", c.tid)
                rows.append(m.element(struct.pack("<HHHIBBBB", cid, *c.pair, c.speed, users, slot, 1 if tid else 0, len(tid)) + tid))
            return m.COMPLETED, m.SUCCESS, self._paged(rows, first)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _paged(self, rows: list[bytes], first: int) -> bytes:
        """more(u8) count(u8) the rows from `first` that fit the frame (connections, streams: core §2.3's lists)."""
        budget = self.probe.max_frame - m.RESULT_HEADER - 2
        out: list[bytes] = []
        for row in rows[first:]:
            if out and sum(map(len, out)) + len(row) > budget:
                break
            out.append(row)
        return bytes([int(first + len(out) < len(rows)), len(out)]) + b"".join(out)

    def _pick_pair(self, fn: int, got: dict[int, bytes]) -> tuple[int, int]:
        if 0x03 in got:
            if len(got[0x03]) != 4:
                raise Reject(m.MALFORMED)
            pair = struct.unpack("<HH", got[0x03])
            if not self._allows(fn, pair):
                raise Reject(m.UNSUPPORTED, bytes([_T_ATTACH["pins"] | m.TAG_CRITICAL]))   # not a pair this wire offers (core §4.3 order 6)
            self._refuse_disabled(pair)                            # a disabled channel: cause 5 (probe.config §1)
            if set(pair) & self._held(fn, pair):
                raise unavailable("pin_in_use", next(iter(set(pair) & self._held(fn, pair))))   # held (§8.1)
            return pair
        live = [c.pair for c in self.conns.values() if c.fn == fn]
        if len(live) == 1:
            return live[0]                                         # no pins: the wire's one live connection
        offered = self._allowed_pairs(fn)
        allowed = [p for p in offered if not set(p) & self.disabled]   # the probe never picks a disabled channel
        if not live and len(offered) == 1 and not allowed:
            self._refuse_disabled(offered[0])                      # its one pair is disabled: cause 5
        if live or len(allowed) != 1:
            raise Reject(m.UNAVAILABLE)                            # the host chooses among several
        return allowed[0]

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
        cid = self._new_resource("connection")                    # one u16 space with the streams (core §9)
        self._order += 1
        self.conns[cid] = Connection(fn, pair, self._order, speed, tg.target_id)
        return cid

    def _close_conn(self, cid: int, mark: int) -> None:
        """The connection goes; its streams close (mark `mark`, then closed 4) and stay readable (console §2)."""
        gone = self.conns.pop(cid, None)
        self.resources.pop(cid, None)
        if gone is not None:
            self._park(gone.pair)                                  # its pins are free again: their idle state
        for (c, _), sid in list(self.stream_keys.items()):
            if c == cid and not self.streams[sid].closed:
                self.streams[sid].add_mark(mark, self.now_ns())
                self._close_stream(sid, MARK_CLOSED["connection_closed"])

    def _target_of(self, cid: int) -> FakeTarget:
        c = self.conns[cid]
        return self._target(c.fn, c.pair)

    # ---- oep.target.riscv-dm --------------------------------------------------------------------
    @staticmethod
    def _outcome(status: int, done: int) -> int:
        return m.SUCCESS if status == OK else (m.PARTIAL if done else m.FAILED)

    def _dm(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        conn = t.take("H")
        self._connection(conn)                                     # no_connection, or unavailable 6 for a stream's number
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
            waits_us = sum(a if k == STEP["wait_us"] else a[3] if k == STEP["poll_us"] else 0 for k, a in steps)
            if waits_us > self.max_op_ms * 1000:
                raise Reject(m.UNSUPPORTED)                        # longer than one request may take (debug §4.1)
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
            return (m.COMPLETED, self._outcome(status, done),
                    struct.pack(f"<HBH{len(values)}I", done, status, len(values), *values))   # done status nvals values
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
            got, _ = t.tail({_RV.tlv["reset"]["method"]})
            if mode > 2:
                raise Reject(m.MALFORMED)                          # not a defined mode (core §4.3 order 5)
            method = got.get(_RV.tlv["reset"]["method"])
            if method is not None and (len(method) != 1 or method[0] > 2):
                t.refuse(_RV.tlv["reset"]["method"], got)
            tg.havereset = True
            tg.halted = mode == 2
            tg.dpc = tg.reset_vector if mode == 2 else tg.reset_vector + 0x200
            # debug §4.3: bit0 reached the mode's state, bit1 confirmed by the pc (mode 1), attempts 1
            flags, pc = (0b01, 0) if mode == 0 else ((0b11, tg.dpc) if mode == 1 else (0b01, tg.dpc))
            self._host_reset(conn, MARK_RESET["ndmreset"])
            return self._answer(struct.pack("<BBBI", OK, flags, 1, pc))
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
            if address % 4:
                raise Reject(m.MALFORMED)                          # not a word address (core §4.3 order 5)
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise unsupported_fixed()                          # past the declared max_length (bytes; debug §4.5)
            if not tg.halted:
                return m.COMPLETED, m.FAILED, struct.pack("<HB", 0, STATE)   # a running hart (debug §4.5)
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
            if address % 4:
                raise Reject(m.MALFORMED)
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise unsupported_fixed()                          # past the declared max_length (debug §4.5)
            words = struct.unpack(f"<{count}I", t.bytes(4 * count))
            t.tail()
            if not tg.halted:
                return m.COMPLETED, m.FAILED, struct.pack("<HB", 0, STATE)
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
            if timeout_ms == 0:
                raise Reject(m.MALFORMED)                          # 1 .. max_op_ms (debug §4.4)
            if timeout_ms > self.max_op_ms:
                raise Reject(m.UNSUPPORTED)
            if not tg.halted:                                      # the answer's shape is always the same: nvals 0
                return m.COMPLETED, m.FAILED, struct.pack("<BBIIB", STATE, 0, tg.dpc, 0, 0)
            tg.regs.update(regs)
            stopped, dpc, us = tg.run_hook(pc, tg.regs) if tg.run_hook else (True, pc + 0x10, 50)
            if not stopped and tg.unstoppable:                     # the limit passed and the hart would not halt
                return m.COMPLETED, m.FAILED, struct.pack("<BBIIB", TIMEOUT, RUN_STOPPED["not_halted"], 0, us, 0)
            tg.dpc = dpc
            status = OK if stopped else TIMEOUT
            values = [tg.regs.get(r, 0) for r in outs]
            code = RUN_STOPPED["stopped"] if stopped else RUN_STOPPED["timeout_halted"]
            return (m.COMPLETED, m.SUCCESS if stopped else m.FAILED,
                    struct.pack(f"<BBIIB{n_out}I", status, code, dpc, us, n_out, *values))   # ... nvals values
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _host_reset(self, cid: int, detail: int) -> None:
        """A reset last-reset counts (probe.config §1.2): riscv-dm reset (detail 1 ndmreset) or an attach's reset TLV
        (detail 3) on connection `cid`; its streams get mark reset."""
        now = self.now_ns()
        slots = [n for n, s in self.slots.items() if (s.wire_fn, s.pair) == (self.conns[cid].fn, self.conns[cid].pair)]
        for (c, _), sid in self.stream_keys.items():
            if c == cid and not self.streams[sid].closed:
                self.streams[sid].add_mark(MARK["reset"], now, detail)
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
                pos = hits[-1][1] if hits else s.end                 # no such mark: from now (common §1.2)
            else:
                raise Reject(m.UNSUPPORTED)
            flags = 0
            if pos < s.base:
                pos, flags = s.base, 2
            budget = self.probe.max_frame - m.RESULT_HEADER - 11
            data = bytes(s.data[pos - s.base:pos - s.base + min(mx, budget)])
            if pos + len(data) < s.end:
                flags |= 1
            return m.COMPLETED, m.SUCCESS, struct.pack("<QBH", pos, flags, len(data)) + data   # start flags len data
        if op == _CON.op["marks"]:
            frm = t.take("I")
            t.tail()
            hits = [mk for mk in s.marks if m.serial_diff(mk[0], frm) >= 0]
            page = hits[:self.MARKS_PER_ANSWER]
            body = struct.pack("<BB", int(len(hits) > len(page)), len(page))
            body += b"".join(m.element(struct.pack("<IQBQB", *mk)) for mk in page)   # time_ns u64 (common §1.3)
            return self._answer(body)
        if s.closed:
            raise unavailable("wrong_state")                       # a closed stream: read / marks only (console §2)
        if op == _CON.op["clear"]:
            t.tail()
            s.drop_oldest(len(s.data))
            s.add_mark(MARK["clear"], self.now_ns())
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["mark"]:
            value = t.take("B")
            t.tail()
            s.add_mark(MARK["host"], self.now_ns(), value)
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["write"]:
            count = t.take("H")
            data = t.bytes(count)
            t.tail()
            if count == 0:
                raise Reject(m.MALFORMED)
            took = min(count, accept)                              # what fit the slot; 0 = failed (common §1.4)
            s.written += data[:took]
            return self._answer(struct.pack("<H", took),
                                m.SUCCESS if took == count else m.PARTIAL if took else m.FAILED)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.target.console ---------------------------------------------------------------------
    def _console(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _CON.op["open"]:
            conn, mech = t.take("HB")
            t.tail()
            self._connection(conn)                                 # no_connection, or unavailable 6 for a stream's number
            if mech not in self.mechanisms:
                raise Reject(m.UNSUPPORTED)                        # not a mechanism this probe opens (0xFF included)
            sid, existing = self._open_stream(conn, mech, "host")
            return self._answer(struct.pack("<HB", sid, int(existing)))
        if op == _CON.op["streams"]:                               # lock-free, paged by first: every stream, live or readable
            first = t.take("B")
            t.tail()
            rows = []
            for sid in sorted(self.streams, key=lambda k: self.stream_order.get(k, 0)):
                s = self.streams[sid]
                users = (STREAM_USERS["host_session"] if "host" in s.users else 0) | \
                        (STREAM_USERS["slot"] if any(u != "host" for u in s.users) else 0)
                rows.append(m.element(struct.pack("<HHBBB", sid, s.conn, s.mechanism, users,
                                                  STREAM_STATE["closed"] if s.closed else STREAM_STATE["open"])))
            return m.COMPLETED, m.SUCCESS, self._paged(rows, first)
        sid = t.take("H")
        s = self._stream(sid)                                      # no_connection, or unavailable 6 for a connection's
        if op == _CON.op["close"]:
            t.tail()
            if not s.closed:
                self._drop_stream_user(sid, "host", MARK_CLOSED["all_released"])
            return m.COMPLETED, m.SUCCESS, b""                     # a closed stream: nothing, ok (console §1)
        return self._stream_op(s, op, t, self.console_accept)

    def _open_stream(self, conn: int, mech: int, user) -> tuple[int, bool]:
        """A stream for `user` on (conn, mech) (console §2): the live one of the pair; another mechanism's live stream
        on the connection -> unavailable 6; a closed one of the same place and mechanism comes back under its number
        (position and marks carry on); else a new number. -> (stream, existing)."""
        sid = self.stream_keys.get((conn, mech))
        if sid is not None and not self.streams[sid].closed:
            self.streams[sid].users.add(user)
            return sid, True
        live = [k for k, v in self.stream_keys.items() if k[0] == conn and not self.streams[v].closed]
        if live:
            raise unavailable("wrong_state")                       # one live stream per connection
        place = (self.conns[conn].fn, self.conns[conn].pair)
        again = None
        for key in [k for k, v in self.stream_keys.items()          # the closed ones of this place
                    if self.streams[v].closed and self.stream_places.get(v) == place]:
            old = self.stream_keys.pop(key)
            if key[1] == mech and again is None:
                again = old                                        # the same mechanism: it opens again as it was
            else:
                self.streams.pop(old, None)                        # another mechanism's: gone
                self.stream_places.pop(old, None)
                self.resources.pop(old, None)
        if again is not None:
            sid, s = again, self.streams[again]
            s.closed = False
        else:
            sid = self._new_resource("stream")                     # one u16 space with the connections (core §9)
            s = self.streams[sid] = Stream()
            self._order += 1
            self.stream_order[sid] = self._order                   # streams lists them in the order they were made
        s.conn, s.mechanism = conn, mech
        s.users = {user}
        self.stream_keys[(conn, mech)] = sid
        self.stream_places[sid] = place
        s.add_mark(MARK["attach"], self.now_ns())
        return sid, again is not None

    def _drop_stream_user(self, sid: int, user, detail: int) -> None:
        """One user lets go of the stream (close, a lapse, a slot change); nobody left closes it (mark closed)."""
        s = self.streams[sid]
        s.users.discard(user)
        if not s.users and not s.closed:
            self._close_stream(sid, detail)

    def _close_stream(self, sid: int, detail: int) -> None:
        s = self.streams[sid]
        s.add_mark(MARK["closed"], self.now_ns(), detail)
        s.closed = True
        s.users.clear()

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
            # drive (fixture §1.1): a probe without drive_levels does not know the tag (all of them ignored)
            got, _ = t.tail({GPIO_SET_DRIVE} if self.drive_levels is not None else set())
            index_tag = _GPIO.tlv["unavailable_payload"]["index"]
            for i, (ch, mode) in enumerate(pairs):                 # core §4.3's order: malformed, unsupported, unavailable
                if mode > 7:
                    raise Reject(m.MALFORMED)
            drives = self._set_drives(t, pairs)
            for i, (ch, mode) in enumerate(pairs):
                if not self.gpio_allowed.get(fn, 0xFF) >> mode & 1:   # a mode it does not drive (fixture §1)
                    raise unsupported_fixed(m.tlv(_UNA["channel"], struct.pack("<H", ch)), m.tlv(index_tag, bytes([i])))
            for i, (ch, mode) in enumerate(pairs):
                self._refuse_disabled([ch], m.tlv(index_tag, bytes([i])))   # disabled: cause 5 (probe.config §1)
                if ch not in mine:                                 # the position as the gpio's TLV (fixture §1)
                    raise unavailable(channel=ch, extra=m.tlv(index_tag, bytes([i])))
            for i, (ch, mode) in enumerate(pairs):
                self.gpio_modes[ch] = mode
                self.gpio_log.append((ch, mode))
                if mode in OUTPUT_MODES and self.drive_levels is not None:   # the effective strength (fixture §1.1)
                    level = drives.get(i)
                    if level is None:
                        level = self._idle_level(ch)
                    self.gpio_drive[ch] = self.drive_levels[0] if level is None else level
                else:
                    self.gpio_drive.pop(ch, None)
            return self._answer(b"")
        if op == _GPIO.op["read"]:
            n = t.take("B")
            chans = [t.take("H") for _ in range(n)]
            t.tail()
            for i, ch in enumerate(chans):
                self._refuse_disabled([ch], m.tlv(_GPIO.tlv["unavailable_payload"]["index"], bytes([i])))
                if ch not in mine:
                    raise unavailable(channel=ch, extra=m.tlv(_GPIO.tlv["unavailable_payload"]["index"], bytes([i])))
            levels = []
            for ch in chans:
                mode = self.gpio_modes.get(ch, 0)
                outside = self.gpio_world(ch, mode) if self.gpio_world and mode in (0, 1, 2, 6, 7) else None
                levels.append(outside if outside is not None else
                              {1: 1, 3: 0, 4: 1, 5: 0, 6: 1}.get(mode, self.gpio_inputs.get(ch, 0)))
            drive = b""
            if self.drive_levels is not None:                      # read's drive: the level in mode 3 / 4, else 0xFF
                drive = m.tlv(GPIO_READ_DRIVE, bytes(self.gpio_drive.get(ch, NOT_DRIVEN)
                                                     if self.gpio_modes.get(ch, 0) in OUTPUT_MODES else NOT_DRIVEN
                                                     for ch in chans))
            return self._answer(bytes([len(levels)]) + bytes(levels) + drive)   # n(u8) n x level [TLV] (fixture §1)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _set_drives(self, t: Take, pairs: list[tuple[int, int]]) -> dict[int, int]:
        """set's drive TLVs (fixture §1.1) -> {element index: level}. malformed (the whole request): a value that is not
        4 bytes, index n or more, the same index twice, an undefined kind, an element whose mode is not 3 / 4; kind 0
        with a level number past the levels is ignored (listed in `t.ignored`; critical: unsupported, core §2.3)."""
        out, seen, drives = {}, set(), []
        for tag, value in getattr(t, "repeated", []):
            if tag & 0x7F != GPIO_SET_DRIVE:
                continue
            if len(value) != 4:
                raise Reject(m.MALFORMED)
            index, kind, v = struct.unpack("<BBH", value)
            if index >= len(pairs) or index in seen or kind not in DRIVE_KIND.values() or pairs[index][1] not in OUTPUT_MODES:
                raise Reject(m.MALFORMED)
            seen.add(index)
            drives.append((tag, index, kind, v))
        for tag, index, kind, v in drives:                         # every form checked first (core §4.3's order)
            level = self._drive_level(kind, v)
            if level is None:
                if tag & m.TAG_CRITICAL:
                    raise Reject(m.UNSUPPORTED, bytes([tag]))
                t.ignored.append(GPIO_SET_DRIVE)                   # one entry per ignored drive
                continue
            out[index] = level
        return out

    # ---- oep.fixture.uart -----------------------------------------------------------------------
    def _uart(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        fmt_tag = _UART.tlv["configure"]["format"]
        if op == _UART.op["configure"]:
            baud = t.take("I")
            got, _ = t.tail({fmt_tag})
            fmt = got.get(fmt_tag, b"\0")
            if len(fmt) != 1:
                raise Reject(m.MALFORMED)
            as_sent = fmt_tag | (m.TAG_CRITICAL if fmt_tag in t.critical else 0)
            try:
                actual = self._uart_check(baud, fmt[0], fn, as_sent)
            except Reject as r:
                if r.reason == m.UNSUPPORTED and r.payload[:1] == bytes([as_sent]) and fmt_tag not in t.critical:
                    t.refuse(fmt_tag, got)                # non-critical: dropped, listed as ignored
                    fmt = bytes(1)
                    actual = self._uart_check(baud, 0, fn, None)
                else:
                    raise
            if fn not in self.uarts:
                raise unavailable("wrong_state")                   # no pins: the plan has neither RX nor TX (fixture §2)
            self.uart_baud[fn] = (actual, fmt[0], UART_CONFIGURED["session"])
            self.uart_session_cfg.add(fn)                          # a session's configure beats the uart item
            return self._answer(struct.pack("<I", actual))
        if op == _UART.op["status"]:                               # lock-free: configured(uart_configured) baud format
            t.tail()
            baud, fmt, how = self.uart_baud.get(fn, (115200, 0, UART_CONFIGURED["default"]))
            return self._answer(struct.pack("<BIB", how, baud, fmt))
        s = self.uarts.get(fn)
        if s is None:
            raise unavailable("wrong_state")                       # the plan makes the stream (fixture §2)
        return self._stream_op(s, op, t, self.uart_accept)

    def uart_rx(self, fn: int, data: bytes) -> None:
        """Bytes arrive on fixture UART `fn`'s RX."""
        self.uarts[fn].data += data

    # ---- oep.fixture.i2c-target (fixture §3) ------------------------------------------------------
    def _i2c_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        st, (max_length, features, depth, max_stretch) = self.i2c[fn], self.target_decl[fn]
        ops = _I2C.op
        planned = any(a[0] == fn for a in self.plan)
        if op == ops["configure"]:                                 # core §4.3's order: malformed, unsupported, unavailable
            address, mode = t.take("BB")
            t.tail()
            if address > 0x7F or mode not in I2C_MODE.values():
                raise Reject(m.MALFORMED)
            if mode == I2C_MODE["preloaded_tx"] and not features & I2C_FEATURES["preloaded_tx"]:
                raise unsupported_fixed()                          # mode 3 without features bit0
            if not planned:
                raise unavailable("wrong_state")                   # before the plan (cause 6)
            self.i2c[fn] = I2cState(state=1, address=address, mode=mode, stretch_us=st.stretch_us)   # made anew
            return self._answer(b"")
        if op == ops["arm_rx"]:
            length = t.take("H")
            t.tail()
            if length == 0:
                raise Reject(m.MALFORMED)
            if length > max_length:
                raise unsupported_fixed()
            if st.state == 0 or st.mode != I2C_MODE["fixed_rx"]:
                raise unavailable("wrong_state")                   # mode 1 only
            st.armed = length                                      # an earlier wait is dropped for this one
            return self._answer(b"")
        if op == ops["read_rx"]:
            t.tail()
            if st.state == 0:
                raise unavailable("wrong_state")                   # cause 6
            if not st.queue:
                return self._answer(struct.pack("<BH", 0, 0))
            frame, ns = st.queue.pop(0)
            return self._answer(struct.pack("<BH", min(len(st.queue), 255), len(frame)) + frame
                                + m.tlv(_I2C.tlv["read_rx_answer"]["ns"], struct.pack("<Q", ns)))
        if op == ops["preload_tx"]:
            count = t.take("H")
            data = t.bytes(count)
            t.tail()
            if count == 0:
                raise Reject(m.MALFORMED)
            if count > max_length:
                raise unsupported_fixed()
            if st.state == 0 or st.mode != I2C_MODE["preloaded_tx"]:
                raise unavailable("wrong_state")                   # mode 3 only
            if len(st.tx) >= depth:
                raise unavailable("limit")                         # every slot holds an unread preload
            st.tx.append(data)
            st.slots = (st.slots + 1) & 0xFF
            return self._answer(bytes([st.slots]))
        if op == ops["status"]:                                    # lock-free
            t.tail()
            return self._answer(struct.pack("<BBBBIBI", st.state, st.mode, int(st.armed > 0), min(len(st.queue), 255),
                                            st.rx_frames, len(st.tx) if st.mode == I2C_MODE["preloaded_tx"] else 0,
                                            st.errors))
        if op == ops["reset"]:
            t.tail()
            if st.state == 0:
                raise unavailable("wrong_state")
            self.i2c[fn] = I2cState(state=1, address=st.address, mode=st.mode, stretch_us=st.stretch_us)
            return self._answer(b"")
        if op == ops["stretch"] and features & I2C_FEATURES["stretch"]:
            us = t.take("I")
            t.tail()
            if us > max_stretch:
                raise unsupported_fixed()                          # past describe's max_stretch_us
            st.stretch_us = us                                     # any state; configure / reset keep it
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _i2c_frame(self, st: I2cState, frame: bytes, depth: int) -> None:
        if len(st.queue) >= depth:
            st.errors += 1                                         # the queue overflows: the new frame goes
            return
        st.queue.append((bytes(frame), self.now_ns()))
        st.rx_frames += 1

    def i2c_write(self, fn: int, data: bytes, address: int | None = None) -> bool:
        """TEST HOOK: a bus controller writes `data` to i2c-target `fn` in one transaction (START, address + W, data,
        STOP). -> whether the target ACKed its address (configured, and `address` None or its own). An address-only
        write (empty data) counts nothing in any mode. Mode 1: the armed length exactly is a frame and the wait goes on
        (armed until the next arm_rx / reset / configure / plan release); another length is a receive error; no wait
        -> ACKed, dropped, an error. Mode 2: the first byte is the length of the rest, the rest a frame (a length that
        does not match is a receive error). Mode 3 takes no writes: ACKed, dropped, an error."""
        st, (max_length, _, depth, _) = self.i2c[fn], self.target_decl[fn]
        if st.state == 0 or (address is not None and address != st.address):
            return False
        if not data:
            pass                                                   # address only: nothing counts
        elif st.mode == I2C_MODE["fixed_rx"] and st.armed:
            if len(data) == st.armed:
                self._i2c_frame(st, data, depth)
            else:
                st.errors += 1
        elif st.mode == I2C_MODE["framed_rx"] and 0 < data[0] == len(data) - 1 <= max_length:
            self._i2c_frame(st, data[1:], depth)
        else:
            st.errors += 1
        return True

    def i2c_read(self, fn: int, n: int, address: int | None = None) -> bytes | None:
        """TEST HOOK: a bus controller reads n bytes from i2c-target `fn` in one transaction. -> the bytes, or None
        when the address is not ACKed. Mode 3 answers from the oldest preloaded slot (cut or padded with 0xFF to n:
        the next read starts at the next slot); an empty slot list, mode 1 and mode 2 answer 0xFF."""
        st = self.i2c[fn]
        if st.state == 0 or (address is not None and address != st.address):
            return None
        if st.mode == I2C_MODE["preloaded_tx"] and st.tx:
            slot = st.tx.pop(0)
            return (slot + b"\xff" * n)[:n]
        return b"\xff" * n

    # ---- oep.fixture.spi-target (fixture §4) ------------------------------------------------------
    def _spi_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        st, (max_length, features, _, _) = self.spi[fn], self.target_decl[fn]
        ops = _SPI.op
        if op == ops["configure"]:
            mode, order = t.take("BB")
            t.tail()
            if mode > 3 or order > 1:
                raise Reject(m.MALFORMED)
            if order == 1 and not features & SPI_FEATURES["lsb_first"]:
                raise unsupported_fixed()
            if not any(a[0] == fn for a in self.plan):
                raise unavailable("wrong_state")                   # before the plan (cause 6)
            self.spi[fn] = SpiState(state=1, mode=mode, bit_order=order)
            return self._answer(b"")
        if op == ops["arm"]:
            length, count = t.take("HH")
            tx = t.bytes(count)
            t.tail()
            if length == 0 or count > length:
                raise Reject(m.MALFORMED)
            if length > max_length:
                raise unsupported_fixed()
            if st.state == 0 or st.armed is not None:
                raise unavailable("wrong_state")                   # not configured, or one is armed already
            st.armed = (length, tx)
            return self._answer(b"")
        if op == ops["read_rx"]:
            t.tail()
            if st.state == 0:
                raise unavailable("wrong_state")                   # cause 6
            if not st.queue:
                return self._answer(struct.pack("<BIH", 0, 0, 0))
            bits, data, ns = st.queue.pop(0)
            return self._answer(struct.pack("<BIH", min(len(st.queue), 255), bits, len(data)) + data
                                + m.tlv(_SPI.tlv["read_rx_answer"]["ns"], struct.pack("<Q", ns)))
        if op == ops["status"]:                                    # lock-free
            t.tail()
            return self._answer(struct.pack("<BBBBBII", st.state, st.mode, st.bit_order, int(st.armed is not None),
                                            min(len(st.queue), 255), st.transactions, st.errors))
        if op == ops["reset"]:
            t.tail()
            if st.state == 0:
                raise unavailable("wrong_state")
            self.spi[fn] = SpiState(state=1, mode=st.mode, bit_order=st.bit_order)
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def spi_transfer(self, fn: int, mosi: bytes, bits: int | None = None) -> bytes:
        """TEST HOOK: a bus controller runs one transaction on spi-target `fn` (CS low, `bits` clocks - 8 a MOSI byte
        by default - CS high). -> the MISO bytes: the armed tx, 0 past it and when not armed. Armed: the MOSI bytes
        (bits / 8 rounded up) up to the armed length (more is an error) and the bits are queued, the wait ends; a full
        queue drops them (an error). Not armed: MOSI dropped, an error. Every transaction of a configured target
        counts; a target not configured sees nothing. 0 bits (CS edges without SCK) is no transaction: nothing counts,
        an arm keeps waiting."""
        st, (_, _, depth, _) = self.spi[fn], self.target_decl[fn]
        n = len(mosi)
        bits = 8 * n if bits is None else bits
        if st.state == 0 or bits == 0:
            return bytes(n)
        st.transactions += 1
        if st.armed is None:
            st.errors += 1
            return bytes(n)
        length, tx = st.armed
        st.armed = None
        got = (bits + 7) // 8
        if got > length:
            st.errors += 1                                         # past the armed length: dropped
        if len(st.queue) >= depth:
            st.errors += 1
        else:
            st.queue.append((bits, bytes(mosi[:min(got, length)]), self.now_ns()))
        return (tx + bytes(n))[:n]

    # ---- oep.probe.config -----------------------------------------------------------------------
    def _config_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _CFG.op["get"]:
            first = t.take("H")
            t.no_tail()                                            # no TLV in a get request (core §7.3)
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
            seen, plans = set(), {}
            try:
                tlvs = m.split_tlvs(t.data) if t.data else []
            except m.ProtocolError:
                raise Reject(m.MALFORMED) from None
            for received, value in tlvs:
                tag = received & 0x7F                              # kept without the critical bit
                if tag in (m.TAG_FIXED, m.TAG_IGNORED):
                    raise Reject(m.MALFORMED)
                if tag not in self.items:
                    raise Reject(m.UNSUPPORTED, bytes([received]))  # the tag as received (core §2.3)
                if tag == ITEM["plan"]:                            # one item per assignment, key (fn, role, channel)
                    if len(value) != 5:
                        raise Reject(m.MALFORMED)                  # core §2.3: a short value is a broken one
                    key = struct.unpack("<HBH", value)
                    if (tag, key) in seen:
                        raise Reject(m.MALFORMED)
                    seen.add((tag, key))
                    plans.setdefault(key[0], []).append(value)
                    continue
                key = self._item_key(tag, value)
                if (tag, key) in seen:
                    raise Reject(m.MALFORMED)                      # the same key twice in one set
                seen.add((tag, key))
                new[(tag, key)] = value
            for fn, values in plans.items():
                new[(ITEM["plan"], fn)] = values
            self._apply_config(new, changed_slots={k for t_, k in seen if t_ == ITEM["slot"]})
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self._hash(self.config))
        if op == _CFG.op["unset"]:                                 # n(u8) n x (len(u8) tag(u8) key)
            n = t.take("B")
            keys = []
            for _ in range(n):
                row = Take(t.bytes(t.take("B")))
                tag = row.take("B")
                if tag not in self.items:
                    raise Reject(m.UNSUPPORTED, bytes([tag]))
                keys.append((tag, self._item_key(tag, row.data[row.at:])))
            t.tail()
            new = dict(self.config)
            for tag, key in keys:
                new.pop((tag, key), None)                          # a key that is not there: nothing
            self._apply_config(new, changed_slots={k for t_, k in keys if t_ == ITEM["slot"]})
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self._hash(self.config))
        if op == _CFG.op["state"]:                                 # lock-free; §3.3, paged
            first_slot, first_bind = t.take("BB")
            t.tail()
            return m.COMPLETED, m.SUCCESS, self._state_answer(first_slot, first_bind)
        if op == _CFG.op["save"]:
            t.tail()
            if not self.storage_max:
                raise Reject(m.UNSUPPORTED)                        # a probe without storage
            if len(b"".join(self._canonical(self.config))) > self.storage_max:
                raise unavailable("storage_full")
            self.saved = dict(self.config)
            self.saved_ids = {fn: self.identity[fn] for fn in self._referenced(self.config) if fn in self.identity}
            self.saved_reason = 0
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self._hash(self.config))
        if op == _CFG.op["erase"]:
            t.tail()
            if not self.storage_max:
                raise Reject(m.UNSUPPORTED)
            self.saved, self.saved_ids, self.saved_reason = None, {}, 0
            return m.COMPLETED, m.SUCCESS, b""
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _state_answer(self, first_slot: int, first_bind: int) -> bytes:
        """probe.config §3.3: more storage_state storage_hash unreadable_reason, the slot states from first_slot and
        the bind states from first_bind that fit the frame."""
        saved_hash = self._hash(self.saved) if self.saved is not None and not self.saved_reason else 0
        state = 0 if self.saved is None else 2 if self.saved_reason else 1
        slots = [m.element(self._slot_state(n)) for n in sorted(self.slots)][first_slot:]
        binds = [m.element(self._bind_state(p)) for p in sorted(self.binds)][first_bind:]
        budget = self.probe.max_frame - m.RESULT_HEADER - 9
        out_s, out_b = [], []
        for row in slots:
            if sum(map(len, out_s)) + len(row) > budget:
                break
            out_s.append(row)
        for row in binds:
            if sum(map(len, out_s)) + sum(map(len, out_b)) + len(row) > budget:
                break
            out_b.append(row)
        more = int(len(out_s) < len(slots) or len(out_b) < len(binds))
        return (struct.pack("<BBIB", more, state, saved_hash, self.saved_reason)
                + bytes([len(out_s)]) + b"".join(out_s) + bytes([len(out_b)]) + b"".join(out_b))

    @staticmethod
    def _key_len(tag: int) -> int:
        return 1 if tag in (ITEM["slot"], ITEM["bind"]) else 2

    def _item_key(self, tag: int, value: bytes) -> int:
        if len(value) < self._key_len(tag):
            raise Reject(m.MALFORMED)
        return value[0] if self._key_len(tag) == 1 else struct.unpack_from("<H", value)[0]

    @staticmethod
    def _canonical(config: dict) -> list[bytes]:
        """probe.config §2: tag order, then key order (plan by (fn, role, channel)), the items' bytes as the host sent
        them, each in the one TLV encoding (core §2.2)."""
        rows = []
        for (tag, key), value in config.items():
            for v in (value if isinstance(value, list) else [value]):
                sort_key = struct.unpack("<HBH", v[:5]) if isinstance(value, list) else (key,)
                rows.append((tag, sort_key, m.tlv(tag, v)))
        return [r[2] for r in sorted(rows)]

    def _hash(self, config: dict | None) -> int:
        return zlib.crc32(b"".join(self._canonical(config or {})))

    @staticmethod
    def _referenced(config: dict) -> set[int]:
        """The fns saved items name (probe.config §2): plan fns, slot wire_fns, fixture UARTs a bind carries."""
        fns = set()
        for (tag, key), value in config.items():
            if tag in (ITEM["plan"], ITEM["uart"]):
                fns.add(key)
            elif tag == ITEM["slot"]:
                fns.add(struct.unpack_from("<H", value, 1)[0])
            elif tag == ITEM["bind"]:
                for at in _bind_streams(value):
                    if value[at] == BIND_STREAM["fixture_uart"]:
                        fns.add(struct.unpack_from("<H", value, at + 1)[0])
        return fns

    def _apply_saved(self) -> None:
        """At boot: the saved items' fns found again by (name, instance, revision) and renumbered, then applied; one
        not found (or of another revision) leaves the whole unapplied (probe.config §2)."""
        now = {ident: fn for fn, ident in self.identity.items()}
        remap = {}
        for fn, ident in self.saved_ids.items():
            if ident not in now:
                self.saved_reason = 2
                return
            remap[fn] = now[ident]
        pack = lambda fn: struct.pack("<H", remap.get(fn, fn))
        new = {}
        for (tag, key), value in self.saved.items():
            if tag == ITEM["plan"]:
                new[(tag, remap.get(key, key))] = [pack(key) + v[2:] for v in value]
            elif tag == ITEM["slot"]:
                new[(tag, key)] = value[:1] + pack(struct.unpack_from("<H", value, 1)[0]) + value[3:]
            elif tag == ITEM["bind"]:
                v = bytearray(value)
                for at in _bind_streams(value):
                    if v[at] == BIND_STREAM["fixture_uart"]:
                        v[at + 1:at + 3] = pack(struct.unpack_from("<H", v, at + 1)[0])
                new[(tag, key)] = bytes(v)
            elif tag == ITEM["uart"]:
                new[(tag, remap.get(key, key))] = pack(key) + value[2:]
            else:
                new[(tag, key)] = value
        self.saved = new                                           # the saved items, read for this boot's fns
        try:
            self._apply_config(new, boot=True)
        except Reject:
            self.saved_reason = 3

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
        self._apply_config(new, changed_slots=None, boot=True)
        if saved:
            self.saved = dict(self.config)
            self.saved_ids = {fn: self.identity[fn] for fn in self._referenced(self.config) if fn in self.identity}

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
            raise Reject(m.MALFORMED)                              # two slots on one place: the settings contradict
        disabled = {key for tag, key in new if tag == ITEM["disable"]}
        idles = {key for tag, key in new if tag == ITEM["idle"]}
        if disabled & idles:
            raise Reject(m.MALFORMED)                              # idle and disable for one channel (probe.config §1)
        for fn in self.pairs:                                      # every wire (pin_roles wires have an empty list)
            if sum(1 for s in slots.values() if s.wire_fn == fn and s.attach == SLOT_ATTACH["at_boot"]) > \
                    self.max_connections.get(fn, 1):
                raise unavailable("limit")
        for (tag, key), value in new.items():
            if tag == ITEM["bind"]:
                binds[key] = self._parse_bind(value, slots)
        plans: dict[int, list[tuple[int, int, int]]] = {}
        uarts: dict[int, tuple[int, int]] = {}
        for (tag, key), value in new.items():
            if tag == ITEM["plan"]:
                if any(len(v) != 5 for v in value):
                    raise Reject(m.MALFORMED)
                plans[key] = [struct.unpack_from("<HBH", v) for v in value]
                if key not in self.names or key == m.CORE_FN:
                    raise Reject(m.UNKNOWN_FUNCTION if key else m.MALFORMED)
            elif tag == ITEM["idle"]:
                # channel mode [drive_kind drive_value] (probe.config §1): 4 or 5 bytes, an undefined kind, a drive on
                # a mode other than 3 / 4 are malformed; past the drive, later fields (skipped)
                if len(value) < 3 or len(value) in (4, 5) or value[2] > IDLE_MODE["output_high"]:
                    raise Reject(m.MALFORMED)
                output = value[2] in (IDLE_MODE["output_low"], IDLE_MODE["output_high"])
                if len(value) >= 6 and (not output or value[3] not in DRIVE_KIND.values()):
                    raise Reject(m.MALFORMED)
                if output and key in self.input_only:
                    raise unsupported_fixed(m.tlv(_UNA["channel"], struct.pack("<H", key)))   # cannot drive it (§1)
                if (len(value) >= 6 and self.drive_levels is not None and value[3] == DRIVE_KIND["level"]
                        and struct.unpack_from("<H", value, 4)[0] >= len(self.drive_levels[1])):
                    raise unsupported_fixed(m.tlv(_UNA["channel"], struct.pack("<H", key)))   # no such level (§1)
            elif tag == ITEM["disable"] and key not in self._all_channels():
                # a channel the firmware does not declare: unsupported, as idle (probe.config §1)
                raise unsupported_fixed(m.tlv(_UNA["channel"], struct.pack("<H", key)))
            elif tag == ITEM["label"]:
                if len(value) < 3:
                    raise Reject(m.MALFORMED)
            elif tag == ITEM["uart"]:
                if len(value) < 7:
                    raise Reject(m.MALFORMED)
                fn, baud, fmt = struct.unpack_from("<HIB", value)
                if fn not in self.names:
                    raise Reject(m.UNKNOWN_FUNCTION)
                if self.names[fn] != "oep.fixture.uart":
                    raise Reject(m.UNSUPPORTED)
                uarts[fn] = (self._uart_check(baud, fmt, fn, None, divide=False), fmt)   # the range now, the divider at plan time
        want = [a for fn in plans for a in plans[fn]]
        if len(want) + len([a for a in self.plan if a[0] not in plans and a[0] not in self.plan_from_config]) > \
                (self.plan_roles if self.plan_roles is not None else 1 << 30):
            raise unavailable("limit")
        old_plan_fns = {k[1] for k in self.config if k[0] == ITEM["plan"]}
        self.slots = slots                                         # the pin check below sees the new slots
        try:
            self._check_plan(want)
            self._check_disabled(new, disabled, plans, want, slots)
        except Reject:
            self.slots = {k: self._parse_slot(v) for (t, k), v in self.config.items() if t == ITEM["slot"]}
            raise
        # accepted: make it current
        old_idle = {key: v for (tag, key), v in self.config.items() if tag == ITEM["idle"]}
        repark = (self.disabled - disabled) | {key for key in idles | set(old_idle) if new.get((ITEM["idle"], key)) != old_idle.get(key)}
        self.config = new                                          # the disable items first: a dropped plan's pins
        # idle before the plans (probe.config §2: idle, plan, uart, the at-boot attach): at boot every free channel,
        # later the ones whose idle changed (or enabled again); a gpio plan then takes a line in that state
        self._park(self._all_channels() if boot else repark)
        for fn in old_plan_fns - set(plans):                       # are not parked when the same set disables them
            self._drop_plan(fn)
        for fn, assigned in plans.items():
            self._replace_plans({fn}, assigned)
            self.plan_from_config.add(fn)
            self._uart_plan_changed(fn)
        for fn in self.uarts:                                      # the uart item (or its going) on the planned UARTs no session set
            if fn not in self.uart_session_cfg:
                self._uart_apply_item(fn)
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

    def _check_disabled(self, new: dict, disabled: set[int], plans: dict, want: list, slots: dict) -> None:
        """probe.config §1 disable: a channel in use now (a plan, a connection, a slot this set keeps) cannot be disabled
        (unavailable cause 1); an item naming a disabled channel (a plan, a slot's pins) is cause 5 with the channel."""
        for ch in sorted(disabled - self.disabled):
            for fn, _, c in sorted(self.plan):
                if c != ch:
                    continue
                if fn in self.plan_from_config:
                    if new.get((ITEM["plan"], fn)) == self.config.get((ITEM["plan"], fn)):
                        raise unavailable("pin_in_use", ch, fn, "settings_plan")
                elif fn not in plans:                              # a session's plan this set does not replace
                    raise unavailable("pin_in_use", ch, fn, "plan")
            conn = next((c for c in self.conns.values() if ch in c.pair), None)
            if conn is not None:
                raise unavailable("pin_in_use", ch, conn.fn, "connection")
            for (tag, key), value in self.config.items():
                if tag == ITEM["slot"] and new.get((tag, key)) == value and ch in self._parse_slot(value).pair:
                    raise unavailable("pin_in_use", ch, holder_kind="slot")
        self._refuse_disabled_in(disabled, [c for _, _, c in want] + [p for s in slots.values() for p in s.pair])

    @staticmethod
    def _refuse_disabled_in(disabled: set[int], channels: list[int]) -> None:
        for ch in channels:
            if ch != 0xFFFF and ch in disabled:
                raise unavailable("held_by_settings", ch, holder_kind="disabled")

    def _parse_slot(self, v: bytes) -> Slot:
        """probe.config §1.1, refused in core §4.3's order: the form (malformed), then what this probe lacks
        (unsupported), then unknown fns."""
        t = Take(v)
        n, wire_fn, swdio, swclk, attach, retry_ms, max_speed, idle_clock, mech, name_len = t.take("BHHHBIIBBB")
        name = t.bytes(name_len)
        lock_len = t.take("B")                                     # the lock's part; 0 = none (probe.config §1.1)
        lock_part = t.bytes(lock_len)
        boot_reset = t.take("B") if t.at < len(v) else SLOT_BOOT_RESET["off"]   # optional; then later fields, skipped
        if n >= self.slots_max or attach not in SLOT_ATTACH.values():
            raise Reject(m.MALFORMED)
        if boot_reset not in SLOT_BOOT_RESET.values():
            raise Reject(m.MALFORMED)                              # 2 or more (probe.config §1.1)
        if boot_reset and attach != SLOT_ATTACH["at_boot"]:
            raise Reject(m.MALFORMED)                              # boot_reset 1 on a slot that is not at boot
        if retry_ms and attach != SLOT_ATTACH["at_boot"]:
            raise Reject(m.MALFORMED)
        if idle_clock > 1:
            raise Reject(m.MALFORMED)
        if not SLOT_NAME.fullmatch(name.decode("ascii", "replace")):
            raise Reject(m.MALFORMED)
        if lock_len and (lock_len < 3 or lock_len % 2 == 0 or lock_part[0] == 0):
            raise Reject(m.MALFORMED)
        if wire_fn not in self.names:
            raise Reject(m.UNKNOWN_FUNCTION)
        if self.names[wire_fn] not in WIRES:
            raise Reject(m.UNSUPPORTED)                            # a wire without a target_id scheme (swd)
        if not self._allows(wire_fn, (swdio, swclk)):
            raise Reject(m.UNSUPPORTED)                            # not a pair that wire offers
        if idle_clock and self.names[wire_fn] != "oep.wire.rvswd":
            raise Reject(m.UNSUPPORTED)                            # as attach's idle_clock (debug §3)
        if mech != MECHANISM_NONE and mech not in self.mechanisms:
            raise Reject(m.UNSUPPORTED)
        lock = None
        if lock_len:
            half = (lock_len - 1) // 2
            scheme = lock_part[0]
            if scheme not in TARGET_ID_SCHEMES:
                raise Reject(m.MALFORMED)                          # not a defined scheme (§1.1)
            if scheme != reg.WIRE_RVSWD.enum["target_id_scheme"]["wch_dmi_7f"]:
                raise Reject(m.UNSUPPORTED)                        # defined, but not this wire's (swd's targetsel)
            if half != TARGET_ID_LEN:
                raise Reject(m.MALFORMED)                          # the lock's length is the scheme's value's (§1.1)
            lock = (scheme, lock_part[1:1 + half], lock_part[1 + half:])
        return Slot(n, wire_fn, (swdio, swclk), attach, retry_ms, max_speed, idle_clock, mech, name.decode(), lock,
                    boot_reset)

    def _parse_bind(self, v: bytes, slots: dict[int, Slot]) -> Bind:
        if len(v) < 4:
            raise Reject(m.MALFORMED)
        port, mode, selected, n = v[:4]
        streams = tuple((v[at], struct.unpack_from("<H", v, at + 1)[0]) for at in _bind_streams(v))
        if n == 0:                                                 # after the streams: later fields, skipped
            raise Reject(m.MALFORMED)
        if mode == BIND_MODE["manual"] and selected >= n:
            raise Reject(m.MALFORMED)
        for kind, i in streams:
            if kind == BIND_STREAM["slot_console"]:
                if i not in slots or slots[i].mechanism == MECHANISM_NONE:
                    raise Reject(m.MALFORMED)                      # a slot that is not there, or has no console
            elif kind != BIND_STREAM["fixture_uart"]:
                raise Reject(m.MALFORMED)
        if port not in self.serial_ports:
            raise Reject(m.UNSUPPORTED)                            # not a serial port (§1.2)
        if mode not in BIND_MODE.values() or not self.bind_modes & (1 << mode):
            raise Reject(m.UNSUPPORTED)
        for kind, i in streams:
            if kind == BIND_STREAM["fixture_uart"]:
                if i not in self.names:
                    raise Reject(m.UNKNOWN_FUNCTION)
                if self.names[i] != "oep.fixture.uart":
                    raise Reject(m.UNSUPPORTED)
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
        if s.attach != SLOT_ATTACH["at_boot"]:
            return
        tg = self._target(s.wire_fn, s.pair)
        cid = self._conn_at(s.wire_fn, s.pair)
        if cid is None:
            if not tg.answers:                                     # completed failed, status line
                if not self._retry_with_reset(n):
                    return
            try:
                speed = min(4_000_000, s.max_speed) if s.max_speed else 4_000_000   # the slot's line settings
                cid = self._seat(s.wire_fn, s.pair, tg, speed, evict=False)   # automatic: never evicts
                self.conns[cid].idle_clock = s.idle_clock
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

    def _retry_with_reset(self, n: int) -> bool:
        """probe.config §3.1: after an automatic attach of a boot_reset slot failed with status line, once per boot and
        only while no session has taken the lock since boot, the same attach again with the reset line (`nrst` by the
        §1.3 search, usable for that wire's reset TLV: role 3, not disabled, not held by a plan or a connection), held
        slot_retry_reset_hold_ms. -> True when the target answers after it."""
        s, rt = self.slots[n], self.slot_rt[n]
        if s.boot_reset != SLOT_BOOT_RESET["retry_with_reset"] or self.lock_taken or n in self.reset_retried:
            return False
        ch = self.line_for(s.name, "nrst")
        if (ch is None or ch not in self.reset_channels.get(s.wire_fn, set()) or ch in self.disabled
                or any(a[2] == ch for a in self.plan) or any(ch in c.pair for c in self.conns.values())):
            return False
        self.reset_retried.add(n)
        rt.reset_at_ms = self.now()                                # the time it starts pulling the line
        rt.last_try_ms = rt.reset_at_ms + RETRY_RESET_HOLD_MS      # the retry is an attempt too (§3.3), after the hold
        self.slot_reset_log.append((n, ch, RETRY_RESET_HOLD_MS))
        tg = self._target(s.wire_fn, s.pair)
        if tg.resets_through(ch):
            tg.silent_until_reset = False
            if tg.present:
                tg.halted, tg.dpc, tg.havereset = False, tg.reset_vector + 0x200, True   # method 0: running from reset
        return tg.answers                                          # the bind selection stays (the probe's own attach)

    def line_for(self, slot_name: str | None, name: str) -> int | None:
        """The channel of a line by the label convention (probe.config §1.3) over the settings' label items."""
        labels = [(key, value[2:].decode("utf-8", "replace")) for (tag, key), value in self.config.items()
                  if tag == ITEM["label"]]
        n_slots = sum(1 for tag, _ in self.config if tag == ITEM["slot"])
        return cfgmod.line_from_labels(labels, n_slots, slot_name, name)

    def _refresh(self) -> None:
        """Make what the slots use match the config: a bound slot rides any connection on its place (lock
        permitting) with its console open; an at-boot slot keeps its automatic connection; nothing else."""
        for sid, st in list(self.streams.items()):                 # a slot's share of a console: while bound, same place
            for u in [u for u in st.users if u != "host"]:
                s = self.slots.get(u[1])
                c = self.conns.get(st.conn)
                if (s is None or c is None or (s.wire_fn, s.pair) != (c.fn, c.pair) or s.mechanism != st.mechanism
                        or self._lock_ok(s, c.tid) is not True or not self._bound(u[1])):
                    self._drop_stream_user(sid, u, MARK_CLOSED["slot_changed"])
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
            if s.mechanism == MECHANISM_NONE:
                continue                                           # no console on this slot
            self.conns[cid].users.add(("slot", n))
            try:
                sid, existing = self._open_stream(cid, s.mechanism, ("slot", n))
            except Reject:
                continue                                           # another mechanism's stream is live there
            if not existing:
                for port, b in self.binds.items():
                    key = (BIND_STREAM["slot_console"], n)
                    if key in b.streams:
                        self.flows[(port, key)] = Flow(sid, 0)

    def tick(self) -> None:
        """Time passes: the lease, at-boot retries, mixed lines closed by quiet, the captures, port_speed's timers."""
        self._lapse()
        self._speed_tick()
        now = self.now()
        for fn, cap in self.captures.items():
            self._events(fn, cap.tick(now))
        for n, s in self.slots.items():
            rt = self.slot_rt[n]
            if (s.attach == SLOT_ATTACH["at_boot"] and s.retry_ms and not rt.evicted
                    and self._conn_at(s.wire_fn, s.pair) is None
                    and (rt.last_try_ms is None or now - rt.last_try_ms >= s.retry_ms)):
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
        tried = NEVER_NS if rt.last_try_ms is None else rt.last_try_ms * 1_000_000   # when (the probe's clock, ns)
        reset_at = NEVER_NS if rt.reset_at_ms is None else rt.reset_at_ms * 1_000_000   # the retry with reset (§3.1)
        raw = b"" if tid is None else struct.pack("<I", tid)
        return struct.pack("<BBHQBB", n, state, cid or 0, tried, 1 if raw else 0, len(raw)) + raw + struct.pack("<Q", reset_at)

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
        """The session ended (end, lapse, force): the ports it held resume from its last host reset (or now); a port
        off its boot speed goes back after the answer (core §3.5)."""
        if self.speed_state != "base":
            self.speed_pending = ("revert",)
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
        "oep.probe.config": "config_op", "oep.fixture.logic": "capture", "oep.fixture.analog": "capture",
        "oep.fixture.capture-group": "group", "oep.fixture.i2c-target": "i2c_op", "oep.fixture.spi-target": "spi_op"}
