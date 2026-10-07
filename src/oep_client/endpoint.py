"""The virtual bench's probe endpoint that speaks OEP v1, answering whole messages (no hardware).

This is the spec side's "working spec": ch32rv, the JS client, this client and the probe firmware are checked against
it. It wraps a `virtual_bench.VirtualProbe` (the static declarations, and what the probe keeps to itself: `own_channels`,
`Offered.inner`) and does what oep-spec docs/oep-core.ja.md, docs/oep-transports.ja.md and interfaces/*.ja.md
define (oep-spec 0f455a0, the rule review of 2026-10-07):

- the core (fn 0) has no name and is never listed (core §0, §7.2); its ops are exactly the eight mandatory ones:
  confirm, list, describe, clock, open, end, keepalive, lock_state (core §1.2, §12). list takes first(u16) alone and
  pages every interface (core §7.2); describe answers declarations only (§7.3). clock (core §7.7) answers boot_id and
  uptime_ns, read while handling the request - lock-free, and with session_id 0 it touches no session, lock or lease
- one request header of 10 bytes with session_id (0 = no session; core §4.1), TLVs as tag(u8) len(u16) value (§2.2),
  every sequence count x element with no element length and every fixed form closed (§2.3)
- request tails (core §2.3, `Take`): a tag this probe does not implement in the op's context is ignored, or refused
  unsupported (the tag as received) when critical; a tag it implements is checked the same with or without bit 7 - a
  value of another length (longer too) or one the definition excludes is malformed, a value the definition leaves
  unused or this probe does not handle is unsupported with the tag as received; a non-repeating tag sent twice: the
  first is used; tags 0x00 / 0x7F are no tags. `tail=` appends TLVs to every result that may carry them, so hosts can
  be checked to skip what they do not know
- refusals (core §4.3): the header (unknown_function, unknown_operation, session_required), then the resend table,
  then the session (no_session / locked + remaining ms + owner); after that every check runs before anything changes
  and the request is refused for one reason that applies (this virtual bench's handlers check the form, then the fns named, then
  what they do not handle, then the state and the resources). unsupported is always `tag [TLV]` (0x00 for a fixed-part
  value); unavailable carries cause, channel and (capture-group) fn; a resource number of the wrong kind is unavailable
  cause 6 - connections and streams share one u16 space, +1 skipping numbers in use (core §9)
- confirm with a revision range (its answer carries the boot_id and the transport it came on, core §7.1);
  `revision=0` makes a v0 probe that answers in the v0 shape and drops requests with a session_id
- the ops tag (core §1.2, §7.4): every fn's describe carries it, and an op it does not set is unknown_operation
  (`offers`)
- the lock (core §6): a host-chosen session id, extended by every answer to its holder that passed the session check
  (rejected ones too), counted from when that request completed; no resume: end, a lapse and force release everything
  the session created (its plan, its shares of connections and streams, its subscriptions, its capture-group bind);
  lease 0 = the probe default, 1000-60000 ms taken as asked; open's force is a boolean (non-zero = true, §2.1); the
  owner TLV (1-32 bytes) from the open that takes the lock, shown by lock_state and by rejected locked
- the resend table (core §5.2): (corr, answer) of the last session's recent requests - a request is known by its corr
  alone, a resend gets the remembered answer whatever it carries (answers longer than `remember_max` bytes are not kept
  -> result_lost); result_lost for old corrs; open is never looked up; emptied by a successful open, kept over end
- oep.probe.plan (oep-if-plan: role_assignment, plan_roles), oep.probe.restart (oep-if-restart §2: the answer first,
  then nothing on any transport until the probe has restarted; the target is not reset), notifications (core §11:
  subscribe / unsubscribe are the emitting interface's ops; events at once, data batched by min_bytes / max_delay_ms),
  oep.probe.link (oep-if-link: source len <= max_frame - 7, sink, and port_speed when the profile's ops offer it)
- port_speed (oep-if-link §3, the handshake): baud(u32) step(u8) verify_ms(u16) on the UART bridge the request came in
  on (another transport, or a try while a port is raised: unavailable cause 6); the nearest rate the virtual bench's UART makes
  (300..5000000 exactly, else the nearest end) within port_speed_tolerance_pct of the request, else unsupported; a try
  goes back after verify_ms without a commit, a commit after port_speed_idle_ms with no good frame (counted from a
  good frame or an answer sent), the session's end after its answer. The line is modelled by `broken_rates` (rate ->
  BrokenRate): frames at such a rate break, from a size and in the directions given, only both ways at once
  (`duplex`), only every Nth (`every`), only once `after` bytes have passed since the switch (`virtual_bench_serial` applies it)
- oep.wire.rvswd / swio (debug §1-§3): scan (count = 0 pages the free pairs from `skip`; a count > 0 request's skip is
  not looked at; tried and found per frame), attach on declared pin pairs with the reset TLV (an output-idle or
  disabled reset line unavailable cause 5), several connections up to max_connections and the seat rule, connections;
  attach and scan answer within max_op_ms - a target that restarts by itself after a reset (`VirtualTarget.restart_ms`)
  is waited for up to max_op_ms, then status line with the connection kept (`settle_log`); an attach that joins a live
  connection keeps its settings but what it carries (idle_clock), max_speed only lowers its speed; target_id scheme
  dmi_7f; search_retries only when a bring-up ran; a target op whose exchanges go unanswered fails status line and
  leaves the lines undriven until one succeeds (`pin_state` "wire-free")
- oep.target.riscv-dm (debug §4) on one `VirtualTarget` per pin pair: dmi step lists, halt, resume, reset (status flags
  pc; ndmreset; within max_op_ms), read_block / write_block, run (stopped 0-3: 3 when the preparation fails -
  `VirtualTarget.fail_regs` - or the hart runs), step (step_left)
- oep.target.console (console §1-§3): one live stream per connection, lifetime by its users, the streams list; a stream
  of a mechanism that carries host -> target bytes (DMDATA, dmseq) has a send queue of the probe's own size
  (`send_queue`, not declared): a write puts min(count, the free space) at its end (count 0: success; accepted 0 only
  when full; SDI takes nothing); the probe hands the queue's head to the target, dmseq 2 bytes and DMDATA 3 a poll -
  one poll per ms while the hart runs, none while it is halted or a riscv-dm request of the connection runs - or all of
  it at `console_take`
- oep.fixture.gpio (fixture §1: modes 0-6, the lines outside through `gpio_world`; the drive a u8 level, 0xFF the
  default - a level past drive_levels, or any drive without them, unsupported; `gpio_drive` / `parked_drive` the
  effective strength), oep.fixture.uart (its stream made by the plan; status baud / format; the settings' uart item),
  oep.fixture.i2c-target (§3, one form: configure an address, a write with data is one frame cut at max_length, reads
  from preload_tx slots or 0xFF, stretch when the ops offer it; test hooks `i2c_write` / `i2c_read`) and
  oep.fixture.spi-target (§4: configure, arm, read_rx, status; errors at most 1 a transfer; `spi_transfer`),
  oep.fixture.logic / analog / capture-group (`virtual_bench_capture`)
- oep.probe.config (probe.config §1-§3): plan / label / idle / slot / bind / uart / disable items (each of one form:
  another length is malformed, critical or not), get / set / unset / save / erase / state; the hash is the virtual bench's own
  (a seeded CRC-32 of get's bytes: no host may compute it); storage_hash is get's hash when the saved settings became
  current; at boot disable and idle before any other item; slots without a lock (state 0 connected / 1 absent), binds
  of one stream whose port position stays during a session and carries on after it; a disabled channel is refused
  everywhere with unavailable cause 5 and never parked; `parked` records the free pins' states
- the serial ports' raw side (transports §4, probe.config §1.2): `port_input` / `port_output` carry the bytes outside
  the frames for each serial port by its bind; a port the lock holder's requests came in on is held until the session
  ends. The byte framing itself is `virtual_bench_serial`.

Every other non-core fn gets two stand-in operations so the session rules can be exercised - VIRTUAL BENCH ONLY, they mean
nothing on a real probe:  0x01 write(u32) changes state, 0x02 read -> u32 needs no lock.
"""

from __future__ import annotations

import re
import secrets
import struct
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from . import catalog, config as cfgmod, virtual_bench, virtual_bench_capture, message as m, registry as reg

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
RESET_MODE = _RV.enum["reset_mode"]
STEP_LEFT = _RV.tlv["step_answer"]["step_left"]            # step's answer: the hart could not be halted again (§4.2)
ITEM = _CFG.tlv["item"]
CHANNEL_ITEMS = (ITEM["label"], ITEM["idle"], ITEM["disable"])   # items keyed by a channel (probe.config §1)
IDLE_MODE = _CFG.enum["idle_mode"]
CFG_DESCRIBE = _CFG.tlv["describe"]
SLOT_ATTACH = _CFG.enum["slot_attach"]
SLOT_STATE = _CFG.enum["slot_state"]
BIND_STREAM = _CFG.enum["bind_stream"]
BIND_FLOW = _CFG.enum["bind_flow"]
WIRES = ("oep.wire.rvswd", "oep.wire.swio")
OWNER = reg.CORE.tlv["open"]["owner"]
ROLE_ASSIGNMENT = reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"]   # the tag number (0x10); hosts send it critical
OP_PLAN_APPLY, OP_PLAN_RELEASE = reg.PROBE_PLAN.op["plan_apply"], reg.PROBE_PLAN.op["plan_release"]
OP_RESTART = reg.PROBE_RESTART.op["restart"]
CONFIRM_TRANSPORT = reg.CORE.tlv["confirm_answer"]["transport"]   # confirm's answer: the transport it came on (§7.1)
UNSUPPORTED_SUPPORTED = reg.CORE.tlv["unsupported_payload"]["supported"]   # confirm's refusal: the revisions handled
SLOT_NAME = re.compile(r"[a-z0-9_-]{1,32}")
NO_SLOT, NEVER_NS = 0xFF, 0xFFFFFFFFFFFFFFFF
PIN_ROLE_RESET = reg.WIRE_RVSWD.enum["pin_role"]["reset"]   # the channels an attach's reset TLV may take
TARGET_ID_SCHEME = reg.COMMON.enum["target_id_scheme"]     # one space for the whole probe (oep-if-debug §1)
_I2C, _SPI = reg.FIXTURE_I2C_TARGET, reg.FIXTURE_SPI_TARGET
I2C_FEATURES, SPI_FEATURES = _I2C.enum["features"], _SPI.enum["features"]
TARGET_ROLES = {"oep.fixture.gpio": {1}, "oep.fixture.uart": {1, 2}, _I2C.name: set(_I2C.enum["role"].values()),
                _SPI.name: set(_SPI.enum["role"].values())}   # the plan roles each fixture takes
UART_FORMAT_MASK = 0x1F                                    # the defined format bits (fixture §2)
_T_SCAN, _T_ATTACH, _T_DETACH, _T_ATTACH_ANSWER = (reg.WIRE_RVSWD.tlv[k] for k in ("scan", "attach", "detach", "attach_answer"))
ATTACH_METHOD = reg.WIRE_RVSWD.enum["attach_method"]
_UNSUP_INDEX = reg.CORE.tlv["unsupported_payload"]["index"]   # the position in the request's list (scan, gpio set)
GPIO_SET_DRIVE = _GPIO.tlv["set"]["drive"]                # index(u8) level(u8), repeats (fixture §1.1)
DRIVE_DEFAULT = _GPIO.enum["drive_level"]["default"]       # 0xFF: drive_levels' default level (fixture §1.1)
GPIO_MODE_MAX = max(_GPIO.enum["mode"].values())           # 6: the modes revision 1 defines are 0-6 (fixture §1)
OUTPUT_MODES = (_GPIO.enum["mode"]["output_low"], _GPIO.enum["mode"]["output_high"])   # the modes a strength applies to
LINK_SOURCE_OVERHEAD = m.RESULT_HEADER + 2                 # source's len <= max_frame - 7: header and len (oep-if-link §2)
REQUEST_HEADER = m.REQUEST_HEADER                          # role corr fn op session_id: 10 bytes (core §4.1)


def _any_ops(v: bytes) -> set[int]:
    """The ops an ops value's bits name, whatever its encoding (the virtual bench serves a test's broken value as given)."""
    if not v:
        return set()
    return {v[0] + i * 8 + b for i, byte in enumerate(v[1:]) for b in range(8) if byte >> b & 1 and v[0] + i * 8 + b <= 0xFF}


def _role_channels(v: bytes) -> list[int]:
    """The channels of a role_channels value: role(u8) base(u16) bitmap (core §7.4)."""
    return catalog.bitmap_to_channels(struct.unpack_from("<H", v, 1)[0], v[3:])


class _Unanswered(Exception):
    """A target op's exchange got no answer from the wire (debug §2)."""

    def __init__(self, conn):
        self.conn = conn


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
UNAVAILABLE_CAUSE = reg.CORE.enum["unavailable_cause"]


def unavailable(cause: str | None = None, channel: int | None = None, extra: bytes = b"") -> Reject:
    """rejected unavailable with core §4.3's payload: why and the channel (each optional), then `extra`."""
    body = b""
    if cause:
        body += m.tlv(_UNA["cause"], bytes([UNAVAILABLE_CAUSE[cause]]))
    if channel is not None:
        body += m.tlv(_UNA["channel"], struct.pack("<H", channel))
    return Reject(m.UNAVAILABLE, body + extra)


def wrong_kind() -> Reject:
    """A resource number of another kind (a stream where a connection goes): rejected unavailable cause 6 (core §9)."""
    return unavailable("wrong_state")


class Take:
    """Reads a request's fixed part; too short -> rejected malformed. `tail(known)` applies the request-tail rules of
    core §2.3:

    - a TLV past the end of the request -> malformed;
    - an unknown tag (one this probe does not implement in this (fn, op) context; 0x00 and 0x7F are never tags) ->
      critical: unsupported with the tag as received, raised once the whole tail has been read (so a malformed one
      later in it wins), or kept for `check()` when `defer` (the handler checks its own malformed cases first); not
      critical: ignored;
    - a known tag is the same with or without bit 7 (`critical` keeps which came with it, `received` gives it back):
      a non-repeating one twice -> the first is used; `fixed(got, tag, size)` checks its length (a value of another
      length -> malformed), `refuse(tag)` an unhandled value (-> unsupported with the tag as received)."""

    def __init__(self, payload: bytes):
        self.data, self.at = payload, 0
        self.critical: set[int] = set()
        self.repeated: list[tuple[int, bytes]] = []
        self.unknown_critical: int | None = None

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

    def tail(self, known: set[int] = frozenset(), repeats: set[int] = frozenset(),
             defer: bool = False) -> dict[int, bytes]:
        """-> known tags without the critical bit -> value (the first of a non-repeating tag sent twice)."""
        rest, at, got = self.data[self.at:], 0, {}
        self.critical = set()                                      # known tags that came with the critical bit
        self.repeated = []                                         # every known TLV in order, critical bit kept
        while at < len(rest):
            if at + m.TLV_HEADER > len(rest):
                raise Reject(m.MALFORMED)
            tag, n = struct.unpack_from("<BH", rest, at)          # tag(u8) len(u16) (core §2.2)
            at += m.TLV_HEADER
            if at + n > len(rest):
                raise Reject(m.MALFORMED)
            value = rest[at:at + n]
            at += n
            number = tag & 0x7F
            if number in known:
                if number in got and number not in repeats:
                    continue                                       # the first is used (core §2.3)
                got.setdefault(number, value)
                self.repeated.append((tag, value))
                if tag & m.TAG_CRITICAL:
                    self.critical.add(number)
            elif tag & m.TAG_CRITICAL:
                if self.unknown_critical is None:
                    self.unknown_critical = tag
            # an unknown tag without the critical bit: ignored (core §2.3)
        self.at = len(self.data)
        if not defer:
            self.check()
        return got

    def check(self) -> None:
        """An unknown critical TLV the tail held: rejected unsupported, the tag as received (core §2.3)."""
        if self.unknown_critical is not None:
            raise Reject(m.UNSUPPORTED, bytes([self.unknown_critical]))

    def received(self, tag: int) -> int:
        """A known tag as the request sent it (the critical bit kept): what an unsupported payload names."""
        return tag | (m.TAG_CRITICAL if tag in self.critical else 0)

    def fixed(self, got: dict[int, bytes], tag: int, size: int) -> bytes | None:
        """A known TLV of a fixed `size`: a value of another length -> malformed (core §2.3: an implemented TLV is
        checked with or without bit 7); None when absent."""
        value = got.get(tag)
        if value is not None and len(value) != size:
            raise Reject(m.MALFORMED)
        return value

    def refuse(self, tag: int, payload: bytes = b"") -> Reject:
        """A known TLV whose value this probe does not handle (a value the definition left unused, or one this probe
        does not declare): rejected unsupported with the tag as received (core §2.3), then `payload` (channel, index)."""
        return Reject(m.UNSUPPORTED, bytes([self.received(tag)]) + payload)


