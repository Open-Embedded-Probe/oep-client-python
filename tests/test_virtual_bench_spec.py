"""The virtual bench as the working spec: resend table, owner, lease, describe, the serial port's frames, the wires and
riscv-dm, and virtual_bench_serve."""

import fcntl
import os
import struct
import subprocess
import sys
import termios
import time

import pytest

from oep_client import cobs, endpoint, virtual_bench, message as m, registry as reg

CFG = reg.PROBE_CONFIG
W, RV = reg.WIRE_RVSWD.op, reg.TARGET_RISCV_DM.op


def att(pair, method=0, speed=4_000_000):
    """attach's fixed part: method swdio swclk max_speed (debug §1, §3)."""
    return struct.pack("<BHHI", method, *pair, speed)
PLAN_APPLY, PLAN_RELEASE = reg.PROBE_PLAN.op["plan_apply"], reg.PROBE_PLAN.op["plan_release"]   # oep.probe.plan


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


class Host:
    """Whole messages to an endpoint, with corr counting as a host does."""

    def __init__(self, ep, session=0x51, transport=0):
        self.ep, self.session, self.transport, self.corr = ep, session, transport, 0

    def raw(self, fn, op, payload=b"", session=True, corr=None):
        if corr is None:
            self.corr += 1
            corr = self.corr
        req = m.Request(corr, fn, op, payload, self.session if session else None)
        return m.Result.unpack(self.ep.handle(req.pack(), self.transport))

    def open(self, lease=3000, force=0, owner=None):
        """open: lease_ms(u32) force(u8) [TLV owner], the session_id in the header (core §4.1, §6.4)."""
        tail = m.tlv(reg.CORE.tlv["open"]["owner"], owner) if owner else b""
        return self.raw(0, m.OP_OPEN, struct.pack("<IB", lease, force) + tail)

    @property
    def plan_fn(self):
        """The endpoint's oep.probe.plan fn (oep-if-plan)."""
        return self.ep.fns["oep.probe.plan"]

    def ok(self, fn, op, payload=b""):
        r = self.raw(fn, op, payload)
        assert r.succeeded, r.describe()
        return r.payload


def describe(ep, fn):
    """Every describe TLV of `fn`, paged (core §7.3)."""
    h, out = Host(ep), []
    while True:
        r = h.raw(0, m.OP_DESCRIBE, struct.pack("<HH", fn, len(out)), session=False)
        page = m.split_tlvs(r.payload[1:])
        out += page
        if not r.payload[0] or not page:
            return out


# ---- the resend table (core §5.2) --------------------------------------------------------------------

def test_a_resent_request_gets_its_remembered_result_and_runs_once():
    ep = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), Clock())
    h = Host(ep)
    h.open()
    toy = next(fn for fn, name in ep.names.items() if name.endswith(".stand-in"))   # TOY_WRITE: write(u32)
    first = h.raw(toy, endpoint.TOY_WRITE, struct.pack("<I", 5))
    ep.values[toy] = 99                                             # the state moved on
    again = h.raw(toy, endpoint.TOY_WRITE, struct.pack("<I", 5), corr=h.corr)
    assert again == first and ep.values[toy] == 99                  # not executed a second time
    # core §5.1: the record is the whole request; the same corr with another payload is malformed, never run
    assert h.raw(toy, endpoint.TOY_WRITE, struct.pack("<I", 6), corr=h.corr).detail == m.MALFORMED and ep.values[toy] == 99
    h.raw(toy, endpoint.TOY_WRITE, struct.pack("<I", 1))
    h.raw(toy, endpoint.TOY_WRITE, struct.pack("<I", 2))
    for _ in range(10):
        h.raw(toy, endpoint.TOY_WRITE, struct.pack("<I", 3))
    assert h.raw(toy, endpoint.TOY_WRITE, struct.pack("<I", 1), corr=2).detail == m.RESULT_LOST   # fell out of the table