@dataclass
class VirtualTarget:
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
    target_id: int | None = None                       # the dmi_7f target_id attach reports (None: none)
    # run(pc, regs) -> (stopped, dpc, elapsed_us); default: halts 0x10 past the start
    run_hook: Callable | None = None
    unstoppable: bool = False                          # run: the limit passes and the hart cannot be halted (stopped 2)
    fail_regs: set = field(default_factory=set)        # run: regnos whose write before the run fails on the line - the
                                                       # preparation fails, the hart is not run (stopped 3, debug §4.4)
    reset_line: int | None = None                      # the channel wired to its reset (None: any reset channel resets it)
    silent_until_reset: bool = False                   # answers nothing on the wire until a reset through its line
    version: int = 2                                   # DMSTATUS.version: "found" is 2 or more and not 15 (debug §1)
    search_retries: int | None = 0                     # the bring-up's extra attempts an attach answer reports (debug §1;
                                                       # None: not sent; sent only when a bring-up ran)
    halt_stuck: bool = False                           # halt: allhalted never comes (status timeout, haltreq cleared)
    step_stuck: str | None = None                      # step: the hart does not come back - "halts" (to haltreq) / "runs"
    restart_ms: int = 0                                # after a reset's release it restarts by itself: the DM is silent
                                                       # this long (a bootloader on the way; debug §3, §4.3)
    haltreq: bool = False                              # what the probe left in DMCONTROL.haltreq
    dcsr_step: bool = False                            # what the probe left in dcsr.step
    # A debug link that drops (a CH32L103 after a change of hart state, through an RVSWD probe): from the DMI access
    # (each read and write of a dmi step, counted in dmi_accesses from 0) whose index is a key of drop_at, writes are
    # lost and reads give the last value read ("stale"), all ones ("ones"), or all ones with a DMSTATUS read failing
    # on the line ("ones_line": the reference probe takes a DMSTATUS of all ones for no answer). The drop lasts until
    # drop_requests later dmi requests have begun (the probe brings an idle link back before its next transaction);
    # within one request it never ends by itself. "glitch" is one access missed and the link up again at once (a link
    # coming back from a drop through a flicker): that write lost, or that read giving the last value read.
    # "glitch_parity" is the same, a write lost with cmderr 6 set in ABSTRACTCS when none is (QingKe: the module took the
    # frame for one with a bad parity).
    drop_at: dict = field(default_factory=dict)        # access index -> "stale" / "ones" / "ones_line" / "glitch" /
                                                       # "glitch_parity"
    drop_requests: int = 1
    dmi_accesses: int = 0
    dropped: str | None = None                         # the mode of the drop now in force (None: the link is up)
    drop_left: int = 0
    last_read: int = 0
    lost_writes: int = 0
    glitch: bool = False                               # this access is a "glitch" one
    glitch_parity: bool = False                        # ... a "glitch_parity" one

    @property
    def answers(self) -> bool:
        """Something answers on the wire now (scan, attach): present, and not stuck until a reset."""
        return self.present and not self.silent_until_reset

    @property
    def found(self) -> bool:
        """scan's "found" (debug §1): DMSTATUS.version 2 or more and not 15."""
        return self.version >= 2 and self.version != 15

    def resets_through(self, channel: int) -> bool:
        return self.reset_line in (None, channel)

    def dmstatus(self) -> int:
        return 0x80 | (self.version & 0xF) | ((0x300 if self.halted else 0xC00)) | (0xC0000 if self.havereset else 0)

    def begin_dmi(self) -> None:
        """A dmi request begins: a drop in force counts it, and ends once drop_requests requests have begun."""
        if self.dropped:
            self.drop_left -= 1
            if self.drop_left <= 0:
                self.dropped = None

    def _access(self) -> None:
        mode = self.drop_at.get(self.dmi_accesses)
        self.dmi_accesses += 1
        self.glitch = mode in ("glitch", "glitch_parity")
        self.glitch_parity = mode == "glitch_parity"
        if mode and not self.glitch:
            self.dropped, self.drop_left = mode, self.drop_requests

    def read_dmi(self, address: int) -> int | None:
        """One DMI read: the register's value, or what a dropped link gives (None: the read failed on the line).
        DMSTATUS and DMCONTROL read as the module has them unless a test set them in `dmi`."""
        self._access()
        if self.glitch:
            return self.last_read
        if self.dropped:
            if self.dropped == "stale":
                return self.last_read
            if self.dropped == "ones_line" and address == 0x11:
                return None
            return 0xFFFFFFFF
        queue = self.dmi_reads.get(address)
        if queue:
            self.dmi[address] = queue.pop(0)
        if address == 0x11 and address not in self.dmi:
            value = self.dmstatus()
        else:
            value = self.dmi.get(address, 1 if address == 0x10 else 0)   # DMCONTROL: dmactive, hart 0
        self.last_read = value
        return value

    def write_dmi(self, address: int, value: int) -> None:
        """One DMI write: lost on a dropped link. DMCONTROL's haltreq / resumereq move the hart (and read back 0);
        ABSTRACTCS's cmderr is write-1-to-clear; COMMAND runs an access-register command (32 bits) on regs / dpc,
        ignored while cmderr is set, cmderr 4 (halt/resume) on a running hart."""
        self._access()
        if self.dropped or self.glitch:
            self.lost_writes += 1
            if self.glitch_parity and not (self.dmi.get(0x16, 0) >> 8) & 7:
                self.dmi[0x16] = self.dmi.get(0x16, 0) | (6 << 8)
            return
        if address == 0x10:
            if value & (1 << 31):
                self.halted = True
            elif value & (1 << 30):
                self.halted = False
            self.dmi[0x10] = value & ~0xD0000000                # haltreq, resumereq, ackhavereset read 0
            return
        if address == 0x16:
            cs = self.dmi.get(0x16, 0)
            self.dmi[0x16] = cs & ~(value & 0x700)
            return
        self.dmi[address] = value
        if address == 0x17:
            self.abstract(value)

    def abstract(self, command: int) -> None:
        cs = self.dmi.get(0x16, 0)
        if (cs >> 8) & 7 or command >> 24:                     # cmderr set: ignored; only access register here
            return
        if not self.halted:
            self.dmi[0x16] = cs | (4 << 8)
            return
        if not command & (1 << 17):                            # no transfer
            return
        regno = command & 0xFFFF
        if command & (1 << 16):                                # write
            value = self.dmi.get(0x04, 0)
            if regno == 0x07B1:
                self.dpc = value
            else:
                self.regs[regno] = value
        else:
            self.dmi[0x04] = self.dpc if regno == 0x07B1 else self.regs.get(regno, 0)


@dataclass
class I2cState:
    """One oep.fixture.i2c-target (fixture §3): what configure made, the frames waiting for read_rx, the tx slots."""
    state: int = 0                       # 0 not configured, 1 running
    address: int = 0
    queue: list = field(default_factory=list)   # (frame, ns)
    rx_frames: int = 0
    errors: int = 0
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
    queue: bytearray = field(default_factory=bytearray)  # console: the send queue, accepted bytes not handed on yet
    fed_ms: int = 0                                    # console: the last poll that handed queue bytes on (timers' clock)
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
    free: bool = False                                 # an exchange went unanswered: the lines rest undriven (debug §2)


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


ITEM_SIZES = {ITEM["plan"]: 5, ITEM["idle"]: 4, ITEM["uart"]: 7, ITEM["disable"]: 2, ITEM["bind"]: 4}
SLOT_HEAD = struct.Struct("<BHHHBIIBBB")   # slot wire_fn swdio swclk attach retry_ms max_speed_hz idle_clock mechanism name_len


def _item_size(tag: int, v: bytes) -> int | None:
    """The length of item `tag`'s one form as the value's own counts make it (probe.config §1: plan 5, idle 4, uart 7,
    disable 2, bind 4, a slot by its name_len; a label is its text to the end: None). A slot too short for its head is
    malformed (`Reject`)."""
    if tag in ITEM_SIZES:
        return ITEM_SIZES[tag]
    if tag == ITEM["slot"]:
        if len(v) < SLOT_HEAD.size:
            raise Reject(m.MALFORMED)
        return SLOT_HEAD.size + v[SLOT_HEAD.size - 1]              # the name ends the item
    return None


@dataclass(frozen=True)
class Bind:
    port: int
    stream: tuple[int, int]                            # (kind, id): the one stream the port carries (probe.config §1.2)


@dataclass
class SlotRuntime:
    last_try_ms: int | None = None
    evicted: bool = False                              # the seat rule closed its connection: no retry until a new cue


@dataclass
class Flow:
    """One stream as a serial port's bind carries it: the stream id and the port's position in it."""
    sid: object = None
    pos: int = 0