def test_a_long_result_is_not_remembered():
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    h = Host(ep)
    h.open()
    r = h.raw(1, W["attach"], att(ep.pairs[1][0]))
    conn = struct.unpack_from("<I", r.payload)[0]
    ep.target.halted = True
    big = h.raw(2, RV["read_block"], struct.pack("<IIH", conn, 0x20000000, 32))   # 32 words, over 72 bytes
    assert big.succeeded
    assert h.raw(2, RV["read_block"], struct.pack("<IIH", conn, 0x20000000, 32), corr=h.corr).detail == m.RESULT_LOST


def test_open_empties_the_table():
    ep = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), Clock())
    h = Host(ep)
    h.open()
    h.raw(13, endpoint.TOY_WRITE, struct.pack("<I", 5))                           # the stand-in fn
    h.open()                                                        # a new open: the table goes
    assert h.raw(13, endpoint.TOY_WRITE, struct.pack("<I", 5), corr=h.corr - 1).succeeded   # an old corr is new again


# ---- the lock: owner and lease (core §6.4) -----------------------------------------------------------

def test_owner_is_named_by_lock_state_and_by_locked_but_never_the_session():
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)
    a, b = Host(ep, 0xA), Host(ep, 0xB)
    a.open(owner=b"ch32rv monitor pid 1234")
    r = b.raw(0, m.OP_LOCK_STATE, session=False)
    assert r.payload[0] == 1 and m.Tail.parse(r.payload[5:]).get(0x01) == b"ch32rv monitor pid 1234"
    refused = b.open()
    assert refused.detail == m.LOCKED and refused.payload[4:] == m.tlv(0x01, b"ch32rv monitor pid 1234")
    a.ok(0, m.OP_END)
    assert b.raw(0, m.OP_LOCK_STATE, session=False).payload == struct.pack("<BI", 0, 0)
    c = Host(ep, 0xC)                                               # a new id: the ended 0xA never opens again (§6.2)
    assert c.open().succeeded                                       # a new lock without an owner: none (core §6.4)
    assert m.Tail.parse(b.raw(0, m.OP_LOCK_STATE, session=False).payload[5:]).get(0x01) is None
    assert c.open(owner=b"later").succeeded                         # the holder's own open again: the lock's owner stays
    assert m.Tail.parse(b.raw(0, m.OP_LOCK_STATE, session=False).payload[5:]).get(0x01) is None
    c.ok(0, m.OP_END)
    assert b.open(owner=b"next").succeeded                          # the owner comes from the open that takes the lock
    assert m.Tail.parse(a.raw(0, m.OP_LOCK_STATE, session=False).payload[5:]).get(0x01) == b"next"


@pytest.mark.parametrize("asked,given", [(0, 3000), (1000, 1000), (60000, 60000), (700000, 60000), (1, 1000)])
def test_lease(asked, given):
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    assert struct.unpack_from("<I", Host(ep).open(lease=asked).payload)[0] == given


# ---- describe ------------------------------------------------------------------------------------------

def test_core_describe_lists_the_transports_and_max_op_ms():
    tlvs = describe(endpoint.Endpoint(virtual_bench.p4_x035(), Clock()), 0)
    tags = reg.CORE.tlv["describe"]
    kinds = [v[1] for t, v in tlvs if t == tags["transport"]]
    assert kinds == [3, 4, 5, 2]
    assert (tags["max_op_ms"], struct.pack("<I", virtual_bench.MAX_OP_MS)) in tlvs
    assert (virtual_bench.PLAN_ROLES_TAG, struct.pack("<I", 32)) in describe(endpoint.Endpoint(virtual_bench.p4_x035(), Clock()), 14)
    assert any(t == tags["unit_id"] for t, _ in tlvs) and 0x48 not in [t for t, _ in tlvs]
    # declarations only (core §7.3): a label the settings give is not in the describe
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    holder = Host(ep)
    holder.open()
    holder.ok(10, CFG.op["set"], b"\x01" + m.tlv(CFG.tlv["item"]["label"], struct.pack("<H", 20) + b"DUT"))
    assert all(v[2:] != b"DUT" for t, v in describe(ep, 0) if t == tags["label"])