@dataclass
class BrokenRate:
    """How a line rate breaks frames in the virtual bench (port_speed tests): frames of `min_size` bytes and more (on the wire)
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


_LINK = reg.PROBE_LINK
OP_PORT_SPEED = _LINK.op["port_speed"]
SPEED_STEP = _LINK.enum["port_speed_step"]
SPEED_IDLE_MS = reg.TIMING["port_speed_idle_ms"]   # committed: back to the boot speed after this with no good frame
SPEED_TOLERANCE_PCT = reg.LIMITS["port_speed_tolerance_pct"]   # the UART's nearest rate within this of the request (§3)
SPEED_RATES = (300, 5_000_000)                     # what the virtual bench's UART makes (anything between, exactly; outside it,
                                                   # the nearest end of the range)


class Endpoint:
    MARKS_PER_ANSWER = 3                     # small, so hosts must follow `more`
    CHUNK = 64                               # raw bytes a serial port takes at a time (probe guide §6)

    def __init__(self, probe: virtual_bench.VirtualProbe, now_ms: Callable[[], int], boot_id: int = 0x1234ABCD,
                 lease_default_ms: int = 3000, lease_max_ms: int = 60000, revision: int = 1, tail: bytes = b"",
                 window: int = 1 << 18, max_inflight: int = 4, remember_max: int = 72,
                 now_ns: Callable[[], int] | None = None):
        """`now_ms`: the clock the timers run on (leases, retries, port_speed), since this boot. `now_ns`: the same clock
        in ns when it has that resolution (virtual_bench_serve: time.monotonic_ns); without it the probe's clock (core §2.6a)
        is `now_ms` in ns. confirm's limits are checked against core §7.1 (max_frame 64 or more, window max_frame or
        more, max_inflight 1 or more, C-20) and the declared max_op_ms against core §7.5 (1 to max_op_ms_max, C-47):
        a probe outside them is not built."""
        if probe.max_frame < reg.MIN_MAX_FRAME or window < probe.max_frame or max_inflight < 1:
            raise ValueError(f"confirm's limits out of core §7.1's bounds: max_frame {probe.max_frame}, window {window}, "
                             f"max_inflight {max_inflight}")
        self.probe = probe
        self.now = now_ms
        self._clock_ns = now_ns
        self._origin_ns = 0                               # the probe clock's 0: this boot's start (core §2.6a)
        self._last_ns = 0                                 # the clock never goes back while the boot_id stays (C-31)
        self.boot_id = boot_id
        self.lease_default_ms = lease_default_ms
        self.lease_max_ms = lease_max_ms
        self.revision = revision
        self.tail = tail
        self.window, self.max_inflight = window, max_inflight
        self.remember_max = remember_max
        self.names = {o.fn: o.name for o in probe.offered}
        self.decl = {o.fn: [m.split_tlvs(t)[0] for t in o.tlvs] for o in probe.offered}   # fn -> [(tag, value)]
        self.identity = {o.fn: (o.name, o.instance, o.revision) for o in probe.offered}   # what a saved item names
        self.fns = {name: fn for fn, name in sorted(self.names.items(), reverse=True)}   # first fn of each name
        self.static = {o.fn: o.tlvs for o in probe.offered}
        self.static_labels: dict[int, str] = {}
        # channels the probe can only read (a test sets them): an idle of mode 3 / 4 there is unsupported (probe.config §1)
        self.input_only: set[int] = set()
        # channels without a pull (a test sets them): channel -> the idle modes (1 pull-up / 2 pull-down) it cannot
        # make; such an idle is unsupported (probe.config §1, PC-3)
        self.no_pull: dict[int, set[int]] = {}
        self.channels = 0xFFFF                          # fn 0 describe 0x43: an item's channel is below it (probe.config §1)
        self.own_channels: set[int] = set(probe.own_channels)   # the probe's own (never an interface's; not declared)
        self.transports: dict[int, int] = {}            # index -> kind, by the TLV's own index (core §7.5), not its order
        self.max_op_ms = virtual_bench.MAX_OP_MS
        # oep.probe.plan's plan_roles (oep-if-plan §1): the most role assignments at once (None: no limit declared)
        self.plan_roles: int | None = next((struct.unpack_from("<I", v)[0] for fn, name in self.names.items()
                                            if name == virtual_bench.PLAN for tag, v in self.decl[fn] if tag == virtual_bench.PLAN_ROLES_TAG),
                                           None)
        for tag, v in self.decl.get(0, ()):
            if tag == virtual_bench.CORE_LABEL:
                self.static_labels[struct.unpack_from("<H", v)[0]] = v[2:].decode()
            if tag == virtual_bench.CORE_TRANSPORT:
                self.transports[v[0]] = v[1]
            if tag == virtual_bench.CORE_MAX_OP_MS:
                self.max_op_ms = struct.unpack_from("<I", v)[0]
            if tag == virtual_bench.CORE_CHANNELS:
                self.channels = struct.unpack_from("<H", v)[0]
        if not 1 <= self.max_op_ms <= reg.LIMITS["max_op_ms_max"]:
            raise ValueError(f"max_op_ms {self.max_op_ms}: core §7.5 wants 1 to {reg.LIMITS['max_op_ms_max']}")
        self.serial_ports = {i for i, k in self.transports.items() if k in virtual_bench.SERIAL_KINDS}
        self.pairs: dict[int, list[tuple[int, int]]] = {}  # wire fn -> allowed (swdio, swclk), declared order
        self.max_connections: dict[int, int] = {}
        self.reset_channels: dict[int, set[int]] = {}     # wire fn -> channels an attach's reset TLV may take (role 3)
        self.pin_roles: dict[int, dict[int, set[int]]] = {}   # wire fn -> role -> candidates (role_channels wires)
        for fn, name in self.names.items():
            if name in WIRES:
                self.pairs[fn] = [self._group_pair(name, v) for tag, v in self.decl[fn] if tag == catalog.CHANNEL_GROUP]
                roles: dict[int, set[int]] = {}
                for tag, v in self.decl[fn]:
                    if tag == catalog.ROLE_CHANNELS and v[0] in (1, 2):
                        roles.setdefault(v[0], set()).update(_role_channels(v))
                if roles:
                    self.pin_roles[fn] = roles
                self.reset_channels[fn] = {
                    c for tag, v in self.decl[fn] if tag == catalog.ROLE_CHANNELS and v[0] == PIN_ROLE_RESET
                    for c in _role_channels(v)}
                self.max_connections[fn] = next((v[0] for tag, v in self.decl[fn] if tag == virtual_bench.MAX_CONNECTIONS), 1)
        self.targets: dict[tuple[int, tuple[int, int]], VirtualTarget] = {
            (fn, p): VirtualTarget() for fn in sorted(self.pairs) for p in self.pairs[fn]}
        for fn in sorted(self.pin_roles):                          # any pair: one target, on the first pair
            first = self._role_pairs(fn)[0]
            self.targets[(fn, first)] = VirtualTarget()
        self.target = next(iter(self.targets.values()), VirtualTarget())
        self.inner = {o.fn: dict(o.inner) for o in probe.offered}   # what each fn keeps to itself (not declared)
        self.captures: dict[int, virtual_bench_capture.VirtualCapture] = {
            fn: self._capture_from(self.decl[fn], self.inner[fn]) for fn, name in self.names.items()
            if name in ("oep.fixture.logic", "oep.fixture.analog")}
        self.groups: dict[int, virtual_bench_capture.VirtualGroup] = {
            fn: self._group_from(self.decl[fn], self.inner[fn]) for fn, name in self.names.items()
            if name == "oep.fixture.capture-group"}
        self.mechanisms = set()
        for fn, name in self.names.items():
            if name == "oep.target.console":
                self.mechanisms |= {b for tag, v in self.decl[fn] if tag == virtual_bench.MECHANISMS for b in v}
        self.block_max = {fn: self._own(fn, catalog.MAX_LENGTH, "H", 1 << 16)
                          for fn, name in self.names.items() if name == "oep.target.riscv-dm"}
        self.gpio_allowed = {fn: self._own(fn, virtual_bench.GPIO_MODES, "I", 0xFF)
                             for fn, name in self.names.items() if name == "oep.fixture.gpio"}
        # the output strengths (fixture §1.1, drive_levels): (default level, [approximate mA per level]) or None - one
        # declaration for the whole probe (every gpio fn declares the same)
        self.drive_levels: tuple[int, list[int]] | None = None
        for fn, name in sorted(self.names.items()):
            for tag, v in self.decl[fn] if name == "oep.fixture.gpio" else ():
                if tag == virtual_bench.GPIO_DRIVE_LEVELS:
                    default, n = v[0], v[1]
                    self.drive_levels = (default, list(struct.unpack_from(f"<{n}H", v, 2)))
        self.uart_formats = {fn: next((set(v[1:1 + v[0]]) for tag, v in self.decl[fn] if tag == virtual_bench.UART_FORMATS), {0})
                             for fn, name in self.names.items() if name == "oep.fixture.uart"}
        self.uart_max_hz = {fn: self._own(fn, catalog.MAX_CLOCK_HZ, "I", 3_000_000)
                            for fn, name in self.names.items() if name == "oep.fixture.uart"}
        own = self._own
        # the fixture targets' declarations (fixture §3 / §4): max_length, features, queue_depth, max_stretch_us
        # (i2c-target only; 0 when not declared)
        self.target_decl = {fn: (own(fn, catalog.MAX_LENGTH, "H", 1), own(fn, catalog.FEATURES, "I", 0),
                                 own(fn, _I2C.tlv["describe"]["queue_depth"], "B", 1),
                                 own(fn, _I2C.tlv["describe"]["max_stretch_us"], "I", 0) if name == _I2C.name else 0)
                            for fn, name in self.names.items() if name in (_I2C.name, _SPI.name)}
        # i2c-target internal pull-ups (fixture §3): features bit2, else none
        self.i2c_pullups = {fn: bool(self.target_decl[fn][1] & I2C_FEATURES["internal_pullups"])
                            for fn, name in self.names.items() if name == _I2C.name}
        cfg_fn = self.fns.get("oep.probe.config")
        cfg = dict(self.decl.get(cfg_fn, ()))
        self.slots_max = cfg[CFG_DESCRIBE["slots_max"]][0] if CFG_DESCRIBE["slots_max"] in cfg else 0
        self.items = set(cfg.get(CFG_DESCRIBE["items"], b""))
        self.storage_max = struct.unpack_from("<I", cfg[CFG_DESCRIBE["storage"]])[0] if CFG_DESCRIBE["storage"] in cfg else 0
        # the console's send queue (console §2): its size is the probe's (not declared); what a poll hands to the target
        # per mechanism (dmseq 2 bytes, DMDATA 3; SDI carries nothing to the target, §3.1)
        con_fn = self.fns.get(_CON.name)
        self.send_queue = self.inner[con_fn].get("send_queue", virtual_bench.CONSOLE_SEND_QUEUE) if con_fn is not None else 0
        self.console_feed = {_CON.enum["mechanism"]["dmdata"]: 3, _CON.enum["mechanism"]["dmseq"]: 2}
        self.settle_log: list[int] = []                 # every wait for a silent DM after a reset (ms): riscv-dm reset,
                                                        # attach's reset TLV - at most max_op_ms (debug §3, §4.3)
        self.uart_accept = 256
        # the ops each fn's describe declares (core §1.2, §7.4): what `offers` answers (an fn without the tag: none)
        self.ops = {fn: set().union(*(_any_ops(v) for tag, v in decl if tag == catalog.OPS))
                    for fn, decl in self.decl.items()}
        # an ops tag a test gave outside core §7.4's form (a probe that does not conform): served as given
        self.broken_ops = {fn: v for fn, decl in self.decl.items() for tag, v in decl
                           if tag == catalog.OPS and catalog.check_ops(v)}
        # port_speed (oep-if-link §3): on when the profile's oep.probe.link offers it (its ops); the boot speed every revert
        # goes back to. A test turns it off (None) or on: the link fn's ops follow (`offers`, `_declarations`)
        self.link_fn = self.fns.get(_LINK.name)
        self.port_speed_base: int | None = (115200 if self.link_fn is not None and OP_PORT_SPEED in self.ops[self.link_fn]
                                            else None)
        self.broken_rates: dict[int, BrokenRate] = {}   # the line: rates that break frames (virtual_bench_serial applies it)
        # oep.probe.restart (oep-if-restart, optional): the fn the profile lists it at (None: the probe has none -
        # `virtual_bench.without(probe, virtual_bench.RESTART)`), and the restart_max_ms its describe declares (a test may change it)
        self.restart_fn = self.fns.get(virtual_bench.RESTART)
        self.restart_max_ms = next((struct.unpack_from("<I", v)[0] for tag, v in self.decl.get(self.restart_fn, ())
                                    if tag == virtual_bench.RESTART_MAX_MS_TAG), virtual_bench.RESTART_MAX_MS)
        self.reboots = 0                                # restarts so far (the restart op and reboot()): a transport
                                                        # drops what it had read for the old boot when this moves
        self._transport = 0                             # the transport the request being handled came in on
        # what the simulation is, not the probe's state (a reboot keeps them):
        self.capture_slipped = False                    # every capture segment says flags bit2 (a pace that fell behind)
        self.uart_clock_hz = 80_000_000                # the UARTs' divider clock (a test lowers it: the item's fallback)
        # the lines outside (a test hook): gpio_world(channel, mode) -> the level an input mode reads (None: the
        # default - gpio_inputs, a pull-up 1); reads see the world as the modes set it (gpio_modes)
        self.gpio_world: Callable[[int, int], int | None] | None = None
        self._boot()

    def _boot(self) -> None:
        self.restarting = False              # restart answered (oep-if-restart §2): nothing more until reboot()
        self.unanswered = 0                  # requests that came while restarting (no answer, oep-if-restart §2)
        self.holder: int | None = None
        self.last: int | None = None         # S: the id that holds or last held the lock (the resend table's, §5.2)
        self.owner: bytes | None = None      # the holder's owner, while the lock is held (core §6.4)
        self.lease_ms = self.lease_default_ms
        self.expires_ms = 0
        self.values: dict[int, int] = {}
        self.dropped = 0                     # requests a v0 endpoint dropped (any with a session_id)
        self.revision_in_use: dict[int, int] = {}   # transport -> the revision its last confirm chose (core §7.1)
        self.discarded = 0                   # messages discarded unanswered: not a request role, or short (core §2.4)
        self.requests: list[m.Request] = []
        # fn -> (min_bytes, max_delay_ms) of its subscription (core §11.3); ends with the lock
        self.subscribed: dict[int, tuple[int, int]] = {}
        self.data_since_ms: dict[int, int] = {}         # fn -> when its oldest data not sent yet was there (max_delay_ms)
        self.push_seq: dict[int, int] = {}              # fn -> the next event / data seq (core §11.2)
        self.outbox: list[bytes] = []                   # events and data frames waiting to go out (pushes())
        self.plan: set[tuple[int, int, int]] = set()   # (fn, role, channel), from plan_apply and the config
        self.plan_from_config: set[int] = set()        # fns whose plan came from the config (not a session's)
        self.resend: OrderedDict[int, bytes | None] = OrderedDict()   # corr -> its answer (None: too large to keep)
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
        self.uarts: dict[int, Stream] = {}             # fn -> stream (while its plan has RX or TX)
        self.uart_carry: dict[int, tuple[int, int]] = {}   # fn -> (position, mark serial) a released stream left off at
        self.uart_baud: dict[int, tuple[int, int]] = {}   # fn -> (baud, format) in force (absent: 115200 8N1)
        self.uart_session_cfg: set[int] = set()        # fns a session's configure set (it beats the uart item)
        self.uart_tx: dict[int, bytearray] = {}        # what a serial port's raw bytes sent out on a fixture UART
        self.i2c: dict[int, I2cState] = {fn: I2cState() for fn, n in self.names.items() if n == _I2C.name}
        self.spi_selected: set[int] = set()            # spi-target fns whose CS is active now (spi_select)
        self.spi: dict[int, SpiState] = {fn: SpiState() for fn, n in self.names.items() if n == _SPI.name}
        self.config: dict[tuple[int, int], bytes | list[bytes]] = {}   # (item tag, key) -> value (plan: list)
        self.saved: dict | None = getattr(self, "saved", None)
        self.saved_ids: dict[int, tuple] = getattr(self, "saved_ids", {})   # saved fn -> (name, instance, revision)
        self.saved_reason = 0                          # why the saved settings were not applied (probe.config §4)
        self.saved_hash = getattr(self, "saved_hash", 0)   # storage_hash: get's hash when the saved ones became current
        self.slots: dict[int, Slot] = {}
        self.binds: dict[int, Bind] = {}
        self.slot_rt: dict[int, SlotRuntime] = {}
        self.flows: dict[tuple[int, tuple[int, int]], Flow] = {}
        self.held_ports: set[int] = set()
        self.parked: dict[int, int] = {}               # channel -> the idle mode the probe put the free pin in
        self.speed_state = "base"                      # port_speed: "base", "try" or "committed" (oep-if-link §3)
        self.speed_port: int | None = None             # the port off its boot speed
        self.speed_rate = 0                            # the rate it runs at
        self.speed_asked = 0                           # the baud the try asked (the commit names it again)
        self.speed_until_ms = 0                        # try: the commit's deadline (verify_ms)
        self.speed_good_ms = 0                         # committed: the last good frame on that port or answer sent there
        self.speed_pending: tuple | None = None        # ("switch", port, rate, asked, verify_ms) / ("revert",): after the answer
        self.speed_log: list[tuple[int, int]] = []     # (port, rate) every switch, reverts included
        if self.saved is not None:
            self._apply_saved()                        # disable and idle before any other item (probe.config §2)
        self._park(self._boot_channels())              # every free pin's idle state before the first answer (core §8)

    def _own(self, fn: int, tag: int, fmt: str, default: int) -> int:
        """fn's describe value of `tag` as one number (`fmt`), else `default`."""
        return next((struct.unpack_from("<" + fmt, v)[0] for t, v in self.decl.get(fn, ()) if t == tag), default)

    def _channel_ok(self, ch: int) -> bool:
        """An item's channel (probe.config §1): below fn 0's `channels` and not one the probe uses itself."""
        return ch < self.channels and ch not in self.own_channels

    def _idle_mode(self, ch: int) -> int | None:
        """The mode of channel `ch`'s idle item (probe.config §1), None without one."""
        item = self.config.get((ITEM["idle"], ch))
        return item[2] if item and len(item) >= 3 else None

    def _output_idle(self, ch: int) -> bool:
        return self._idle_mode(ch) in (IDLE_MODE["output_low"], IDLE_MODE["output_high"])

    @property
    def disabled(self) -> set[int]:
        """The channels the settings' disable items take away (probe.config §1): never used, driven or configured."""
        return {key for tag, key in self.config if tag == ITEM["disable"]}

    def _refuse_disabled(self, channels, extra: bytes = b"") -> None:
        """A request naming a disabled channel: rejected unavailable cause 5 (held by settings) with the channel."""
        for ch in channels:
            if ch != 0xFFFF and ch in self.disabled:
                raise unavailable("held_by_settings", ch, extra=extra)

    def _all_channels(self) -> set[int]:
        """Every channel some fn's describe offers (role_channels, channel_group, the wires' pairs and reset lines)."""
        out = {ch for fn in self.decl if fn != m.CORE_FN for ch in self._declared_channels(fn)}
        out |= {p for pairs in self.pairs.values() for pair in pairs for p in pair}
        out |= {ch for chs in self.reset_channels.values() for ch in chs}
        out.discard(0xFFFF)
        return out

    def _boot_channels(self) -> set[int]:
        """What the probe parks at boot, before its first answer (core §8): every channel but its own - below fn 0's
        `channels` when it declares them, else every channel an interface offers."""
        if any(tag == virtual_bench.CORE_CHANNELS for tag, _ in self.decl.get(0, ())):
            return set(range(self.channels)) - self.own_channels
        return self._all_channels() - self.own_channels

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

    def _drive_level(self, level: int) -> int | None:
        """The level a strength specification (fixture §1.1: a level number, 0xFF the default level) picks; None: no
        such level (past drive_levels) or no drive_levels at all."""
        if self.drive_levels is None:
            return None
        default, ma = self.drive_levels
        if level == DRIVE_DEFAULT:
            return default
        return level if level < len(ma) else None

    def _idle_level(self, ch: int) -> int | None:
        """The level the idle state of `ch` drives at: None when it is not an output idle (or the probe declares no
        drive_levels); the idle's drive (fixture §1.1)."""
        item = self.config.get((ITEM["idle"], ch))
        if self.drive_levels is None or not item or item[2] not in OUTPUT_MODES:
            return None
        level = self._drive_level(item[3])
        return self.drive_levels[0] if level is None else level

    @property
    def target_id(self) -> int | None:
        return self.target.target_id

    @target_id.setter
    def target_id(self, value: int | None) -> None:
        self.target.target_id = value

    @staticmethod
    def _capture_from(decl: list[tuple[int, bytes]], inner: dict) -> virtual_bench_capture.VirtualCapture:
        """A capture as its describe declares it (modes and their max_samples, the rate range, the frontends) and as it
        keeps to itself (`inner`: the w it can lay a sample out in, the segment records it keeps, how much a read
        returns at most - the probe's own choices, oep-if-capture §1.1, §2, §3.2)."""
        d = reg.FIXTURE_ANALOG.tlv["describe"]                     # the logic's tags are the same numbers
        modes, lo, hi, fronts, most_samples = set(), 1, 1_000_000, {}, {}
        for tag, v in decl:
            if tag == d["frontend"]:                               # analog: frontend min_mv max_mv attenuation_mdb
                fe, lo_mv, hi_mv, mdb = struct.unpack("<BiiI", v)
                fronts[fe] = (lo_mv, hi_mv, mdb)
            elif tag == d["mode"]:
                modes.add(v[0])
                most_samples[v[0]] = struct.unpack_from("<I", v, 1)[0]   # mode max_samples max_segments
            elif tag == catalog.MIN_CLOCK_HZ:
                lo = struct.unpack("<I", v)[0]
            elif tag == catalog.MAX_CLOCK_HZ:
                hi = struct.unpack("<I", v)[0]
        return virtual_bench_capture.VirtualCapture(modes or {virtual_bench_capture.MODE["one_shot"]}, set(inner.get("widths", (8,))), lo, hi,
                                        inner.get("ring", 8), inner.get("max_read", 4096), fronts,
                                        max_samples=most_samples)

    @staticmethod
    def _group_from(decl: list[tuple[int, bytes]], inner: dict) -> virtual_bench_capture.VirtualGroup:
        d = reg.FIXTURE_CAPTURE_GROUP.tlv["describe"]
        tracks = []
        for tag, v in decl:
            if tag == d["tracks"]:                                 # n(u8) n x fn(u16)
                tracks = list(struct.unpack_from(f"<{v[0]}H", v, 1))
        return virtual_bench_capture.VirtualGroup(tracks, inner.get("max_tracks", len(tracks)), list(inner.get("budgets", ())))

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
        """The frames the probe sends by itself now (core §11): events (at once: never batched, core §11.3) and a
        streaming capture's data - held back while its subscription's batching says so: until min_bytes bytes wait, or
        max_delay_ms has passed since the oldest of them was there (0 = that condition unused; both 0: at once). fn 0
        sends nothing. A serving loop frames and sends them; a test takes them from here. None once a restart was
        answered (oep-if-restart §2)."""
        if self.restarting:
            return []
        self.tick()
        for fn, cap in self.captures.items():
            if fn not in self.subscribed:
                continue
            waiting = cap.unsent()
            if not waiting:
                self.data_since_ms.pop(fn, None)
                continue
            since = self.data_since_ms.setdefault(fn, self.now())
            min_bytes, max_delay_ms = self.subscribed[fn]
            due = (not min_bytes and not max_delay_ms) or (min_bytes and waiting >= min_bytes) or (
                max_delay_ms and self.now() - since >= max_delay_ms)
            if due:
                self.outbox += cap.pushes(fn, lambda fn=fn: self._next_seq(fn), self.probe.max_frame)
                self.data_since_ms.pop(fn, None)
        out, self.outbox = self.outbox, []
        return out

    def _capture(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        cap, O = self.captures[fn], virtual_bench_capture.OP
        self._events(fn, cap.tick(self.uptime_ms()))               # what the clock captured up to this request
        roles = sorted(a[1] for a in self.plan if a[0] == fn)
        budget = self.probe.max_frame - m.RESULT_HEADER
        try:
            if op in (O["configure"], O["query"]):
                analog = self.names[fn] == "oep.fixture.analog"
                frontend_tag = virtual_bench_capture.ANA.tlv["configure"]["frontend"]
                known = set(virtual_bench_capture.TLV.values()) | ({frontend_tag} if analog else set())
                # the frontend comes once per channel (capture §3.3): it repeats by its meaning
                got = t.tail(known, repeats={frontend_tag})
                fronts = []                                        # (role, frontend, the tag as received)
                for tag, v in t.repeated:
                    if tag & 0x7F != frontend_tag:
                        continue
                    if len(v) != 2:
                        raise Reject(m.MALFORMED)                  # an implemented TLV of another length (core §2.3)
                    fronts.append((v[0], v[1], tag))
                if len({r for r, _, _ in fronts}) != len(fronts):
                    raise Reject(m.MALFORMED)                      # two frontends for one role (capture §3.3)
                settled = cap.settle(got, t, len(roles), fronts)
                if op == O["configure"]:
                    cap.apply(settled)
                    cap.slipped = self.capture_slipped
                return self._answer(cap.answer(settled))
            if op in (O["start"], O["stop"], O["force"]):
                t.tail()                                           # the request's form before the state (core §4.3)
                if cap.group is not None:
                    raise unavailable("bound_in_group")            # the group's ops run it (capture §4)
            if op == O["start"]:
                t.tail()
                self._events(fn, cap.start(self.uptime_ms(), subscribed=fn in self.subscribed))
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
                cap.release(generation, serial, self.uptime_ms())
                return m.COMPLETED, m.SUCCESS, b""
            if op == virtual_bench_capture.ANA.op["calibration"] and cap.analog:
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.calibration()
        except virtual_bench_capture.Reject as e:
            raise Reject(e.reason, e.payload)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _group(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        grp, O = self.groups[fn], virtual_bench_capture.GRP.op
        try:
            if op == O["bind"]:
                n = t.take("B")
                fns = [t.take("H") for _ in range(n)]
                got = t.tail({virtual_bench_capture.GRP.tlv["bind"]["trigger_track"]})
                src = t.fixed(got, virtual_bench_capture.GRP.tlv["bind"]["trigger_track"], 2)
                grp.bind(self.captures, fns, struct.unpack("<H", src)[0] if src else 0)
                return self._answer(b"")
            if op == O["start"]:
                t.tail()
                per, own = grp.start(self.captures, self.uptime_ms(), lambda track: track in self.subscribed)
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
                    self._events(track, self.captures[track].tick(self.uptime_ms()))
                return m.COMPLETED, m.SUCCESS, grp.status(self.captures)
        except virtual_bench_capture.Reject as e:
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

    def _target(self, fn: int, pair: tuple[int, int]) -> VirtualTarget:
        tg = self.targets.get((fn, pair))
        if tg is None:                                             # a pair nothing is wired to
            tg = self.targets[(fn, pair)] = VirtualTarget(present=False)
        return tg

    @staticmethod
    def _group_pair(name: str, v: bytes) -> tuple[int, int]:
        roles = dict(catalog.unpack_channel_group(v)[1])
        return roles.get(1, 0xFFFF), roles.get(2, 0xFFFF) if name != "oep.wire.swio" else 0xFFFF

    def _raw_ns(self) -> int:
        return int(self._clock_ns()) if self._clock_ns is not None else int(self.now() * 1_000_000)

    def now_ns(self) -> int:
        """The probe's one clock (core §2.6a): ns since boot, at the resolution of `now_ns` when the endpoint has one
        (else ms). It never decreases while the boot_id is the same (C-31), and starts again at a reboot."""
        self._last_ns = max(self._last_ns, self._raw_ns() - self._origin_ns)
        return self._last_ns

    def uptime_ms(self) -> int:
        """The probe's clock in ms (what the captures count their times from)."""
        return self.now_ns() // 1_000_000

    def _clock_ns_of(self, ms: int) -> int:
        """A time of the timers' clock (`now_ms`, e.g. a slot's last attempt) on the probe's clock (ns since boot)."""
        return max(0, ms * 1_000_000 - self._origin_ns)

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
        if not data or data[0] != m.ROLE_REQUEST or len(data) < REQUEST_HEADER:
            self.discarded += 1                                   # not a request, or shorter than its header (C-36)
            return None
        if self.restarting:
            self.unanswered += 1                                  # the restart's answer is out: nothing more (oep-if-restart §2)
            return None
        req = m.Request.unpack(data)
        if self.revision == 0 and req.session:
            self.dropped += 1                                     # a v0 probe: no sessions in its shapes, no answer
            return None
        self.requests.append(req)
        self._transport = transport
        header = self._header(req)                                 # core §4.3: the header, before the resend table
        if header is not None:
            return m.Result(req.corr, m.REJECTED, header).pack()
        remembered = self._resent(req)
        if remembered is not None:
            return remembered                                      # replayed: the lease does not restart (§6.1)
        try:
            res, detail, payload = self._dispatch(req)
        except Reject as r:
            res, detail, payload = m.REJECTED, r.reason, r.payload
        if res == m.REJECTED and detail == m.UNSUPPORTED and not payload:
            payload = bytes([m.TAG_FIXED])                         # core §4.3: unsupported is always tag [TLV]
        if res != m.REJECTED and not self._closed_tail(req.fn, req.op) and self.revision >= 1:
            payload += self.tail
        past_header = not (res == m.REJECTED and detail in (m.UNKNOWN_FUNCTION, m.UNKNOWN_OPERATION,
                                                            m.SESSION_REQUIRED))
        is_open = req.fn == m.CORE_FN and req.op == m.OP_OPEN
        if req.session and req.session == self.holder and past_header:
            # every answer to the holder's request past the session check, rejected ones too (core §6.1)
            self.expires_ms = self.now() + self.lease_ms
        took_lock = is_open and res == m.COMPLETED
        if self.holder is not None and transport in self.serial_ports and (
                took_lock or (req.session and req.session == self.holder)):
            self.held_ports.add(transport)                         # transports §4: the raw transfer holds here
        out = m.Result(req.corr, res, detail, payload).pack()
        self.speed_answered(transport)                             # a committed rate's idle counts from here too
        if req.session and req.session == self.last and past_header and not is_open:
            self._remember(req, out)                               # rejected answers too; open is not (core §5.2)
        if transport not in self.serial_ports:
            self.after_answer()                                    # the answer is not on a port whose speed changes
        return out

    def _header(self, req: m.Request) -> int | None:
        """core §4.3, the header: unknown_function, unknown_operation (an op the interface does not define,
        or an optional op this probe does not offer: core §1.2, `offers`), session_required - in that order and before
        the resend table, so these are neither remembered nor restart the lease."""
        if req.fn != m.CORE_FN and req.fn not in self.names:
            return m.UNKNOWN_FUNCTION
        if not self.offers(req.fn, req.op):
            return m.UNKNOWN_OPERATION
        if not req.session and not self._lock_free(req.fn, req.op):
            return m.SESSION_REQUIRED                              # an op that needs the lock with session_id 0 (§4.1)
        return None

    def features(self, fn: int) -> int:
        """fn's describe features (common tag 0x06, u32), 0 without one."""
        return self._own(fn, catalog.FEATURES, "I", 0)

    def offers(self, fn: int, op: int) -> bool:
        """core §1.2: an fn offers exactly the ops its describe's ops tag sets (§7.4) - every required op, and an
        optional one when the probe has it; any other op is unknown_operation (§4.3, the header). The profiles say which
        optional ops they have (virtual_bench.VirtualProbe fills in the ops tag of an interface that gives none: all of its ops).
        oep.probe.link's port_speed follows `port_speed_base` (a test turns it off or on)."""
        if fn not in self.ops:
            return False
        if fn == self.link_fn and op == OP_PORT_SPEED:
            return self.port_speed_base is not None
        return op in self.ops[fn]

    def _interface(self, fn: int):
        return reg.INTERFACES.get(self.names.get(fn, ""))

    def _closed_tail(self, fn: int, op: int) -> bool:
        """Answers the test `tail` is not appended to: the answers that are TLV lists themselves - describe and
        probe.config's get (their own meta TLVs are 0x3F / 0x7E)."""
        if fn == m.CORE_FN and op == m.OP_DESCRIBE:
            return True
        return self.names.get(fn) == "oep.probe.config" and op == _CFG.op["get"]

    def _lock_free(self, fn: int, op: int) -> bool:
        i = self._interface(fn)
        if fn == m.CORE_FN:
            return op in reg.CORE.lock_free
        if i is None or self.names.get(fn) not in SIMS:
            return op == TOY_READ
        return op in i.lock_free

    @staticmethod
    def _answer(payload: bytes, detail: int = m.SUCCESS) -> tuple[int, int, bytes]:
        """A completed result."""
        return m.COMPLETED, detail, payload

    def _dispatch(self, req: m.Request) -> tuple[int, int, bytes]:
        """Routes a request (its Take reads the payload as core §2.3 says)."""
        return self._route(req, Take(req.payload))

    def _route(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        self._lapse()
        self._console_poll()
        if req.fn == m.CORE_FN and req.op == m.OP_OPEN:
            return self._open(t, req)
        if req.session:                                            # §4.1: any request with an id goes through §6.2
            refused = self._check(req.session)
            if refused:
                return refused
        if req.fn == m.CORE_FN and req.op == m.OP_CONFIRM:
            return self._confirm(t)
        if req.fn == m.CORE_FN:
            return self._core(req, t)
        if req.op in (m.OP_SUBSCRIBE, m.OP_UNSUBSCRIBE) and (req.fn in self.captures or req.fn in self.groups):
            return self._subscription(req.fn, req.op, t)          # the emitting interface's own ops (core §11.3)
        sim = SIMS.get(self.names[req.fn])
        if sim is not None:
            return getattr(self, f"_{sim}")(req.fn, req.op, t)
        return self._toy(req, t)

    # ---- the resend table (core §5.2) -----------------------------------------------------------
    def _resent(self, req: m.Request) -> bytes | None:
        """A request of the last session seen before: its remembered result or result_lost; None = new. The request is
        known by its corr alone (core §5.2): a request with a remembered corr gets that result whatever it carries.
        An open is never looked up (core §5.2: a resent open is decided by §6.2)."""
        if not req.session or req.session != self.last or (req.fn == m.CORE_FN and req.op == m.OP_OPEN):
            return None
        if req.corr in self.resend:
            result = self.resend[req.corr]
            return result if result is not None else m.Result(req.corr, m.REJECTED, m.RESULT_LOST).pack()
        if self.newest_corr is not None and m.serial_diff(req.corr, self.newest_corr, 16) <= 0:
            return m.Result(req.corr, m.REJECTED, m.RESULT_LOST).pack()
        return None

    def _remember(self, req: m.Request, result: bytes) -> None:
        self.resend[req.corr] = result if len(result) <= self.remember_max else None   # (corr, answer) (core §5.2)
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
            self._release_lock()                                   # the lock goes, the last id stays (the table's)

    def _release_lock(self) -> None:
        """The session's lock ends - end, lease expiry or force, all alike (core §6.4, §9): everything it created is
        released - its plan (the pins to their idle state), its shares of the streams (before those of the connections:
        a stream it was the last user of closes with detail session_ended, console §2) and of the connections (one
        nobody else uses closes), its subscriptions; the owner is forgotten. Nothing passes to the next session. What
        the settings keep (their plan, a slot's connection, a bind's stream) stays."""
        self.holder = None
        self.owner = None
        self.subscribed.clear()                                    # subscriptions end with the lock
        for fn in {a[0] for a in self.plan} - self.plan_from_config:
            self._drop_plan(fn)
        for sid, st in list(self.streams.items()):
            if "host" in st.users:
                self._drop_stream_user(sid, "host", MARK_CLOSED["session_ended"])
        for cid, c in list(self.conns.items()):
            c.users.discard("host")
            if not c.users:
                self._close_conn(cid, MARK["detach"])
        for grp in self.groups.values():
            grp.release_session(self.captures)
        self._refresh()
        self._session_over()

    def _remaining(self) -> int:
        return max(0, self.expires_ms - self.now()) if self.holder is not None else 0

    def _locked(self) -> tuple[int, int, bytes]:
        owner = m.tlv(reg.CORE.tlv["locked_payload"]["owner"], self.owner) if self.owner else b""
        return m.REJECTED, m.LOCKED, struct.pack("<I", self._remaining()) + owner

    def _check(self, session: int) -> tuple[int, int, bytes] | None:
        """core §6.2 for a request (not open) with a session_id: no session holds the lock -> no_session; another
        holds it -> locked; the holder's -> None (handled)."""
        if self.holder is None:
            return m.REJECTED, m.NO_SESSION, b""                   # no resume: an ended session never continues
        if session == self.holder:
            return None
        return self._locked()

    def _open(self, t: Take, req: m.Request) -> tuple[int, int, bytes]:
        """open (core §6.4): lease_ms(u32) force(u8) [TLV owner], the id in the header; the decision table of §6.2.
        force is a boolean: any value but 0 is true (core §2.1)."""
        lease, force = t.take("IB")
        got = t.tail({OWNER})
        session = req.session
        if session == 0:
            raise Reject(m.MALFORMED)                              # session_id 0 (§4.1)
        owner = got.get(OWNER)
        if owner is not None and not 1 <= len(owner) <= reg.LIMITS["owner_max_bytes"]:
            raise Reject(m.MALFORMED)                              # owner: text of 1-32 bytes (core §6.4)
        if self.holder is not None and self.holder != session:
            if not force:
                return self._locked()
            self._release_lock()                                   # force: the old session is released first (§9)
        if session != self.holder:                                 # a new lock (a resent open keeps everything)
            self.subscribed.clear()
            self.owner = owner                                     # from the open that takes the lock, kept while held
        self.holder = self.last = session
        self.resend.clear()
        self.newest_corr = None
        # 0: the probe's default; else rounded into lease_min_ms .. lease_max_ms (core §6.4)
        lo, hi = reg.LIMITS["lease_min_ms"], min(self.lease_max_ms, reg.LIMITS["lease_max_ms"])
        self.lease_ms = self.lease_default_ms if lease == 0 else min(max(lease, lo), hi)
        self.expires_ms = self.now() + self.lease_ms
        return self._answer(struct.pack("<II", self.lease_ms, self.boot_id))

    def reboot(self, boot_id: int | None = None) -> None:
        """The probe restarts: lock, last id, the resend table, connections, streams, captures, subscriptions, the
        revision in use, the port speed, the unsaved config and the plan are gone; the saved config comes back, and the
        clock starts again from 0 (ns since boot, core §2.6a). `boot_id` None
        draws a new one, as from a hardware random source (core §6.5, C-19); passing the current one plays a probe
        whose only source repeated it - its hosts learn only that their session is gone (no_session, core §6.2)."""
        if boot_id is None:
            boot_id = self.boot_id
            while boot_id == self.boot_id:
                boot_id = secrets.randbits(32)
        self.boot_id = boot_id
        self.reboots += 1
        self._origin_ns, self._last_ns = self._raw_ns(), 0
        for fn in self.captures:
            self.captures[fn] = self._capture_from(self.decl[fn], self.inner[fn])
        for fn in self.groups:
            self.groups[fn] = self._group_from(self.decl[fn], self.inner[fn])
        self._boot()

    def lose_connections(self) -> None:
        """A wire drops every connection; their console streams close with a link-lost mark."""
        self.lose()

    def lose(self, cid: int | None = None) -> list[int]:
        """TEST HOOK: the line of connection `cid` (every connection when None) is lost for good (debug §2): the
        connection closes, its console streams get mark link-lost and close with detail 4 (connection closed), and a
        request naming it is answered no_connection. An at-boot slot on its place attaches again by itself at its next
        retry (probe.config §3.1), its bound console coming back under the same stream number (console §2). -> the
        connections closed (an unknown `cid`: none)."""
        gone = [c for c in (list(self.conns) if cid is None else [cid]) if c in self.conns]
        for c in gone:
            self._close_conn(c, MARK["link_lost"])
        self._refresh()
        return gone

    # ---- core -----------------------------------------------------------------------------------
    def _confirm(self, t: Take) -> tuple[int, int, bytes]:
        magic, lo, hi = t.bytes(4), *t.take("BB")
        if magic != m.CONFIRM_REQUEST:
            return m.REJECTED, m.MALFORMED, b""
        if self.revision == 0:                                     # v0 shape: max_frame(16) window(16) inflight flags
            return m.COMPLETED, m.SUCCESS, struct.pack("<4sBHHBB", m.CONFIRM_RESULT, 0, self.probe.max_frame,
                                                       min(self.window, 0xFFFF), self.max_inflight, 0)
        t.tail()
        if lo > hi:
            raise Reject(m.MALFORMED)                              # min_rev > max_rev (core §7.1)
        if not lo <= self.revision <= hi:
            # no revision in the range: tag 0x00, then TLV supported (min, max) - the range this probe handles
            raise unsupported_fixed(m.tlv(UNSUPPORTED_SUPPORTED, bytes([self.revision, self.revision])))
        self.revision_in_use[self._transport] = self.revision     # until the next confirm on this transport (§7.1)
        return self._answer(struct.pack("<4sBBHIBI", m.CONFIRM_RESULT, self.revision, 0, self.probe.max_frame,
                                        self.window, self.max_inflight, self.boot_id)    # boot_id (core §7.1)
                            + m.tlv(CONFIRM_TRANSPORT, bytes([self._transport])))   # the transport it came on

    def _core(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        op = req.op
        if op == m.OP_LIST:                                         # first(u16) [TLV] (core §7.2)
            first = t.take("H")
            t.tail()
            return m.COMPLETED, m.SUCCESS, self.probe.call(m.CORE_FN, op, struct.pack("<H", first))
        if op == m.OP_DESCRIBE:
            fn, first = t.take("HH")
            t.tail()
            if fn not in self.names:
                return m.REJECTED, m.UNKNOWN_FUNCTION, b""         # an fn the probe does not offer (core §4.3)
            return m.COMPLETED, m.SUCCESS, self._page(self._declarations(fn), first)
        if op == m.OP_LOCK_STATE:
            t.tail()
            owner = (m.tlv(reg.CORE.tlv["lock_state_answer"]["owner"], self.owner)
                     if self.owner and self.holder is not None else b"")
            return self._answer(struct.pack("<BI", int(self.holder is not None), self._remaining()) + owner)
        if op == m.OP_END:
            t.tail()
            self._release_lock()
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_KEEPALIVE:
            t.tail()
            return self._answer(b"")
        if op == m.OP_CLOCK:
            # core §7.7: no fixed part; lock-free (session_id 0 touches no session, lock or lease, §4.1). The clock is
            # read while handling this request, never a value read earlier
            t.tail()
            return self._answer(struct.pack("<IQ", self.boot_id, self.now_ns()))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.probe.restart (oep-if-restart) ------------------------------------------------------
    def _restart(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """restart (oep-if-restart §2): no fixed part; the lock was checked (session_required / no_session / locked).
        The answer goes first; once it is out (`after_answer`) the probe restarts, and until then nothing more is
        processed."""
        if op == OP_RESTART:
            t.tail()
            self.restarting = True
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- notifications: the emitting interface's own subscribe / unsubscribe (core §11.3) --------------------
    def _subscription(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """subscribe: min_bytes(u16) max_delay_ms(u32) [TLV] - replaces the fn's subscription atomically (the batching,
        the transport, seq from 0); unsubscribe: [TLV] - none to end succeeds doing nothing."""
        if op == m.OP_SUBSCRIBE:
            min_bytes, max_delay_ms = t.take("HI")
            t.tail()
            self.subscribed[fn] = (min_bytes, max_delay_ms)
            self.push_seq[fn] = 0                                  # seq from 0 at every subscribe (core §11.2)
            self.data_since_ms.pop(fn, None)
            return m.COMPLETED, m.SUCCESS, b""
        t.tail()
        self.subscribed.pop(fn, None)
        self.data_since_ms.pop(fn, None)
        return m.COMPLETED, m.SUCCESS, b""

    # ---- oep.probe.plan (oep-if-plan) ------------------------------------------------------------
    def _plan_op(self, plan_fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """plan_apply: role_assignment TLVs (oep-if-plan §2.1; the tag the same with or without bit 7, core §2.3);
        plan_release: n(u8) n x fn(u16) (§2.2). Every check runs before anything changes (core §4.3)."""
        if op == OP_PLAN_APPLY:
            t.tail({ROLE_ASSIGNMENT}, repeats={ROLE_ASSIGNMENT}, defer=True)
            got, tags = [], {}
            for tag, value in t.repeated:
                if len(value) != 5:
                    raise Reject(m.MALFORMED)                      # fn(u16) role(u8) channel(u16) (oep-if-plan §2.1)
                a = struct.unpack("<HBH", value)
                got.append(a)
                tags.setdefault(a[0], tag)                         # the tag as received, for unsupported (§2.5)
            if len(set(got)) != len(got) or any(fn == m.CORE_FN for fn, _, _ in got):
                raise Reject(m.MALFORMED)                          # the same (fn, role, channel) twice, or fn 0 (oep-if-plan §2.5)
            self._check_target_roles(got)                          # the whole form first ...
            named = {fn for fn, _, _ in got}
            if any(fn not in self.names for fn in named):
                raise Reject(m.UNKNOWN_FUNCTION)                   # ... then its fns ...
            t.check()                                              # ... an unknown critical TLV ...
            tag_of = lambda fn: tags.get(fn, ROLE_ASSIGNMENT)      # noqa: E731
            self._check_plan(got, held=False, tag_of=tag_of)       # ... roles and channels not declared ...
            if named & self.plan_from_config:                       # ... the settings' plan is the settings' (§2.3)
                raise unavailable("held_by_settings")
            self._check_plan(got, tag_of=tag_of)
            self._refuse_disabled(ch for _, _, ch in got)          # a disabled channel (probe.config §1): cause 5
            if self.plan_roles is not None and len([a for a in self.plan if a[0] not in named]) + len(got) > self.plan_roles:
                raise unavailable("limit")                          # plan_roles (oep-if-plan §2.1)
            self._replace_plans(named, got)
            for fn in named:
                self._uart_plan_changed(fn)
            return m.COMPLETED, m.SUCCESS, b""
        if op == OP_PLAN_RELEASE:                                 # n(u8) n x fn(u16); n = 0: every fn
            n = t.take("B")
            fns = {t.take("H") for _ in range(n)}
            t.tail()
            for fn in {a[0] for a in self.plan if not fns or a[0] in fns} - self.plan_from_config:
                self._drop_plan(fn)                                 # the settings' plans stay, n = 0 too (oep-if-plan §2.3)
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

    def _check_plan(self, got: list[tuple[int, int, int]], held: bool = True, tag_of=None) -> None:
        """plan_apply's all-or-nothing check, in core §4.3's order over the whole request: the roles and channels each
        fn declares (unsupported) for every assignment, then - `held` - no pin another fn, a slot or a
        connection holds (unavailable). `tag_of(fn)`: the tag an unsupported names (the role_assignment or
        probe.config's plan item as received)."""
        self._check_target_roles(got)
        for declared in (True, False) if held else (True,):
            self._check_plan_pass(got, declared, tag_of)

    def _check_plan_pass(self, got: list[tuple[int, int, int]], declared: bool, tag_of) -> None:
        named = {fn for fn, _, _ in got}
        others = {a for a in self.plan if a[0] not in named}
        kept = {a for a in others if not self._listens(a[0])}
        slot_pins = {p for s in self.slots.values() for p in s.pair if p != 0xFFFF}
        slot_pins |= {p for c in self.conns.values() for p in c.pair if p != 0xFFFF}   # a live connection's pins too
        analog = {fn for fn, cap in self.captures.items() if cap.analog}
        current = [None]                                           # the fn of the assignment being checked

        def not_declared(ch: int) -> Reject | None:
            # a role or channel the describe does not offer: unsupported, tag 0x90 + the channel (oep-if-plan §2.5); this pass
            # raises only its own kind (None: the other pass's)
            if not declared:
                return None
            tag = tag_of(current[0]) if tag_of else ROLE_ASSIGNMENT
            return Reject(m.UNSUPPORTED, bytes([tag]) + m.tlv(_UNA["channel"], struct.pack("<H", ch)))

        def held(ch: int, holder: int | None) -> Reject | None:
            return None if declared else unavailable("pin_in_use", ch)
        def check(r: Reject | None) -> None:
            if r is not None:
                raise r
        for fn, role, ch in got:
            current[0] = fn
            beside = [f for f, _, c in got if c == ch and f != fn] + [a[0] for a in others if a[2] == ch]
            if fn in analog:   # the analog shares its pin with nothing: another fn's plan, a slot, a connection
                if role not in self._declared_roles(fn, ch):
                    check(not_declared(ch))
                    continue
                if beside or ch in slot_pins:
                    check(held(ch, beside[0] if beside else None))
                if self._output_idle(ch) and not declared:         # its pad would leave an output idle (capture §1.2)
                    raise unavailable("held_by_settings", ch)
                continue
            if any(f in analog for f in beside):   # nor may anything come onto an analog pin
                check(held(ch, next(f for f in beside if f in analog)))
            if fn in self.captures:
                if role not in self._declared_roles(fn, ch):
                    check(not_declared(ch))
                continue
            roles = TARGET_ROLES.get(self.names.get(fn, ""), set())
            if role not in roles or ch not in self._declared_channels(fn):
                check(not_declared(ch))
                continue
            if fn in self.i2c or fn in self.spi:                   # role_channels binds the roles it lists (core §7.4)
                listed = self._listed_roles(fn)
                if role in listed and role not in self._declared_roles(fn, ch):
                    check(not_declared(ch))
                    continue
            if any(k[2] == ch for k in kept) or ch in slot_pins:
                check(held(ch, next((k[0] for k in kept if k[2] == ch), None)))
        for fn in {f for f, _, _ in got if f in self.i2c or f in self.spi}:
            current[0] = fn
            groups = [set(catalog.unpack_channel_group(v)[1]) for tag, v in self.decl[fn]
                      if tag == catalog.CHANNEL_GROUP]
            mine = {(role, ch) for f, role, ch in got if f == fn}
            if groups and mine not in groups:                      # one channel_group exactly (core §7.4)
                check(not_declared(min(ch for _, ch in mine)))

    def _declared_roles(self, fn: int, channel: int) -> set[int]:
        """The roles fn's describe offers on `channel` (role_channels)."""
        return {v[0] for tag, v in self.decl[fn] if tag == catalog.ROLE_CHANNELS and channel in _role_channels(v)}

    def _listed_roles(self, fn: int) -> set[int]:
        """The roles fn's role_channels name (the only ones it binds, core §7.4)."""
        return {v[0] for tag, v in self.decl[fn] if tag == catalog.ROLE_CHANNELS}

    def _declared_channels(self, fn: int) -> set[int]:
        """Every channel fn's describe offers in any role (role_channels and channel_group)."""
        out = set()
        for tag, v in self.decl[fn]:
            if tag == catalog.ROLE_CHANNELS:
                out.update(_role_channels(v))
            elif tag == catalog.CHANNEL_GROUP:
                out.update(c for _, c in catalog.unpack_channel_group(v)[1])
        return out

    def _replace_plans(self, fns: set[int], got) -> None:
        """The plans of `fns` become `got` as one change (oep-if-plan §2.1): a channel leaving goes to its idle state, one in
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

    # ---- what the probe does to a pin now (core §8, fixture §2-§4, capture §1.2) -----------------------
    IDLE_NAMES = {v: k.replace("_", "-") for k, v in IDLE_MODE.items()}

    def pin_state(self, ch: int) -> str:
        """TEST HOOK: the electrical state the probe gives channel `ch` now, as a word a test can compare:
        "reset" (disabled: never touched), "wire" (a live connection's pin at its rest state between exchanges),
        "wire-free" (the same after an exchange went unanswered, until one succeeds: undriven, no pull - never a pull-up
        on a clock resting low; debug §2, swio §3.2), "idle <mode>" (the idle item's, hi-z
        without one), "gpio <mode>" (after a set, or taken in its idle state), "uart-tx-high" / "input" (a fixture
        UART's TX at the plan, its RX), "open-drain" / "open-drain pull-up" (an i2c-target from configure: pulls low or
        releases, never drives high; pull-up only when it declares internal pull-ups), "miso-driven" / "miso-hi-z" /
        "input" (an spi-target from configure: MISO driven only while CS is active), "analog" (from start). Taking a
        plan changes nothing until the interface starts to use the pin (core §8), and a logic capture only listens."""
        if ch in self.disabled:
            return "reset"
        conn = next((c for c in self.conns.values() if ch in c.pair), None)
        if conn is not None:
            return "wire-free" if conn.free else "wire"
        idle = f"idle {self.IDLE_NAMES.get(self.parked.get(ch, 0), 'hi-z')}"
        for fn, role, c in sorted(self.plan):
            if c != ch or self._listens(fn):
                continue
            name = self.names.get(fn)
            if name == "oep.fixture.gpio":
                mode = self.gpio_modes.get(ch)
                return idle if mode is None else f"gpio {mode}"
            if name == "oep.fixture.uart":
                return "uart-tx-high" if role == _UART.enum["role"]["tx"] else "input"
            if fn in self.i2c:
                if self.i2c[fn].state == 0:
                    return idle                                    # state 0: released, the idle state (fixture §3)
                return "open-drain pull-up" if self.i2c_pullups.get(fn) else "open-drain"
            if fn in self.spi:
                if self.spi[fn].state == 0:
                    return idle                                    # before configure: the idle state (fixture §4)
                if role == _SPI.enum["role"]["miso"]:
                    return "miso-driven" if fn in self.spi_selected else "miso-hi-z"
                return "input"
            if fn in self.captures:                                # the analog: its pad leaves the digital function at start
                started = self.captures[fn].state not in (virtual_bench_capture.STATE["unconfigured"],
                                                          virtual_bench_capture.STATE["configured"])
                return "analog" if started or self.captures[fn].generation else idle
        return idle

    def spi_select(self, fn: int, active: bool) -> None:
        """TEST HOOK: the bus controller moves spi-target `fn`'s CS (active: selected). MISO is driven only while CS is
        active, from configure until the plan is released (fixture §4)."""
        if active:
            self.spi_selected.add(fn)
        else:
            self.spi_selected.discard(fn)

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
        make within 5 % falls back to the default 115200 8N1 (status shows what runs). No item: default."""
        item = self.config.get((ITEM["uart"], fn))
        if item is None:
            self.uart_baud.pop(fn, None)
            return
        _, baud, fmt = struct.unpack_from("<HIB", item)
        actual = self._uart_actual(baud)
        if abs(actual - baud) * 20 > baud:
            self.uart_baud.pop(fn, None)                           # the default 115200 8N1 (fixture §2)
        else:
            self.uart_baud[fn] = (actual, fmt)

    def _uart_actual(self, baud: int) -> int:
        return self.uart_clock_hz // max(1, self.uart_clock_hz // baud)

    def _uart_check(self, baud: int, fmt: int, fn: int, fmt_tag: int | None, divide: bool = True) -> int:
        """fixture §2's refusals: baud 0 -> malformed; an undefined format value or bit, and a format the UART does not
        declare -> unsupported (the format TLV's tag as received, or 0x00 for the item); a baud off by more than 5 % ->
        unsupported 0x00. -> the actual baud. `divide` False (the settings' item at set time): the range alone
        (1 .. the UART's max_clock_hz), the divider is made when the plan runs the UART (the default when it misses)."""
        if baud == 0:
            raise Reject(m.MALFORMED)
        # undefined values (2 / 3 in bit0-1, 3 in bit2-3, bits 5-7: a later revision may define them, core §2.5) and a
        # format not declared: unsupported alike
        if fmt & ~UART_FORMAT_MASK or fmt & 3 > 1 or (fmt >> 2) & 3 > 2 or fmt not in self.uart_formats.get(fn, {0}):
            raise Reject(m.UNSUPPORTED, bytes([fmt_tag if fmt_tag is not None else m.TAG_FIXED]))
        if not divide:
            if baud > self.uart_max_hz.get(fn, 3_000_000):
                raise Reject(m.UNSUPPORTED)
            return baud
        actual = self._uart_actual(baud)
        if abs(actual - baud) * 20 > baud:
            raise Reject(m.UNSUPPORTED)
        return actual

    def add_transport(self, kind: int, interface: int = 0xFF) -> int:
        """A transport the virtual bench also serves (virtual_bench_serve's TCP listener: kind 6, interface 0xFF): listed in fn 0's
        describe under the next unused index - an index is never reused (core §7.5) - which every confirm accepted on
        it reports (transports §1, core §7.1). -> its index."""
        index = max(self.transports, default=-1) + 1
        self.transports[index] = kind
        self.static[0] = tuple(self.static.get(0, ())) + (catalog.tlv(virtual_bench.CORE_TRANSPORT, bytes([index, kind, interface])),)
        self.decl[0] = self.decl.get(0, []) + [(virtual_bench.CORE_TRANSPORT, bytes([index, kind, interface]))]
        if kind in virtual_bench.SERIAL_KINDS:
            self.serial_ports.add(index)
        return index

    # ---- describe: the static declarations plus the live ones -----------------------------------
    def _declarations(self, fn: int) -> list[bytes]:
        """describe: the profile's declarations as they are (core §7.3: nothing that changes - the firmware's labels
        only, the settings' are read by get; probe.config's state is its op state), the ops tag first and as `offers`
        answers (oep.probe.link's port_speed follows `port_speed_base`: a test turns it off or on); oep.probe.restart's
        restart_max_ms is `restart_max_ms` (oep-if-restart §1)."""
        ops = {op for op in range(256) if self.offers(fn, op)}
        if fn in self.broken_ops:
            head = [catalog.tlv(catalog.OPS, self.broken_ops[fn])]
        else:
            head = [catalog.ops_tlv(ops)] if ops else []           # no op at all: no ops tag (a test's probe)
        out = head + [t for t in self.static[fn] if t[0] != catalog.OPS]
        if fn == self.restart_fn:                                  # restart_max_ms as the endpoint has it now
            out = [catalog.u32(virtual_bench.RESTART_MAX_MS_TAG, self.restart_max_ms) if t[0] == virtual_bench.RESTART_MAX_MS_TAG else t
                   for t in out]
        return out

    # ---- oep.probe.link (oep-if-link): the link test and port_speed -------------------------------------
    def _link(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """source: length(u32) [TLV] -> len(u16) data [TLV], byte k = k & 0xFF, at most max_frame - 7 (the answer's
        header and len); sink: count(u16) data [TLV] -> empty, a count past the bytes that follow malformed; port_speed
        (§3)."""
        if op == _LINK.op["source"]:
            length = t.take("I")
            t.tail()
            n = min(length, self.probe.max_frame - LINK_SOURCE_OVERHEAD)   # oep-if-link §2
            return self._answer(struct.pack("<H", n) + bytes(k & 0xFF for k in range(n)))
        if op == _LINK.op["sink"]:
            count = t.take("H")
            t.bytes(count)                                         # count past the bytes that follow: malformed
            t.tail()
            return self._answer(b"")
        if op == OP_PORT_SPEED:
            return self._port_speed(t)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- port_speed (oep-if-link §3) --------------------------------------------------------------
    @staticmethod
    def speed_actual(baud: int) -> int:
        """The rate the virtual bench's UART makes nearest `baud`: any rate in SPEED_RATES exactly, else the nearest end."""
        return min(max(baud, SPEED_RATES[0]), SPEED_RATES[1])

    def _port_speed(self, t: "Take") -> tuple[int, int, bytes]:
        """baud(u32) step(u8) verify_ms(u16) [TLV] -> baud(u32): the rate that applies. The port is the one the request
        came on: only a UART bridge (else unavailable cause 6)."""
        baud, step, verify_ms = t.take("IBH")
        t.tail()
        if step == SPEED_STEP["try"] and verify_ms == 0:
            raise Reject(m.MALFORMED)                              # verify_ms 0 in a try (oep-if-link §3)
        if step not in SPEED_STEP.values():
            raise Reject(m.UNSUPPORTED)                            # a step the definition leaves unused (§2.5): 0x00
        port = self._transport
        if self.transports.get(port) != virtual_bench.TRANSPORT["uart_bridge"]:
            raise unavailable("wrong_state")                       # only a UART bridge's port (oep-if-link §3)
        # the port's state (boot speed / trying / committed) decides which step fits (oep-if-link §3): any other is cause
        # 6, and so is a try on another port while one is raised
        if step == SPEED_STEP["try"]:
            if self.speed_state != "base":
                raise unavailable("wrong_state")                   # trying or committed (here or on another port)
            actual = self.speed_actual(baud)
            if abs(actual - baud) * 100 > baud * SPEED_TOLERANCE_PCT:
                raise unsupported_fixed()                          # the nearest rate is more than 2 % off
            self.speed_pending = ("switch", port, actual, baud, verify_ms)
            return self._answer(struct.pack("<I", actual))
        if step == SPEED_STEP["commit"]:
            if self.speed_state != "try" or port != self.speed_port or baud != self.speed_asked:
                raise unavailable("wrong_state")                   # nothing tried, committed already, or another baud
            self.speed_state = "committed"
            self.speed_good_ms = self.now()
            return self._answer(struct.pack("<I", self.speed_rate))
        if self.speed_state == "base" or port != self.speed_port:
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
        """The rate `port` runs at now when it breaks only both ways at once (virtual_bench_serial checks the unread answers)."""
        b = self.broken_rates.get(self.port_baud(port))
        return b if b is not None and b.duplex else None

    def after_answer(self) -> None:
        """The answer just handled is out (its transport calls this once the answer has left - at the old speed): a
        port_speed switch or revert it asked for happens now (`speed_after_answer`), and a restart it answered
        (oep-if-restart §2) restarts the probe now (`reboot`: a new boot_id, the saved settings, no session; the pins in their
        free state, nothing driven before). A test calling `handle` itself on a serial port index calls this as
        VirtualSerialPort does."""
        self.speed_after_answer()
        if self.restarting:
            self.reboot()

    def speed_after_answer(self) -> None:
        """The answer that asked for a switch or a revert is out (at the old speed): now do it."""
        pending, self.speed_pending = self.speed_pending, None
        if pending is None:
            return
        if pending[0] == "revert":
            self._speed_revert()
            return
        _, port, rate, asked, verify_ms = pending
        self.speed_state, self.speed_port, self.speed_rate, self.speed_asked = "try", port, rate, asked
        if rate in self.broken_rates:
            self.broken_rates[rate].carried = 0                    # `after` counts from this switch
        self.speed_until_ms = self.now() + verify_ms
        self.speed_good_ms = self.now()
        self.speed_log.append((port, rate))

    def _speed_revert(self) -> None:
        if self.speed_state == "base":
            return
        self.speed_log.append((self.speed_port, self.port_speed_base))
        self.speed_state, self.speed_port, self.speed_rate, self.speed_asked = "base", None, 0, 0

    def speed_frame(self, port: int, good: bool) -> None:
        """A candidate closed on serial port `port`: a frame (good) or not. A good frame restarts the committed rate's
        port_speed_idle_ms (oep-if-link §3, return condition 2); a broken one changes nothing."""
        if self.speed_state == "base" or port != self.speed_port or not good:
            return
        self.speed_good_ms = self.now()

    def speed_answered(self, port: int) -> None:
        """An answer went out on `port`: the committed rate's port_speed_idle_ms counts from it too (oep-if-link §3)."""
        if self.speed_state != "base" and port == self.speed_port:
            self.speed_good_ms = self.now()

    def _speed_tick(self) -> None:
        # Return condition 2 (committed, port_speed_idle_ms with no good frame) is not counted while a request executes
        # (oep-if-link §3, as the lease, §6.1). The virtual bench handles every request within one call and its clock does not
        # move meanwhile, so there is nothing to pause here: the answer restarts it (`speed_answered`).
        if self.speed_pending and self.speed_pending[0] == "revert":
            self.speed_after_answer()
        now = self.now()
        if self.speed_state == "try" and now >= self.speed_until_ms:
            self._speed_revert()                                   # no commit within verify_ms (return condition 1)
        elif self.speed_state == "committed" and now - self.speed_good_ms >= SPEED_IDLE_MS:
            self._speed_revert()                                   # return condition 2

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
            known = {_T_SCAN["max_speed"], _T_SCAN["skip"]} | ({_T_SCAN["idle_clock"]} if rvswd else set())
            got = t.tail(known, defer=True)
            skip_tlv = t.fixed(got, _T_SCAN["skip"], 2) if not count else None   # a count > 0 request: not looked at (§1)
            max_speed = t.fixed(got, _T_SCAN["max_speed"], 4)
            self._idle_clock(t, got)
            t.check()                                              # an unknown critical TLV (core §2.3)
            if max_speed is not None:
                self._check_min_speed(fn, t, struct.unpack("<I", max_speed)[0])
            for i, p in enumerate(pairs):                          # not a combination the declaration allows:
                if not self._allows(fn, p):                        # unsupported, 0x00 + its index (debug §1)
                    raise unsupported_fixed(m.tlv(_UNSUP_INDEX, bytes([i])))
            live = [c.pair for c in self.conns.values() if c.fn == fn]
            full = len(live) >= self.max_connections.get(fn, 1)   # every seat taken: the live pairs only
            for p in pairs:                                        # a named channel the settings or another hold
                self._refuse_named(fn, p)
                if full and p not in live:
                    raise unavailable("limit")                     # no seat to try it on (max_connections)
            if not pairs:                                          # the count-0 list, from `skip` on (oep-if-debug §1)
                skip = struct.unpack("<H", skip_tlv)[0] if skip_tlv is not None else 0
                pairs = [p for p in self._allowed_pairs(fn) if self._candidate(fn, p) and (not full or p in live)][skip:]
            pairs = pairs[:255]                                    # tried is a u8
            found = []                                             # a live pair keeps its speed and rest (debug §1)
            for p in pairs:
                tg = self._target(fn, p)
                if (tg.answers and tg.found) or self._conn_at(fn, p) is not None:   # a live pair: read over it
                    found.append(struct.pack("<BHHI", 1, *p, tg.dmstatus()))   # 9 bytes, no length (§2.3)
            return m.COMPLETED, m.SUCCESS, struct.pack("<BB", len(pairs), len(found)) + b"".join(found)
        if op == 0x02:                                             # attach: method(u8) [TLV] (oep-if-debug §3)
            method = t.take("B")
            known = {_T_ATTACH["max_speed"], _T_ATTACH["pins"], _T_ATTACH["reset"]} | ({_T_ATTACH["idle_clock"]} if rvswd else set())
            got = t.tail(known, defer=True)
            max_speed = t.fixed(got, _T_ATTACH["max_speed"], 4)
            if max_speed is None:
                raise Reject(m.MALFORMED)                          # max_speed is required (§1)
            idle_clock = self._idle_clock(t, got)
            reset = t.fixed(got, _T_ATTACH["reset"], 4)
            pins = t.fixed(got, _T_ATTACH["pins"], 4)
            t.check()
            if method not in ATTACH_METHOD.values():
                raise Reject(m.UNSUPPORTED)                        # a method a later revision may define: 0x00 (§2.5)
            self._check_min_speed(fn, t, struct.unpack("<I", max_speed)[0])
            if reset is not None:
                channel, hold_ms = struct.unpack("<HH", reset)
                if channel not in self.reset_channels.get(fn, set()) or hold_ms > self.max_op_ms:
                    # not a reset line here (§3), or longer than one request may take: the tag as received
                    raise Reject(m.UNSUPPORTED, bytes([t.received(_T_ATTACH["reset"])]))
            pair = self._pick_pair(fn, pins, t)
            if reset is not None:
                self._refuse_disabled([channel])                   # a disabled reset line: cause 5 (probe.config §1)
                if self._output_idle(channel):                     # its idle drives it: nothing executed (debug §1)
                    raise unavailable("held_by_settings", channel)
                if any(a[2] == channel for a in self.plan):
                    raise unavailable("pin_in_use", channel)
            tg = self._target(fn, pair)
            speed = min(4_000_000, struct.unpack("<I", max_speed)[0])
            cid = self._conn_at(fn, pair)
            flags = 0
            if reset is not None and tg.resets_through(channel):
                tg.silent_until_reset = False                      # held in reset and let go: it answers again
                if not self._settled(tg):                          # silent past max_op_ms (debug §3)
                    if cid is not None:                            # the connection kept, the target was reset
                        self._host_reset(cid, MARK_RESET["attach_reset"])
                    return m.COMPLETED, m.FAILED, bytes([LINE])
            bring_up = cid is None or reset is not None            # a new connection, or the reset TLV (debug §1)
            if cid is None:
                if not tg.answers:
                    return m.COMPLETED, m.FAILED, bytes([LINE])    # failed: status [TLV] (common §3)
                cid = self._seat(fn, pair, tg, speed)              # a failed attach consumed no number
                if tg.havereset:
                    tg.havereset, flags = False, flags | ATTACH_FLAGS["havereset_acked"]
            else:
                flags |= ATTACH_FLAGS["existing"]
                bring_up |= speed < self.conns[cid].speed          # lowered for max_speed: brought up again
                self.conns[cid].speed = min(self.conns[cid].speed, speed)
            c = self.conns[cid]
            if idle_clock is not None:                             # only a carried idle_clock changes the rest; a join
                c.idle_clock = idle_clock                          # without it keeps the connection's (debug §1, §3)
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
            tail = b"" if c.tid is None else m.tlv(_T_ATTACH_ANSWER["target_id"],
                                                   bytes([TARGET_ID_SCHEME["dmi_7f"]]) + struct.pack("<I", c.tid))
            if tg.halted:
                flags |= ATTACH_FLAGS["halted"]
                tail += m.tlv(_T_ATTACH_ANSWER["dpc"], struct.pack("<I", tg.dpc))
            if tg.search_retries is not None and bring_up:         # the bring-up's extra attempts (debug §1): only when
                tail += m.tlv(_T_ATTACH_ANSWER["search_retries"],  # one ran; saturating at 0xFFFF
                              struct.pack("<H", min(tg.search_retries, 0xFFFF)))
            self._refresh()
            return self._answer(struct.pack("<HIBI", cid, tg.dmstatus(), flags, c.speed) + tail)
        if op == 0x03:                                             # detach
            cid = t.take("H")
            got = t.tail({_T_DETACH["force"]})
            t.fixed(got, _T_DETACH["force"], 0)                    # length 0; another length: malformed (core §2.3)
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
                rows.append(struct.pack("<HHHIBBBB", cid, *c.pair, c.speed, users, slot, 1 if tid else 0, len(tid)) + tid)
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

    def _pick_pair(self, fn: int, pins: bytes | None, t: Take) -> tuple[int, int]:
        """attach's pin pair (debug §1): the pins TLV's - not one the declaration allows: unsupported with the tag as
        received; a channel disabled, held, or with an output idle: unavailable - or, without pins, the wire's one live
        connection, else its one candidate (a disabled channel or one with an idle item is not a candidate)."""
        if pins is not None:
            pair = struct.unpack("<HH", pins)
            if not self._allows(fn, pair):
                raise Reject(m.UNSUPPORTED, bytes([t.received(_T_ATTACH["pins"])]))   # core §2.3
            self._refuse_named(fn, pair)
            return pair
        live = [c.pair for c in self.conns.values() if c.fn == fn]
        if len(live) == 1:
            return live[0]                                         # no pins: the wire's one live connection
        offered = self._allowed_pairs(fn)
        allowed = [p for p in offered if not set(p) & self.disabled and not any(
            self._idle_mode(ch) is not None for ch in p if ch != 0xFFFF)]   # never a disabled or idle-item channel
        if not live and len(offered) == 1 and not allowed:
            self._refuse_named(fn, offered[0], any_idle=True)     # its one pair is left out: cause 5
        if live or len(allowed) != 1:
            raise Reject(m.UNAVAILABLE)                            # the host chooses among several
        return allowed[0]

    def _candidate(self, fn: int, pair: tuple[int, int]) -> bool:
        """A pair the count = 0 sequence lists (debug §1): no channel disabled, held by something else, or with an
        idle item (any mode) in the settings."""
        chans = {ch for ch in pair if ch != 0xFFFF}
        return not (chans & self.disabled or chans & self._held(fn, pair)
                    or any(self._idle_mode(ch) is not None for ch in chans))

    def _refuse_named(self, fn: int, pair: tuple[int, int], any_idle: bool = False) -> None:
        """A pair a request names (scan's list, attach's pins): a disabled channel or an output idle (any idle with
        `any_idle`) -> unavailable cause 5; held by a plan, a slot or another connection -> cause 1 (debug §1, core
        §8.1); each with the channel."""
        self._refuse_disabled(pair)
        for ch in pair:
            if ch != 0xFFFF and (self._output_idle(ch) or (any_idle and self._idle_mode(ch) is not None)):
                raise unavailable("held_by_settings", ch)
        held = set(pair) & self._held(fn, pair)
        if held:
            raise unavailable("pin_in_use", min(held))

    def _idle_clock(self, t: Take, got: dict[int, bytes]) -> int | None:
        """rvswd's idle_clock TLV (u8 enum: 0 high, 1 low): another value is one the definition leaves unused ->
        unsupported with the tag as received, critical or not (core §2.3, §2.5)."""
        tag = _T_ATTACH["idle_clock"]
        value = t.fixed(got, tag, 1)
        if value is None:
            return None
        if value[0] not in reg.WIRE_RVSWD.enum["idle_clock"].values():
            raise t.refuse(tag)
        return value[0]

    def _check_min_speed(self, fn: int, t: Take, hz: int) -> None:
        """max_speed below the wire's min_clock_hz (when declared): unsupported, tag 0x01 as received (debug §1)."""
        low = self._own(fn, catalog.MIN_CLOCK_HZ, "I", 0)
        if hz < low:
            raise Reject(m.UNSUPPORTED, bytes([t.received(_T_ATTACH["max_speed"])]))

    def _conn_at(self, fn: int, pair: tuple[int, int]) -> int | None:
        return next((cid for cid, c in self.conns.items() if c.fn == fn and c.pair == pair), None)

    def _seat(self, fn: int, pair: tuple[int, int], tg: VirtualTarget, speed: int, evict: bool = True) -> int:
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

    def _target_of(self, cid: int) -> VirtualTarget:
        c = self.conns[cid]
        return self._target(c.fn, c.pair)

    # ---- oep.target.riscv-dm --------------------------------------------------------------------
    @staticmethod
    def _outcome(status: int, done: int) -> int:
        return m.SUCCESS if status == OK else (m.PARTIAL if done else m.FAILED)

    def _dm(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """oep.target.riscv-dm (debug §4); `_dm_op` does the op. An op whose connection's target does not answer
        on the wire (`VirtualTarget.answers` False) fails with status line and nothing done: its exchanges went unanswered,
        and from then until an exchange succeeds the probe rests the connection's lines in the free state (debug §2,
        `Connection.free`, `pin_state` "wire-free")."""
        try:
            return self._dm_op(fn, op, t)
        except _Unanswered as e:
            e.conn.free = True
            return m.COMPLETED, m.FAILED, DM_LINE_FAILED[op]

    def _exchange(self, cid: int) -> VirtualTarget:
        """A target op's exchanges on connection `cid` (after the request's checks): the target, or _Unanswered.
        A success after failures restores the connection's rest state (debug §2: rvswd §3.1, swio §3.2)."""
        c = self._connection(cid)
        tg = self._target_of(cid)
        if not tg.answers:
            raise _Unanswered(c)
        c.free = False
        return tg

    def _dm_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """Each op reads and checks its whole request first - malformed, then unsupported (core §4.3 orders 5 / 6) -
        and only then looks up the connection (no_connection; every check before any change, core §4.3)."""
        conn = t.take("H")
        target = lambda: self._exchange(conn)
        if op == _RV.op["dmi"]:
            n = t.take("H")
            steps = []
            for _ in range(n):
                kind = t.take("B")
                if kind not in STEP_ARGS:
                    raise Reject(m.MALFORMED)                      # its length is unknown: the rest is unreadable
                steps.append((kind, t.take(STEP_ARGS[kind])))
            t.tail()
            # the time-based waits only: 0x04's wait_us and 0x05's max_us; 0x03 is bounded by its count (debug §4.1)
            waits_us = sum(a if k == STEP["wait_us"] else a[3] if k == STEP["poll_us"] else 0 for k, a in steps)
            if waits_us > self.max_op_ms * 1000:
                raise Reject(m.UNSUPPORTED)                        # longer than one request may take (debug §4.1)
            tg = target()
            tg.begin_dmi()
            done, status, values = 0, OK, []
            for kind, args in steps:
                if kind == STEP["write"]:
                    address, value = args
                    if address in tg.fail_write:
                        status = LINE
                        break
                    tg.write_dmi(address, value)
                elif kind == STEP["read"]:
                    v = tg.read_dmi(args)
                    if v is None:
                        status = LINE
                        break
                    values.append(v)
                elif kind in (STEP["poll_reads"], STEP["poll_us"]):
                    address, mask, want, limit = args
                    tries = limit if kind == STEP["poll_reads"] else max(1, limit // 100)
                    for _ in range(max(1, tries)):
                        v = tg.read_dmi(address)
                        if v is None or v & mask == want:
                            break
                    if v is None:                                  # cut off by the line: no value (debug §4.1)
                        status = LINE
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
            tg = target()
            if tg.halted:
                return m.COMPLETED, m.SUCCESS, bytes([OK])         # already halted: nothing, ok (debug §4.2)
            if tg.halt_stuck:                                      # allhalted never seen: haltreq cleared (debug §4.2)
                tg.haltreq = False
                return m.COMPLETED, m.FAILED, bytes([TIMEOUT])
            tg.halted = True
            return m.COMPLETED, m.SUCCESS, bytes([OK])
        if op == _RV.op["resume"]:
            t.tail()
            tg = target()
            if tg.resume_misses:                                  # a CH32V006 now and then: the request does not take
                tg.resume_misses -= 1
                return m.COMPLETED, m.FAILED, bytes([STATE])
            if tg.halted:
                tg.halted, tg.dpc = False, tg.dpc + 0x40
            return m.COMPLETED, m.SUCCESS, bytes([OK])
        if op == _RV.op["reset"]:
            mode = t.take("B")
            t.tail()                                               # revision 1 defines no request TLV (0x01 reserved)
            if mode not in RESET_MODE.values():
                raise Reject(m.UNSUPPORTED)                        # a mode the definition leaves unused: 0x00 (§2.5)
            tg = target()
            tg.havereset = True
            self._host_reset(conn, MARK_RESET["ndmreset"])
            if not self._settled(tg):                              # the DM still silent at max_op_ms (§4.3)
                return m.COMPLETED, m.FAILED, struct.pack("<BBI", LINE, 0, 0)   # the connection kept
            tg.halted = mode == 2
            tg.dpc = tg.reset_vector if mode == 2 else tg.reset_vector + 0x200
            # debug §4.3: status flags(bit0 reached the mode's state, bit1 confirmed by the pc - mode 1) pc
            flags, pc = (0b01, 0) if mode == 0 else ((0b11, tg.dpc) if mode == 1 else (0b01, tg.dpc))
            return self._answer(struct.pack("<BBI", OK, flags, pc))
        if op == _RV.op["step"]:
            t.tail()
            tg = target()
            if not tg.halted:
                return m.COMPLETED, m.FAILED, struct.pack("<BBII", STATE, 0, tg.dpc, tg.dpc)
            before = tg.dpc
            if tg.step_stuck is not None:
                # not back in debug mode: haltreq, and a wait for the halt (debug §4.2)
                if tg.step_stuck == "halts":                       # halted: dcsr.step cleared, DATA1 / DATA0 restored
                    tg.dpc, tg.haltreq, tg.dcsr_step = tg.dpc + 0x20, False, False
                    return m.COMPLETED, m.FAILED, struct.pack("<BBII", STATE, 1, before, tg.dpc)
                tg.halted, tg.haltreq, tg.dcsr_step = False, False, True   # runs on, dcsr.step may still be set
                return (m.COMPLETED, m.FAILED, struct.pack("<BBII", STATE, 0, before, before)
                        + m.tlv(STEP_LEFT, b""))
            tg.dpc += 4
            return m.COMPLETED, m.SUCCESS, struct.pack("<BBII", OK, 1, before, tg.dpc)
        if op == _RV.op["read_block"]:
            address, count = t.take("IH")
            t.tail()
            if address % 4:
                raise Reject(m.MALFORMED)                          # not a word address (debug §4.5)
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise unsupported_fixed()                          # past the declared max_length (bytes; debug §4.5)
            tg = target()
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
            words = struct.unpack(f"<{count}I", t.bytes(4 * count))
            t.tail()
            if address % 4:
                raise Reject(m.MALFORMED)
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise unsupported_fixed()                          # past the declared max_length (debug §4.5)
            tg = target()
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
            if timeout_ms > self.max_op_ms:
                raise Reject(m.UNSUPPORTED)                        # longer than one request may take (debug §4.4)
            tg = target()
            not_run = struct.pack("<BIIB", RUN_STOPPED["not_run"], 0, 0, 0)   # stopped 3, dpc invalid, nvals 0 (Q1)
            if not tg.halted:                                      # nothing prepared on a running hart: not run
                return m.COMPLETED, m.FAILED, bytes([STATE]) + not_run
            if set(regs) & tg.fail_regs:                           # a register write before the run failed on the line
                return m.COMPLETED, m.FAILED, bytes([LINE]) + not_run   # not run, still halted (debug §4.4)
            tg.regs.update(regs)
            if timeout_ms == 0:                                    # the limit passes at once: halted where it began
                tg.dpc = pc
                values = [tg.regs.get(r, 0) for r in outs]
                return (m.COMPLETED, m.FAILED, struct.pack(f"<BBIIB{n_out}I", TIMEOUT, RUN_STOPPED["timeout_halted"],
                                                           pc, 0, n_out, *values))
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

    def _settled(self, tg: VirtualTarget) -> bool:
        """The wait after a reset's release for a target that restarts by itself (debug §3, §4.3): the DM answers after
        tg.restart_ms; the probe answers within max_op_ms. -> whether it answered in time (the wait goes in
        `settle_log`)."""
        self.settle_log.append(min(tg.restart_ms, self.max_op_ms))
        return tg.restart_ms <= self.max_op_ms

    def _host_reset(self, cid: int, detail: int) -> None:
        """A reset of the target on connection `cid` - riscv-dm reset (detail 1 ndmreset) or an attach's reset TLV
        (detail 3): its streams get mark reset (common §1.3)."""
        now = self.now_ns()
        for (c, _), sid in self.stream_keys.items():
            if c == cid and not self.streams[sid].closed:
                self.streams[sid].add_mark(MARK["reset"], now, detail)

    # ---- position streams (console, fixture.uart) -----------------------------------------------
    def _stream_args(self, op: int, t: Take):
        """A position stream op's request (common §1), read and checked before anything is looked up (core §4.3):
        read's from 4 or more -> unsupported (a from 3 arg above 0xFF names no mark kind: from now); a write of count 0
        takes nothing and succeeds (accepted = count)."""
        if op == _CON.op["read"]:
            frm, arg, mx = t.take("BQH")
            t.tail()
            if frm > 3:
                raise Reject(m.UNSUPPORTED)                        # a later revision may define it: 0x00 (§2.5)
            return frm, arg, mx
        if op == _CON.op["marks"]:
            frm = t.take("I")
            t.tail()
            return frm
        if op == _CON.op["mark"]:
            value = t.take("B")
            t.tail()
            return value
        if op == _CON.op["write"]:
            count = t.take("H")
            data = t.bytes(count)
            t.tail()
            return data
        if op in (_CON.op["clear"], _CON.op.get("close", -1)):
            t.tail()
        return None

    def _stream_op(self, s: Stream, op: int, args, t: Take, accept: int | None) -> tuple[int, int, bytes]:
        """read / marks / clear / mark / write of a position stream (common §1). `accept`: what a write takes (a
        fixture UART's queue); None: a console, its send queue (`send_queue`)."""
        if op == _CON.op["read"]:
            frm, arg, mx = args
            if frm == 0:
                pos = arg
            elif frm == 1:
                pos = s.base
            elif frm == 2:
                pos = s.end
            else:
                hits = [mk for mk in s.marks if arg == 0 or mk[2] == arg]
                pos = hits[-1][1] if hits else s.end                 # no such mark: from now (common §1.2)
            flags = 0
            if pos < s.base:
                pos, flags = s.base, 2
            if pos >= s.end:
                return m.COMPLETED, m.SUCCESS, struct.pack("<QBH", s.end, 0, 0)   # at or past the write position
            budget = self.probe.max_frame - m.RESULT_HEADER - 11    # within max_frame (common §1.2)
            data = bytes(s.data[pos - s.base:pos - s.base + min(mx, budget)])
            if pos + len(data) < s.end:
                flags |= 1
            return m.COMPLETED, m.SUCCESS, struct.pack("<QBH", pos, flags, len(data)) + data   # start flags len data
        if op == _CON.op["marks"]:
            hits = [mk for mk in s.marks if m.serial_diff(mk[0], args) >= 0]
            page = hits[:self.MARKS_PER_ANSWER]
            body = struct.pack("<BB", int(len(hits) > len(page)), len(page))
            body += b"".join(struct.pack("<IQBQB", *mk) for mk in page)   # 22 bytes each, time_ns u64 (common §1.3)
            return self._answer(body)
        if s.closed:
            raise unavailable("wrong_state")                       # a closed stream: read / marks only (console §2)
        if op == _CON.op["clear"]:
            s.drop_oldest(len(s.data))
            s.add_mark(MARK["clear"], self.now_ns())
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["mark"]:
            s.add_mark(MARK["host"], self.now_ns(), args)
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["write"]:
            data, count = args, len(args)
            if accept is None:                                     # a console: its send queue (console §2)
                carries = s.mechanism in self.console_feed         # SDI has no host -> target way (§3.1): 0
                took = min(count, self.send_queue - len(s.queue)) if carries else 0
                if took:
                    if not s.queue:
                        s.fed_ms = self.now()                      # the first poll that can take them: a ms on
                    s.queue += data[:took]
            else:
                took = min(count, accept)                          # what fit the UART's transmit queue
                s.written += data[:took]
            return self._answer(struct.pack("<H", took),          # all = success, fewer = partial, none = failed
                                m.SUCCESS if took == count else m.PARTIAL if took else m.FAILED)   # (common §1.4)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.target.console ---------------------------------------------------------------------
    def _console(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _CON.op["open"]:
            conn, mech = t.take("HB")
            t.tail()
            if mech not in self.mechanisms:
                raise Reject(m.UNSUPPORTED)                        # not a mechanism this probe opens (0xFF included)
            self._connection(conn)                                 # no_connection, or unavailable 6 for a stream's number
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
                rows.append(struct.pack("<HHBBB", sid, s.conn, s.mechanism, users,
                                        STREAM_STATE["closed"] if s.closed else STREAM_STATE["open"]))
            return m.COMPLETED, m.SUCCESS, self._paged(rows, first)
        if op not in _CON.op.values():
            return m.REJECTED, m.UNKNOWN_OPERATION, b""
        sid = t.take("H")
        args = self._stream_args(op, t)                            # the request first (core §4.3 orders 5 / 6) ...
        s = self._stream(sid)                                      # ... then no_connection (unavailable 6: a connection's)
        if op == _CON.op["close"]:
            if not s.closed:
                self._drop_stream_user(sid, "host", MARK_CLOSED["all_released"])
            return m.COMPLETED, m.SUCCESS, b""                     # a closed stream: nothing, ok (console §1)
        return self._stream_op(s, op, args, t, None)

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
        s.queue.clear()                                            # the send queue goes with the stream (console §2)

    def console_take(self, sid: int | None = None) -> None:
        """TEST HOOK: the target takes everything console stream `sid`'s send queue holds (every stream when None), as
        after enough polls (console §2, §3)."""
        for k, st in self.streams.items():
            if (sid is None or k == sid) and st.queue:
                st.written += st.queue
                st.queue.clear()

    def _console_poll(self) -> None:
        """The probe's polling of the console streams (console §2, §3): one poll per ms on the timers' clock while the
        hart runs and answers, each handing the queue's head to the target - dmseq 2 bytes, DMDATA 3."""
        now = self.now()
        for st in self.streams.values():
            if not st.queue or st.closed or now <= st.fed_ms or st.conn not in self.conns:
                continue
            tg = self._target_of(st.conn)
            if tg.answers and not tg.halted:
                n = min(len(st.queue), (now - st.fed_ms) * self.console_feed.get(st.mechanism, 0))
                st.written += st.queue[:n]
                del st.queue[:n]
            st.fed_ms = now

    def emit(self, sid: int, data: bytes) -> None:
        """The target writes to its console stream `sid`."""
        self.streams[sid].data += data

    def target_says(self, data: bytes, target: VirtualTarget | None = None) -> None:
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
            # drive (fixture §1.1, repeats): gpio revision 1 defines it, so every probe implements the tag - one without
            # drive_levels refuses it unsupported
            t.tail({GPIO_SET_DRIVE}, repeats={GPIO_SET_DRIVE})
            index_tag = _GPIO.tlv["unavailable_payload"]["index"]
            drives = self._drive_forms(t, pairs)                   # every check before anything changes (core §4.3)
            for i, (ch, mode) in enumerate(pairs):
                # a mode the definition leaves unused (7 or more) or one it does not drive (fixture §1)
                if mode > GPIO_MODE_MAX or not self.gpio_allowed.get(fn, 0xFF) >> mode & 1:
                    raise unsupported_fixed(m.tlv(_UNA["channel"], struct.pack("<H", ch)), m.tlv(index_tag, bytes([i])))
            drives = self._drive_levels_of(drives)
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
                outside = self.gpio_world(ch, mode) if self.gpio_world and mode in (0, 1, 2, 6) else None
                levels.append(outside if outside is not None else
                              {1: 1, 3: 0, 4: 1, 5: 0, 6: 1}.get(mode, self.gpio_inputs.get(ch, 0)))
            return self._answer(bytes([len(levels)]) + bytes(levels))   # n(u8) n x level [TLV] (fixture §1)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _drive_forms(self, t: Take, pairs: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
        """set's drive TLVs (fixture §1.1), their form: malformed (the whole request) for a value of another length than
        2, index n or more, the same index twice, an element whose mode is not 3 / 4. -> (tag as received, index,
        level)."""
        seen, drives = set(), []
        for tag, value in t.repeated:
            if tag & 0x7F != GPIO_SET_DRIVE:
                continue
            if len(value) != 2:
                raise Reject(m.MALFORMED)
            index, level = value
            if index >= len(pairs) or index in seen or pairs[index][1] not in OUTPUT_MODES:
                raise Reject(m.MALFORMED)
            seen.add(index)
            drives.append((tag, index, level))
        return drives

    def _drive_levels_of(self, drives: list[tuple[int, int, int]]) -> dict[int, int]:
        """-> {element index: level}. A level past drive_levels (0xFF, the default, aside), and any drive on a probe
        without drive_levels: rejected unsupported with the tag as received (fixture §1.1)."""
        out = {}
        for tag, index, level in drives:
            chosen = self._drive_level(level)
            if chosen is None:
                raise Reject(m.UNSUPPORTED, bytes([tag]))
            out[index] = chosen
        return out

    # ---- oep.fixture.uart -----------------------------------------------------------------------
    def _uart(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        fmt_tag = _UART.tlv["configure"]["format"]
        if op == _UART.op["configure"]:
            baud = t.take("I")
            got = t.tail({fmt_tag})
            fmt = t.fixed(got, fmt_tag, 1)                         # another length: malformed (core §2.3)
            fmt = b"\0" if fmt is None else fmt
            actual = self._uart_check(baud, fmt[0], fn, t.received(fmt_tag))   # unhandled: unsupported, bit 7 or not
            if fn not in self.uarts:
                raise unavailable("wrong_state")                   # no pins: the plan has neither RX nor TX (fixture §2)
            self.uart_baud[fn] = (actual, fmt[0])
            self.uart_session_cfg.add(fn)                          # a session's configure beats the uart item
            return self._answer(struct.pack("<I", actual))
        if op == _UART.op["status"]:                               # lock-free: baud(u32) format(u8) (fixture §2)
            t.tail()
            baud, fmt = self.uart_baud.get(fn, (reg.LIMITS["uart_default_baud"], 0))
            return self._answer(struct.pack("<IB", baud, fmt))
        if op not in (_CON.op["read"], _CON.op["marks"], _CON.op["mark"], _CON.op["write"], _CON.op["clear"]):
            return m.REJECTED, m.UNKNOWN_OPERATION, b""
        args = self._stream_args(op, t)                            # the request first (core §4.3 orders 5 / 6)
        s = self.uarts.get(fn)
        if s is None:
            raise unavailable("wrong_state")                       # the plan makes the stream (fixture §2)
        if op == _UART.op["write"] and not any(
                a[0] == fn and a[1] == _UART.enum["role"]["tx"] for a in self.plan):
            raise unavailable("wrong_state")                       # a plan without TX: nothing to write on (△6)
        return self._stream_op(s, op, args, t, self.uart_accept)

    def uart_rx(self, fn: int, data: bytes) -> None:
        """Bytes arrive on fixture UART `fn`'s RX."""
        self.uarts[fn].data += data

    # ---- oep.fixture.i2c-target (fixture §3) ------------------------------------------------------
    def _i2c_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        st, (max_length, features, depth, max_stretch) = self.i2c[fn], self.target_decl[fn]
        ops = _I2C.op
        planned = any(a[0] == fn for a in self.plan)
        if op == ops["configure"]:                                 # address(u8) [TLV]; every check before any change
            address = t.take("B")
            t.tail()
            if address > 0x7F:
                raise Reject(m.MALFORMED)                          # a 7-bit address (excluded for every revision)
            if address <= 0x07 or address >= 0x78:
                raise unsupported_fixed()                          # reserved by I2C: general call, 10-bit, ... (△5)
            if not planned:
                raise unavailable("wrong_state")                   # before the plan (cause 6)
            self.i2c[fn] = I2cState(state=1, address=address, stretch_us=st.stretch_us)   # made anew, stretch kept
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
        if op == ops["preload_tx"]:                                # count(u16) data -> nothing (fixture §3)
            count = t.take("H")
            data = t.bytes(count)
            t.tail()
            if count == 0:
                raise Reject(m.MALFORMED)
            if count > max_length:
                raise unsupported_fixed()
            if st.state == 0:
                raise unavailable("wrong_state")                   # cause 6
            if len(st.tx) >= depth:
                raise unavailable("limit")                         # every slot holds an unread preload
            st.tx.append(data)
            return self._answer(b"")
        if op == ops["status"]:                                    # lock-free: state queued rx_frames tx_slots errors
            t.tail()
            return self._answer(struct.pack("<BBIBI", st.state, min(len(st.queue), 255), st.rx_frames,
                                            min(len(st.tx), 255), st.errors))
        if op == ops["stretch"]:                                   # offered by the ops tag (`offers`, the header)
            us = t.take("I")
            t.tail()
            if us > max_stretch:
                raise unsupported_fixed()                          # past describe's max_stretch_us
            st.stretch_us = us                                     # any state; configure keeps it
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def i2c_write(self, fn: int, data: bytes, address: int | None = None) -> bool:
        """TEST HOOK: a bus controller writes `data` to i2c-target `fn` in one transaction (START, address + W, data,
        STOP). -> whether the target ACKed its address (configured, and `address` None or its own). A write with data
        is one frame (fixture §3): more than max_length bytes are cut to max_length (errors + 1); a full queue drops the
        frame (errors + 1). An address-only write (empty data) counts nothing."""
        st, (max_length, _, depth, _) = self.i2c[fn], self.target_decl[fn]
        if st.state == 0 or (address is not None and address != st.address):
            return False
        if not data:
            return True                                            # address only: nothing counts
        if len(data) > max_length:
            data = data[:max_length]
            st.errors += 1
        if len(st.queue) >= depth:
            st.errors += 1                                         # the queue overflows: the new frame goes
            return True
        st.queue.append((bytes(data), self.now_ns()))
        st.rx_frames += 1
        return True

    def i2c_read(self, fn: int, n: int, address: int | None = None) -> bytes | None:
        """TEST HOOK: a bus controller reads n bytes from i2c-target `fn` in one transaction. -> the bytes, or None
        when the address is not ACKed. The oldest preloaded slot answers (cut or padded with 0xFF to n: the next read
        starts at the next slot); no slot: 0xFF (fixture §3)."""
        st = self.i2c[fn]
        if st.state == 0 or (address is not None and address != st.address):
            return None
        if st.tx:
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
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def spi_transfer(self, fn: int, mosi: bytes, bits: int | None = None) -> bytes:
        """TEST HOOK: a bus controller runs one transaction on spi-target `fn` (CS low, `bits` clocks - 8 a MOSI byte
        by default - CS high). -> the MISO bytes: the armed tx, 0 past it and when not armed. Armed: the MOSI bytes
        (bits / 8 rounded up) up to the armed length (more is an error) and the bits are queued, the wait ends; a full
        queue drops them (an error). Not armed: MOSI dropped, an error. errors grows by 1 at most per transaction
        (fixture §4). Every transaction of a configured target counts; a target not configured sees nothing. 0 bits (CS edges without SCK) is no transaction: nothing counts,
        an arm keeps waiting."""
        st, (_, _, depth, _) = self.spi[fn], self.target_decl[fn]
        n = len(mosi)
        bits = 8 * n if bits is None else bits
        self.spi_select(fn, True)                                  # CS active: MISO driven for this transaction
        try:
            return self._spi_transaction(fn, st, depth, mosi, n, bits)
        finally:
            self.spi_select(fn, False)

    def _spi_transaction(self, fn: int, st: "SpiState", depth: int, mosi: bytes, n: int, bits: int) -> bytes:
        if st.state == 0 or bits == 0:
            return bytes(n)
        st.transactions += 1
        if st.armed is None:
            st.errors += 1
            return bytes(n)
        length, tx = st.armed
        st.armed = None
        got = (bits + 7) // 8
        if got > length or len(st.queue) >= depth:
            st.errors += 1                                         # past the armed length, or the queue full: once
        if len(st.queue) < depth:
            st.queue.append((bits, bytes(mosi[:min(got, length)]), self.now_ns()))
        return (tx + bytes(n))[:n]

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
            seen, plans = set(), {}
            try:
                tlvs = m.split_tlvs(t.data) if t.data else []
            except m.ProtocolError:
                raise Reject(m.MALFORMED) from None
            as_sent = {}
            undeclared = None                                      # the first item this probe does not handle
            for received, value in tlvs:
                tag = received & 0x7F                              # kept without the critical bit
                if tag not in self.items:
                    undeclared = undeclared if undeclared is not None else received   # the tag as received (§2)
                    continue                                       # refused after every item's form (core §4.3)
                size = _item_size(tag, value)
                if size is not None and len(value) != size:
                    raise Reject(m.MALFORMED)                      # a value of another length, bit 7 or not (§1)
                if tag != ITEM["plan"]:
                    as_sent[(tag, self._item_key(tag, value))] = received
                if tag == ITEM["plan"]:                            # one item per assignment, key (fn, role, channel)
                    key = struct.unpack("<HBH", value)
                    if (tag, key) in seen:
                        raise Reject(m.MALFORMED)
                    seen.add((tag, key))
                    plans.setdefault(key[0], []).append(value)
                    as_sent.setdefault((tag, key[0]), received)
                    continue
                key = self._item_key(tag, value)
                if (tag, key) in seen:
                    raise Reject(m.MALFORMED)                      # the same key twice in one set
                seen.add((tag, key))
                new[(tag, key)] = value
            for fn, values in plans.items():
                new[(ITEM["plan"], fn)] = values
            self._apply_config(new, changed_slots={k for t_, k in seen if t_ == ITEM["slot"]}, received=as_sent,
                               undeclared=undeclared)
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self._hash(self.config))
        if op == _CFG.op["unset"]:                                 # n(u8) n x (len(u8) tag(u8) key)
            n = t.take("B")
            keys, undeclared = [], None
            for _ in range(n):
                row = Take(t.bytes(t.take("B")))
                tag = row.take("B")
                if tag not in self.items:
                    undeclared = tag if undeclared is None else undeclared   # after the whole form (core §4.3)
                    continue
                keys.append((tag, self._item_key(tag, row.data[row.at:])))
            t.tail()
            if undeclared is not None:
                raise Reject(m.UNSUPPORTED, bytes([undeclared]))
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
                raise unavailable("storage_full")                  # max_bytes: the items' TLV bytes (§2)
            self.saved = dict(self.config)
            self.saved_ids = {fn: self.identity[fn] for fn in self._referenced(self.config) if fn in self.identity}
            self.saved_reason = 0
            self.saved_hash = self._hash(self.config)
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self._hash(self.config))
        if op == _CFG.op["erase"]:
            t.tail()
            if not self.storage_max:
                raise Reject(m.UNSUPPORTED)
            self.saved, self.saved_ids, self.saved_reason, self.saved_hash = None, {}, 0, 0
            return m.COMPLETED, m.SUCCESS, b""
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _state_answer(self, first_slot: int, first_bind: int) -> bytes:
        """probe.config §3.3: more storage_state storage_hash unreadable_reason, the slot states from first_slot and
        the bind states from first_bind that fit the frame. storage_hash: the hash the settings had when the saved ones
        became the current ones (at boot or by save) - get's hash while nothing changed since (0: none or unreadable)."""
        saved_hash = self.saved_hash if self.saved is not None and not self.saved_reason else 0
        state = 0 if self.saved is None else 2 if self.saved_reason else 1
        slots = [self._slot_state(n) for n in sorted(self.slots)][first_slot:]   # count x element (core §2.3)
        binds = [self._bind_state(p) for p in sorted(self.binds)][first_bind:]
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
        """get's order (probe.config §2): tag order, then key order (plan by (fn, role, channel)), the items' bytes as
        the host sent them, each as its TLV (core §2.2)."""
        rows = []
        for (tag, key), value in config.items():
            for v in (value if isinstance(value, list) else [value]):
                sort_key = struct.unpack("<HBH", v[:5]) if isinstance(value, list) else (key,)
                rows.append((tag, sort_key, m.tlv(tag, v)))
        return [r[2] for r in sorted(rows)]

    HASH_SEED = 0x4F45                                      # the virtual bench's own way to make the hash (the probe's choice, §2)

    def _hash(self, config: dict | None) -> int:
        """A u32 that changes with the settings (probe.config §2: how it is made is the probe's; a host never computes
        it): here a CRC-32 of get's bytes, seeded so no host can take it for a rule."""
        return zlib.crc32(b"".join(self._canonical(config or {})), self.HASH_SEED)

    @staticmethod
    def _referenced(config: dict) -> set[int]:
        """The fns saved items name (probe.config §2): plan fns, slot wire_fns, fixture UARTs a bind carries."""
        fns = set()
        for (tag, key), value in config.items():
            if tag in (ITEM["plan"], ITEM["uart"]):
                fns.add(key)
            elif tag == ITEM["slot"]:
                fns.add(struct.unpack_from("<H", value, 1)[0])
            elif tag == ITEM["bind"] and value[1] == BIND_STREAM["fixture_uart"]:
                fns.add(struct.unpack_from("<H", value, 2)[0])
        return fns

    def _apply_saved(self) -> None:
        """At boot: the saved items' fns found again by (name, instance, revision) and renumbered, then applied; one
        not found (or of another revision) leaves the whole unapplied (probe.config §2)."""
        for (tag, key), value in self.saved.items():
            if tag == ITEM["bind"] and key not in self.serial_ports:
                self.saved_reason = 2                              # a bind's port not a serial port here (PC-8)
                return
        now = {ident: fn for fn, ident in self.identity.items()}
        remap = {}
        for fn, ident in self.saved_ids.items():
            if ident not in now:
                self.saved_reason = 2
                return
            remap[fn] = now[ident]
        pack = lambda fn: struct.pack("<H", remap.get(fn, fn))   # noqa: E731
        new = {}
        for (tag, key), value in self.saved.items():
            if tag == ITEM["plan"]:
                new[(tag, remap.get(key, key))] = [pack(key) + v[2:] for v in value]
            elif tag == ITEM["slot"]:
                new[(tag, key)] = value[:1] + pack(struct.unpack_from("<H", value, 1)[0]) + value[3:]
            elif tag == ITEM["bind"] and value[1] == BIND_STREAM["fixture_uart"]:
                new[(tag, key)] = value[:2] + pack(struct.unpack_from("<H", value, 2)[0])
            elif tag == ITEM["uart"]:
                new[(tag, remap.get(key, key))] = pack(key) + value[2:]
            else:
                new[(tag, key)] = value
        self.saved = new                                           # the saved items, read for this boot's fns
        try:
            self._apply_config(new, boot=True)
            self.saved_hash = self._hash(self.config)              # the saved settings are the current ones now
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
            self.saved_hash = self._hash(self.config)

    def _apply_config(self, new: dict, changed_slots: set[int] | None = None, boot: bool = False,
                      received: dict | None = None, undeclared: int | None = None) -> None:
        """Check the whole config, then make it the current one (set is all-or-nothing up to reserving resources);
        automatic attaches and console opens follow (and are not rolled back). `received`: (tag, key) -> the item's
        tag as the set sent it (the critical bit kept; a plan's key is its fn), for the unsupported payloads that name
        it. `undeclared`: the first item tag of the set this probe does not handle.

        Every check runs before anything changes (core §4.3: any one reason that applies): the form of each item and
        the contradictions between items (malformed), the fns the items name (unknown_function), what this probe does
        not handle (unsupported), then the state and the resources (unavailable). Items are visited in get's order."""
        received = received or {}
        items = sorted(new.items(), key=lambda kv: kv[0])
        # the form of each item, then the contradictions between items
        slots: dict[int, Slot] = {}
        for (tag, key), value in items:
            if tag == ITEM["slot"]:
                slots[key] = self._parse_slot(value, "form")
            elif tag == ITEM["plan"]:
                if any(len(v) != 5 for v in value) or key == m.CORE_FN:
                    raise Reject(m.MALFORMED)                      # fn 0 holds no plan (oep-if-plan §2.5)
            elif tag == ITEM["idle"]:
                if len(value) != ITEM_SIZES[tag]:
                    raise Reject(m.MALFORMED)                      # channel mode drive: 4 bytes (probe.config §1)
            elif tag == ITEM["label"]:
                # channel(u16) text: 1-32 bytes (probe.config §1: the length is the rule)
                if not 3 <= len(value) <= 2 + reg.LIMITS["label_max_bytes"]:
                    raise Reject(m.MALFORMED)
            elif tag == ITEM["uart"]:
                if len(value) != ITEM_SIZES[tag] or struct.unpack_from("<I", value, 2)[0] == 0:
                    raise Reject(m.MALFORMED)                      # baud 0 (fixture §2)
            elif tag == ITEM["bind"]:
                if len(value) != ITEM_SIZES[tag]:
                    raise Reject(m.MALFORMED)
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
        for (tag, key), value in items:
            if tag == ITEM["bind"]:
                self._parse_bind(value, slots, "form")
        # the fns the items name
        for (tag, key), value in items:
            if tag == ITEM["plan"] and key not in self.names:
                raise Reject(m.UNKNOWN_FUNCTION)
            if tag == ITEM["slot"]:
                self._parse_slot(value, "fn")
            elif tag == ITEM["bind"]:
                self._parse_bind(value, slots, "fn")
            elif tag == ITEM["uart"] and struct.unpack_from("<H", value)[0] not in self.names:
                raise Reject(m.UNKNOWN_FUNCTION)
        # what this probe does not handle
        if undeclared is not None:
            raise Reject(m.UNSUPPORTED, bytes([undeclared]))       # an item it does not declare, the tag as received
        binds: dict[int, Bind] = {}
        uarts: dict[int, tuple[int, int]] = {}
        for (tag, key), value in items:
            as_received = bytes([received.get((tag, key), tag)])  # the item's tag as received (probe.config §1)
            channel = m.tlv(_UNA["channel"], struct.pack("<H", key)) if tag in CHANNEL_ITEMS else b""
            if tag in CHANNEL_ITEMS and not self._channel_ok(key):
                # at or past `channels`, or one the probe uses itself (probe.config §1, the channel of an item)
                raise Reject(m.UNSUPPORTED, as_received + channel)
            if tag == ITEM["slot"]:
                slots[key] = self._item_refusal(received, tag, key, self._parse_slot, value, "values")
            elif tag == ITEM["bind"]:
                binds[key] = self._item_refusal(received, tag, key, self._parse_bind, value, slots, "values")
            elif tag == ITEM["idle"]:
                mode, drive = value[2], value[3]
                if mode not in IDLE_MODE.values():
                    raise Reject(m.UNSUPPORTED, as_received + channel)   # 5 or more: left unused by the definition
                if mode in (IDLE_MODE["output_low"], IDLE_MODE["output_high"]):
                    if key in self.input_only:
                        raise Reject(m.UNSUPPORTED, as_received + channel)   # cannot drive it as an output (§1)
                    if drive != DRIVE_DEFAULT and self._drive_level(drive) is None:
                        raise Reject(m.UNSUPPORTED, as_received + channel)   # no such level, or no drive_levels
                if mode in (IDLE_MODE["pull_up"], IDLE_MODE["pull_down"]) and mode in self.no_pull.get(key, ()):
                    raise Reject(m.UNSUPPORTED, as_received + channel)   # the channel lacks that pull (PC-3)
            elif tag == ITEM["uart"]:
                fn, baud, fmt = struct.unpack_from("<HIB", value)
                if self.names[fn] != "oep.fixture.uart":
                    raise Reject(m.UNSUPPORTED, as_received)
                uarts[fn] = (self._item_refusal(received, tag, key, self._uart_check, baud, fmt, fn, None, divide=False),
                             fmt)                                  # the range now, the divider at plan time
        plans = {key: [struct.unpack_from("<HBH", v) for v in value] for (tag, key), value in items
                 if tag == ITEM["plan"]}
        want = [a for fn in plans for a in plans[fn]]
        plan_tag = lambda fn: received.get((ITEM["plan"], fn), ITEM["plan"])   # noqa: E731
        self._check_plan(want, held=False, tag_of=plan_tag)        # roles and channels the fns do not declare
        # the state and the resources
        for fn in self.pairs:                                      # every wire (pin_roles wires have an empty list)
            if sum(1 for s in slots.values() if s.wire_fn == fn and s.attach == SLOT_ATTACH["at_boot"]) > \
                    self.max_connections.get(fn, 1):
                raise unavailable("limit")
        if len(want) + len([a for a in self.plan if a[0] not in plans and a[0] not in self.plan_from_config]) > \
                (self.plan_roles if self.plan_roles is not None else 1 << 30):
            raise unavailable("limit")
        old_plan_fns = {k[1] for k in self.config if k[0] == ITEM["plan"]}
        self.slots = slots                                         # the pin check below sees the new slots
        try:
            self._check_plan(want, tag_of=plan_tag)
            self._check_disabled(new, disabled, plans, want, slots)
        except Reject:
            self.slots = {k: self._parse_slot(v) for (t, k), v in self.config.items() if t == ITEM["slot"]}
            raise
        # accepted: make it current - disable and idle before any other item (probe.config §2)
        old_idle = {key: v for (tag, key), v in self.config.items() if tag == ITEM["idle"]}
        repark = (self.disabled - disabled) | {key for key in idles | set(old_idle) if new.get((ITEM["idle"], key)) != old_idle.get(key)}
        self.config = new
        # at boot every free channel, later the ones whose idle changed (or enabled again); a gpio plan then takes a
        # line in that state
        self._park(self._boot_channels() if boot else repark)
        for fn in old_plan_fns - set(plans):
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
                # a new bind: the port's position from here on (probe.config §1.2) - the stream's write position now,
                # or the start of the stream that comes later (sid None)
                sid, st = self._stream_for(b.stream)
                self.flows[(port, b.stream)] = Flow(sid, st.end) if st is not None else Flow(None, 0)
        self.binds = binds
        for n, s in slots.items():
            if s.attach == SLOT_ATTACH["at_boot"] and (boot or changed_slots is None or n in changed_slots):
                self.slot_rt[n].evicted = False
                self._auto_attach(n)
        self._refresh()

    @staticmethod
    def _item_refusal(received: dict, tag: int, key: int, check, *args, **kw):
        """check(*args) for one item; its unsupported names the item's tag as received (probe.config §1, core §4.3:
        a value inside a TLV names that TLV), the TLVs after it kept."""
        try:
            return check(*args, **kw)
        except Reject as r:
            if r.reason == m.UNSUPPORTED and r.payload[:1] == bytes([m.TAG_FIXED]):
                raise Reject(m.UNSUPPORTED, bytes([received.get((tag, key), tag)]) + r.payload[1:]) from None
            raise

    def _check_disabled(self, new: dict, disabled: set[int], plans: dict, want: list, slots: dict) -> None:
        """probe.config §1 disable: a channel in use now (a plan, a connection, a slot this set keeps) cannot be disabled
        (unavailable cause 1); an item naming a disabled channel (a plan, a slot's pins) is cause 5 with the channel."""
        for ch in sorted(disabled - self.disabled):
            for fn, _, c in sorted(self.plan):
                if c != ch:
                    continue
                if fn in self.plan_from_config:
                    if new.get((ITEM["plan"], fn)) == self.config.get((ITEM["plan"], fn)):
                        raise unavailable("pin_in_use", ch)
                elif fn not in plans:                              # a session's plan this set does not replace
                    raise unavailable("pin_in_use", ch)
            conn = next((c for c in self.conns.values() if ch in c.pair), None)
            if conn is not None:
                raise unavailable("pin_in_use", ch)
            for (tag, key), value in self.config.items():
                if tag == ITEM["slot"] and new.get((tag, key)) == value and ch in self._parse_slot(value).pair:
                    raise unavailable("pin_in_use", ch)
        self._refuse_disabled_in(disabled, [c for _, _, c in want] + [p for s in slots.values() for p in s.pair])

    @staticmethod
    def _refuse_disabled_in(disabled: set[int], channels: list[int]) -> None:
        for ch in channels:
            if ch != 0xFFFF and ch in disabled:
                raise unavailable("held_by_settings", ch)

    def _parse_slot(self, v: bytes, stage: str | None = None) -> Slot:
        """probe.config §1.1, its checks by kind: the form ("form": malformed), the fn it names ("fn":
        unknown_function), then what this probe lacks ("values": unsupported); `stage` None runs all three. retry_ms is
        not looked at on a host slot."""
        t = Take(v)
        n, wire_fn, swdio, swclk, attach, retry_ms, max_speed, idle_clock, mech, name_len = t.take("BHHHBIIBBB")
        name = t.bytes(name_len)                                   # the item ends with the name
        if stage in (None, "form"):
            if n >= self.slots_max:
                raise Reject(m.MALFORMED)
            if not SLOT_NAME.fullmatch(name.decode("ascii", "replace")):
                raise Reject(m.MALFORMED)
        if stage in (None, "fn") and wire_fn not in self.names:
            raise Reject(m.UNKNOWN_FUNCTION)
        if stage in (None, "values"):
            if attach not in SLOT_ATTACH.values() or idle_clock not in reg.WIRE_RVSWD.enum["idle_clock"].values():
                raise Reject(m.UNSUPPORTED)                        # values the definition leaves unused (core §2.5)
            if self.names[wire_fn] not in WIRES:
                raise Reject(m.UNSUPPORTED)                        # not a wire slots ride on (swd: debug §5)
            if not self._allows(wire_fn, (swdio, swclk)):
                raise Reject(m.UNSUPPORTED)                        # not a pair that wire offers
            if idle_clock and self.names[wire_fn] != "oep.wire.rvswd":
                raise Reject(m.UNSUPPORTED)                        # as attach's idle_clock (debug §3)
            if mech != MECHANISM_NONE and mech not in self.mechanisms:
                raise Reject(m.UNSUPPORTED)
        if attach != SLOT_ATTACH["at_boot"]:
            retry_ms = 0                                           # a host slot is never retried (not looked at)
        return Slot(n, wire_fn, (swdio, swclk), attach, retry_ms, max_speed, idle_clock, mech,
                    name.decode("ascii", "replace"))

    def _parse_bind(self, v: bytes, slots: dict[int, Slot], stage: str | None = None) -> Bind:
        """probe.config §1.2 - port(u8) kind(u8) id(u16) - its checks by kind as `_parse_slot` (stages "form", "fn",
        "values"; None: all)."""
        if len(v) != ITEM_SIZES[ITEM["bind"]]:
            raise Reject(m.MALFORMED)
        port, kind = v[0], v[1]
        i = struct.unpack_from("<H", v, 2)[0]
        if stage in (None, "form"):
            if kind == BIND_STREAM["slot_console"] and (i not in slots or slots[i].mechanism == MECHANISM_NONE):
                raise Reject(m.MALFORMED)                          # a slot that is not there, or has no console
        if stage in (None, "fn"):
            if kind == BIND_STREAM["fixture_uart"] and i not in self.names:
                raise Reject(m.UNKNOWN_FUNCTION)
        if stage in (None, "values"):
            if kind not in (BIND_STREAM["slot_console"], BIND_STREAM["fixture_uart"]):
                raise Reject(m.UNSUPPORTED)                        # a kind the definition leaves unused (core §2.5)
            if port not in self.serial_ports:
                raise Reject(m.UNSUPPORTED)                        # not a serial port (§1.2)
            if kind == BIND_STREAM["fixture_uart"] and self.names[i] != "oep.fixture.uart":
                raise Reject(m.UNSUPPORTED)
        return Bind(port, (kind, i))

    # ---- slots: automatic attach, what uses a connection ----------------------------------------
    def _bound(self, n: int) -> bool:
        return any(b.stream == (BIND_STREAM["slot_console"], n) for b in self.binds.values())

    def _auto_attach(self, n: int) -> None:
        """An at-boot slot's automatic attach (probe.config §3.1): method 0 on the slot's pins with its line settings,
        never evicting a connection; the probe checks no target (a host does, with connections' tid)."""
        s = self.slots[n]
        rt = self.slot_rt[n]
        rt.last_try_ms = self.now()
        if s.attach != SLOT_ATTACH["at_boot"]:
            return
        tg = self._target(s.wire_fn, s.pair)
        cid = self._conn_at(s.wire_fn, s.pair)
        if cid is None:
            if not tg.answers:                                     # completed failed, status line: absent
                return
            try:
                speed = min(4_000_000, s.max_speed) if s.max_speed else 4_000_000   # the slot's line settings
                cid = self._seat(s.wire_fn, s.pair, tg, speed, evict=False)   # automatic: never evicts
                self.conns[cid].idle_clock = s.idle_clock
            except Reject:
                return
            tg.havereset = False
        self.conns[cid].users.add(("slot", n))

    def line_for(self, slot_name: str | None, name: str) -> int | None:
        """The channel of a line by the label convention (probe.config §1.3): the settings' label items, then the
        firmware's fixed labels (step (c), PC-1)."""
        labels = [(key, value[2:].decode("utf-8", "replace")) for (tag, key), value in self.config.items()
                  if tag == ITEM["label"]]
        n_slots = sum(1 for tag, _ in self.config if tag == ITEM["slot"])
        return cfgmod.line_from_labels(labels, n_slots, slot_name, name, list(self.static_labels.items()))

    def _refresh(self) -> None:
        """Make what the slots use match the config: a bound slot rides any connection on its place with its console
        open; an at-boot slot keeps its automatic connection; nothing else."""
        for sid, st in list(self.streams.items()):                 # a slot's share of a console: while bound, same place
            for u in [u for u in st.users if u != "host"]:
                s = self.slots.get(u[1])
                c = self.conns.get(st.conn)
                if (s is None or c is None or (s.wire_fn, s.pair) != (c.fn, c.pair) or s.mechanism != st.mechanism
                        or not self._bound(u[1])):
                    self._drop_stream_user(sid, u, MARK_CLOSED["slot_changed"])
        for cid, c in list(self.conns.items()):
            for u in [u for u in c.users if u != "host"]:
                s = self.slots.get(u[1])
                if (s is None or (s.wire_fn, s.pair) != (c.fn, c.pair)
                        or not (s.attach == SLOT_ATTACH["at_boot"] or self._bound(u[1]))):
                    c.users.discard(u)
            if not c.users:
                self._close_conn(cid, MARK["detach"])
        for n, s in self.slots.items():
            cid = self._conn_at(s.wire_fn, s.pair)
            if cid is None or not self._bound(n):
                continue
            if s.mechanism == MECHANISM_NONE:
                continue                                           # no console on this slot
            self.conns[cid].users.add(("slot", n))
            try:
                self._open_stream(cid, s.mechanism, ("slot", n))
            except Reject:
                continue                                           # another mechanism's stream is live there

    def tick(self) -> None:
        """Time passes: the lease, at-boot retries, the captures, port_speed's timers."""
        self._lapse()
        self._speed_tick()
        self._console_poll()
        now = self.now()
        for fn, cap in self.captures.items():
            self._events(fn, cap.tick(self.uptime_ms()))
        for n, s in self.slots.items():
            rt = self.slot_rt[n]
            if (s.attach == SLOT_ATTACH["at_boot"] and s.retry_ms and not rt.evicted
                    and self._conn_at(s.wire_fn, s.pair) is None
                    and (rt.last_try_ms is None or now - rt.last_try_ms >= s.retry_ms)):
                self._auto_attach(n)
                self._refresh()

    def _slot_state(self, n: int) -> bytes:
        """slot(u8) state(u8: 0 connected, 1 absent) connection(u16, 0 none) last_try_at_ns(u64, all ones: never)
        (probe.config §3.3)."""
        rt = self.slot_rt[n]
        s = self.slots[n]
        cid = self._conn_at(s.wire_fn, s.pair)
        state = SLOT_STATE["connected"] if cid is not None else SLOT_STATE["absent"]
        tried = NEVER_NS if rt.last_try_ms is None else self._clock_ns_of(rt.last_try_ms)   # when (the probe's clock)
        return struct.pack("<BBHQ", n, state, cid or 0, tried)

    def _bind_state(self, port: int) -> bytes:
        """port(u8) flow(u8: 0 nothing to carry, 1 carrying, 2 held by a session) (probe.config §3.3)."""
        b = self.binds[port]
        if port in self.held_ports and self.holder is not None:
            flow = BIND_FLOW["held"]
        else:
            flow = BIND_FLOW["streaming"] if self._stream_for(b.stream)[1] is not None else BIND_FLOW["idle"]
        return struct.pack("<BB", port, flow)

    # ---- serial ports: the raw bytes outside the frames (transports §4, probe.config §1.2) ----------
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
        """The port's position in its bind's stream: from now when first seen; after an overflow, the oldest byte left
        (probe.config §1.2)."""
        sid, s = self._stream_for(key)
        f = self.flows.get((port, key))
        if s is None:
            return f or Flow(), None
        if f is None:
            f = self.flows[(port, key)] = Flow(sid, s.end)         # no position yet: from now
        elif f.sid != sid:
            f = self.flows[(port, key)] = Flow(sid, s.base)        # a stream that came after the bind: from its start
        if f.pos < s.base:
            f.pos = s.base                                         # overflowed past the port: the oldest left
        return f, s

    def port_held(self, port: int) -> bool:
        return self.holder is not None and port in self.held_ports

    def port_output(self, port: int, room: int | None = None) -> bytes:
        """The raw bytes serial port `port` sends now by its bind (none while a session holds it: the position stays)."""
        room = self.CHUNK if room is None else room
        b = self.binds.get(port)
        if b is None or self.port_held(port) or room <= 0:
            return b""
        f, s = self._flow(port, b.stream)
        if s is None:
            return b""
        out = bytes(s.data[f.pos - s.base:f.pos - s.base + room])
        f.pos += len(out)
        return out

    def port_input(self, port: int, data: bytes) -> None:
        """Raw bytes that came in on serial port `port` outside any frame: to the bind's stream's other end (a console
        write, a fixture UART's TX)."""
        b = self.binds.get(port)
        if b is None or self.port_held(port) or not data:
            return
        sid, s = self._stream_for(b.stream)
        if s is None:
            return
        if b.stream[0] == BIND_STREAM["fixture_uart"]:
            self.uart_tx.setdefault(b.stream[1], bytearray()).extend(data)
        else:
            s.written += data

    def _session_over(self) -> None:
        """The session ended (end, lapse, force): the ports it held carry on from where they stopped (probe.config
        §1.2: the oldest byte left after an overflow); a port off its boot speed goes back after the answer
        (oep-if-link §3)."""
        if self.speed_state != "base":
            self.speed_pending = ("revert",)
        self.held_ports.clear()


SIMS = {"oep.probe.link": "link", "oep.probe.plan": "plan_op", "oep.probe.restart": "restart", "oep.wire.rvswd": "wire", "oep.wire.swio": "wire", "oep.target.riscv-dm": "dm",
        "oep.target.console": "console", "oep.fixture.gpio": "gpio", "oep.fixture.uart": "uart",
        "oep.probe.config": "config_op", "oep.fixture.logic": "capture", "oep.fixture.analog": "capture",
        "oep.fixture.capture-group": "group", "oep.fixture.i2c-target": "i2c_op", "oep.fixture.spi-target": "spi_op"}

# A riscv-dm op whose exchanges got no answer from the wire: completed failed, status line, nothing done (debug §4)
DM_LINE_FAILED = {_RV.op["dmi"]: struct.pack("<HBH", 0, LINE, 0), _RV.op["halt"]: bytes([LINE]),
                  _RV.op["resume"]: bytes([LINE]), _RV.op["reset"]: struct.pack("<BBI", LINE, 0, 0),
                  _RV.op["step"]: struct.pack("<BBII", LINE, 0, 0, 0), _RV.op["read_block"]: struct.pack("<HB", 0, LINE),
                  _RV.op["write_block"]: struct.pack("<HB", 0, LINE),
                  _RV.op["run"]: struct.pack("<BBIIB", LINE, RUN_STOPPED["not_run"], 0, 0, 0)}