@pytest.mark.parametrize("profile", sorted(virtual_bench.PROFILES))
def test_core_describe_has_no_removed_tags(profile):
    """core §7.5 (rule review 2026-10-07): reserved (0x44), profile (0x45), resets_on_open (0x47) and discoverable
    (0x4A) are gone - the registry names none of them and no virtual bench profile describes them (implementation went
    from the common tags, whose 0x07 is now ops)."""
    gone = {0x44, 0x45, 0x47, 0x4A}
    tags = reg.CORE.tlv["describe"]
    assert not {"implementation", "reserved", "profile", "resets_on_open", "discoverable"} & set(tags)
    assert not gone & set(tags.values())
    assert not gone & {t & 0x7F for t, _ in describe(endpoint.Endpoint(virtual_bench.PROFILES[profile](), Clock()), 0)}


def _with_transports(probe, entries):
    """`probe` with its fn 0 transport TLVs replaced by `entries` [(index, kind, interface)], in that TLV order."""
    core0 = next(o for o in probe.offered if o.fn == 0)
    tlvs = tuple(t for t in core0.tlvs if t[0] != virtual_bench.CORE_TRANSPORT) + tuple(
        virtual_bench.catalog.tlv(virtual_bench.CORE_TRANSPORT, bytes(e)) for e in entries)
    offered = [virtual_bench.Offered(0, core0.instance, core0.name, tlvs, core0.revision) if o.fn == 0 else o
               for o in probe.offered]
    return virtual_bench.VirtualProbe(probe.label, probe.max_frame, offered)


@pytest.mark.parametrize("entries, bridge, other", [
    ([(1, virtual_bench.TRANSPORT["vendor_bulk"], 0), (0, virtual_bench.TRANSPORT["uart_bridge"], 0xFF)], 0, 1),   # out of TLV order
    ([(7, virtual_bench.TRANSPORT["vendor_bulk"], 0), (3, virtual_bench.TRANSPORT["uart_bridge"], 0xFF)], 3, 7),   # not from 0, gaps
])
def test_transport_list_follows_the_index_byte_not_the_tlv_order(entries, bridge, other):
    """core §7.5: index is the number designating a transport, whatever order the TLVs come in.
    port_speed is accepted on the UART bridge it came on and refused on the vendor bulk (unavailable, oep-if-link §3)."""
    for port, ok in ((bridge, True), (other, False)):
        ep = endpoint.Endpoint(_with_transports(virtual_bench.esp32_v003(), entries), Clock())
        assert ep.transports == {i: k for i, k, _ in entries} and ep.serial_ports == {bridge}
        h = Host(ep, transport=port)
        assert h.open().succeeded
        r = h.raw(ep.link_fn, endpoint.OP_PORT_SPEED, struct.pack("<IBH", 230400, 0, 500))   # try (oep-if-link §3)
        assert r.succeeded if ok else r.detail == m.UNAVAILABLE


def bench(clock=None):
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), clock or Clock())
    h = Host(ep)
    h.open()
    return ep, h


# ---- virtual_bench_serve on a pty -----------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux", reason="pty and TIOCEXCL as on Linux")
def test_virtual_bench_serve_pty_speaks_cobs_and_honours_tiocexcl():
    """The pty is the probe's serial port: OEP frames only (transports §4), opened exclusively."""
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--pty", "--profile", "p4-bench"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        where = proc.stdout.readline().split()
        assert where[0] == "PTY"
        fd = os.open(where[1], os.O_RDWR | os.O_NOCTTY)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        if os.geteuid() != 0:
            with pytest.raises(OSError):
                os.open(where[1], os.O_RDWR | os.O_NOCTTY)          # the second open: EBUSY
        os.write(fd, b"noise" + cobs.frame(m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([1, 1])).pack()))
        got, deadline = b"", time.monotonic() + 3
        while time.monotonic() < deadline and got.count(b"\x00") < 2:
            got += os.read(fd, 4096)
        frames = [f for f in got.split(b"\x00") if f]
        assert [m.Result.unpack(cobs.unframe(f)).corr for f in frames] == [1]   # the answer and nothing else
        os.close(fd)
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_virtual_bench_serve_run_hook_from_a_file(tmp_path):
    from oep_client import virtual_bench_serve
    hook = tmp_path / "loader.py"
    hook.write_text("def run(target, pc, regs):\n    target.mem[0x100] = regs.get(0x100A, 0)\n    return True, pc + 4, 7\n")
    a = virtual_bench_serve.main.__globals__["argparse"].Namespace(
        profile="p4-x035", target_id=None, absent=[], port_index=0, run_hook=f"{hook}:run")
    ep = virtual_bench_serve.build(a)
    assert ep.target.run_hook(0x2000, {0x100A: 5}) == (True, 0x2004, 7) and ep.target.mem[0x100] == 5


def test_virtual_bench_serve_uart_rx():
    """--uart-rx: what arrives on the first UART's RX once a session planned and configured it (fixture §2)."""
    import time
    from oep_client import core, fixture, link
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length",
                             "--profile", "esp32-v003", "--uart-rx", "rx %d\\n", "--every", "10"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        port = proc.stdout.readline().split()[1]
        hst = link.open_host(f"tcp://127.0.0.1:{port}", timeout=1.0)
        hst.open(3000)
        uart = fixture.FixtureUartIO(hst)
        core.plan_apply(hst, [(uart.uart.fn, 1, 22), (uart.uart.fn, 2, 21)])   # the jig's DUT TX / DUT RX
        assert uart.configure(115200) > 0
        got, deadline = b"", time.monotonic() + 2
        while b"\n" not in got and time.monotonic() < deadline:
            got += uart.read()
        assert got.startswith(b"rx ")                                # the RX side, from the configure on
        uart.write(b"to the DUT")                                    # out on TX
        hst.end()
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_read_from_a_last_mark_that_is_not_there_is_from_now():
    ep, h = bench()
    p = ep.pairs[1][0]
    r = h.raw(1, W["attach"], att(p))
    uart_fn = 5
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", uart_fn, 1, 20)) + m.tlv(0x90, struct.pack("<HBH", uart_fn, 2, 21)))
    h.ok(uart_fn, reg.FIXTURE_UART.op["configure"], struct.pack("<I", 115200))
    ep.uart_rx(uart_fn, b"old output")
    lost = reg.COMMON.enum["mark_kind"]["lost"] if "lost" in reg.COMMON.enum["mark_kind"] else 1
    rd = m.Reader(h.raw(uart_fn, reg.FIXTURE_UART.op["read"], struct.pack("<BQH", 3, 1, 64), session=False).payload)
    assert rd.take("QB") == (10, 0) and rd.counted("H") == b""      # no lost mark (kind 1): from now, not the old bytes


def test_no_default_reset_line_only_the_declared_channels():
    """oep-if-debug §3: the attach's reset TLV names its channel; the probe takes only role 3 (reset) channels
    (unsupported, tag 0x85 otherwise), and not one a plan holds (unavailable)."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    h = Host(ep)
    h.open()
    swio = ep.pairs[1][0]
    assert ep.reset_channels[1] == {23}
    reset = lambda ch, hold=20: m.tlv(0x05, struct.pack("<HH", ch, hold), critical=True)
    for channel in (0xFFFF, 22):                                    # no default; 22 is not a reset line
        r = h.raw(1, W["attach"], att(swio, 1) + reset(channel))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([0x85]))
    rd = m.Reader(h.ok(1, W["attach"], att(swio, 1) + reset(23)))
    conn, _, flags, _ = rd.take("IIBI")
    assert flags & 0x08 and rd.tail().get(0x11) == struct.pack("<I", 0)   # halted at the reset vector: TLV dpc
    h.ok(1, W["detach"], struct.pack("<I", conn))
    assert h.raw(1, W["attach"], att(swio, 1) + reset(23, 20000)).detail == m.UNSUPPORTED   # hold_ms over max_op_ms
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 4, 1, 23)))   # fixture.gpio takes NRST
    assert h.raw(1, W["attach"], att(swio, 1) + reset(23)).detail == m.UNAVAILABLE
    assert h.raw(1, W["attach"], att(swio, 1, speed=0)).detail == m.MALFORMED   # max_speed 0


def test_idle_clock_is_rvswd_s():
    ep, h = bench()
    pair = ep.pairs[1][0]
    idle_low = m.tlv(0x04, b"\x01", critical=True)
    h.ok(1, W["attach"], att(pair, 1) + idle_low)
    assert ep.conns[ep._conn_at(1, pair)].idle_clock == 1
    assert h.raw(1, W["attach"], att(ep.pairs[1][1], 1)
                 + m.tlv(0x04, b"\x02", critical=True)).payload == b"\x84"   # a value a later revision may define
    scan = b"\x01" + struct.pack("<HH", *ep.pairs[1][2])
    assert h.raw(1, W["scan"], scan + m.tlv(0x04, b"\x01", critical=True)
                 + m.tlv(0x01, struct.pack("<I", 4_000_000), critical=True)).succeeded   # scan takes them too
    v003 = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hv = Host(v003)
    hv.open()
    r = hv.raw(1, W["attach"], att(v003.pairs[1][0], 1) + idle_low)
    assert r.resolution == m.REJECTED and r.detail == m.UNSUPPORTED  # an unknown critical TLV on swio


def pins_probe():
    ep = endpoint.Endpoint(virtual_bench.rp2350_pins(), Clock())
    h = Host(ep)
    h.open()
    return ep, h


def test_a_wire_takes_any_free_pair_the_host_names():
    """oep-if-debug §1: role_channels pairs, named by the host; a live connection holds its pins (never joined)."""
    ep, h = pins_probe()
    r = m.Reader(h.ok(1, W["scan"], b"\x02" + struct.pack("<HHHH", 0, 1, 1, 0)))
    tried, count = r.take("BB")
    assert tried == 2 and count == 1 and r.take("BHHI")[1:3] == (0, 1)
    conn = struct.unpack_from("<I", h.ok(1, W["attach"], att((0, 1), 1)))[0]
    assert h.raw(1, W["attach"], att((1, 2), 1)).detail == m.UNAVAILABLE    # GP1 is the live connection's
    assert h.raw(1, W["attach"], att((0, 1), 1)).detail == m.UNAVAILABLE    # its own pair too: no join (§1)
    assert h.raw(h.plan_fn, PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 4, 1, 0))).detail == m.UNAVAILABLE
    assert h.raw(1, W["scan"], b"\x01" + struct.pack("<HH", 0, 1)).detail == m.UNAVAILABLE   # not searched (§1)
    assert h.raw(1, W["attach"], att((2, 3), 1)).detail == m.UNAVAILABLE    # its one seat is taken: cause 2
    h.ok(1, W["detach"], struct.pack("<I", conn))                  # detach: the pins go back
    h.ok(h.plan_fn, PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 4, 1, 5)))
    assert h.raw(1, W["scan"], b"\x01" + struct.pack("<HH", 5, 6)).detail == m.UNAVAILABLE   # GP5 is the plan's
    assert h.raw(1, W["attach"], att((3, 3), 1)).detail == m.UNSUPPORTED         # one channel twice: no such pair
    assert h.raw(1, W["attach"], att((19, 3), 1)).detail == m.UNSUPPORTED        # not offered (PSRAM CS)
    r = h.raw(1, W["attach"], att((7, 8), 1))                       # a free pair nothing answers on
    assert r.resolution == m.COMPLETED and not r.succeeded and r.payload == bytes([2])   # failed: status line [TLV]


def test_the_client_scan_walks_every_candidate_pair():
    """debug §1: the host builds the candidate list (role 1 x role 2, no channel twice) and sends what is left after
    each answer's tried."""
    from oep_client import host as hh, riscv as target
    ep = endpoint.Endpoint(virtual_bench.rp2350_pins(), Clock())
    hst = hh.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    ep._target(1, (29, 28)).present = True                        # the last pair of the list
    found = target.Wire(hst).scan(max_speed=1_000_000)
    assert [f.pins for f in found] == [(0, 1), (29, 28)]
    scans = [r for r in ep.requests if (r.fn, r.op) == (1, W["scan"])]
    assert len(scans) == 4 and sum(r.payload[0] for r in scans) == 29 * 28   # 255 at most a request
    assert ep.requests[-1].payload.endswith(m.tlv(0x01, struct.pack("<I", 1_000_000), critical=True))


def test_dmi_and_run_answers_count_their_values_and_rejects_follow_the_order():
    """oep-if-debug §4: dmi answers done status nvals values; run answers ... nvals values; waits over max_op_ms are
    unsupported, a running hart's read_block is status state, an odd address malformed."""
    ep, h = bench()
    conn = struct.unpack_from("<I", h.ok(1, W["attach"], att(ep.pairs[1][0])))[0]
    ep.targets[(1, ep.pairs[1][0])].dmi[0x11] = 0x382
    r = h.ok(2, RV["dmi"], struct.pack("<IH", conn, 2) + bytes([2, 0x11]) + bytes([2, 0x11]))
    assert r == struct.pack("<HBH", 2, 0, 2) + struct.pack("<II", 0x382, 0x382)
    assert h.raw(2, RV["dmi"], struct.pack("<IH", conn, 1) + struct.pack("<BI", 4, 20_000_000)).detail == m.UNSUPPORTED
    r = h.raw(2, RV["read_block"], struct.pack("<IIH", conn, 0x20000000, 2))   # read_block on a running hart
    assert r.detail == m.FAILED and r.payload == struct.pack("<HB", 0, 5)
    assert h.raw(2, RV["read_block"], struct.pack("<IIH", conn, 0x20000001, 2)).detail == m.MALFORMED


@pytest.mark.parametrize("profile", sorted(virtual_bench.PROFILES))
def test_max_length_fits_a_read_answer_and_a_write_request_in_max_frame(profile):
    """oep-if-debug §4.5: a probe with block ops declares max_length (bytes, a multiple of 4) so that read_block's answer
    (5 + 2 + 1 + words) and write_block's request (10 + 2 + 4 + 2 + words) both fit max_frame: max_frame - 24 at most."""
    probe = virtual_bench.PROFILES[profile]()
    ep = endpoint.Endpoint(probe, Clock())
    fns = [fn for fn, name in ep.names.items() if name == "oep.target.riscv-dm"]
    assert fns
    for fn in fns:
        declared = ep.block_max[fn]
        assert declared == virtual_bench.block_max_length(probe.max_frame) == (probe.max_frame - 24) // 4 * 4
        assert declared % 4 == 0 and 8 + declared <= probe.max_frame and 18 + declared <= probe.max_frame
    assert virtual_bench.block_max_length(64) == 40 and virtual_bench.block_max_length(512) == 488 and virtual_bench.block_max_length(1024) == 1000


def test_a_block_past_max_length_is_unsupported_not_malformed():
    """oep-if-debug §4.5 / §6: count x 4 over the declared max_length -> rejected unsupported with payload 0x00 (a
    fixed-part value); an odd address stays malformed (core §4.3 order 5 before 6)."""
    ep, h = bench()
    conn = struct.unpack_from("<I", h.ok(1, W["attach"], att(ep.pairs[1][0])))[0]
    ep.targets[(1, ep.pairs[1][0])].halted = True
    words = ep.block_max[2] // 4
    rb, wb = RV["read_block"], RV["write_block"]
    assert h.raw(2, rb, struct.pack("<IIH", conn, 0x20000000, words)).succeeded
    r = h.raw(2, rb, struct.pack("<IIH", conn, 0x20000000, words + 1))
    assert r.detail == m.UNSUPPORTED and r.payload == b"\x00"
    r = h.raw(2, wb, struct.pack("<IIH", conn, 0x20000000, words + 1) + bytes(4 * (words + 1)))
    assert r.detail == m.UNSUPPORTED and r.payload == b"\x00"
    assert h.raw(2, wb, struct.pack("<IIH", conn, 0x20000002, words + 1) + bytes(4 * (words + 1))).detail == m.MALFORMED
    assert h.raw(2, wb, struct.pack("<IIH", conn, 0x20000000, words) + bytes(4 * words)).succeeded
