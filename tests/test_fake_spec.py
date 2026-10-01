"""The fake probe as the working spec (2026-09-29 revision): resend table, owner, lease, describe, slots and binds,
the seat rule, and a serial port shared by frames and raw bytes."""

import fcntl
import os
import struct
import subprocess
import sys
import termios
import time

import pytest

from oep_client import cobs, endpoint, fake, fake_serial, message as m, registry as reg

CFG = reg.PROBE_CONFIG
ITEM, ATTACH, MODE, KIND = CFG.tlv["item"], CFG.enum["slot_attach"], CFG.enum["bind_mode"], CFG.enum["bind_stream"]
STATE = CFG.enum["slot_state"]
SPEED = m.tlv(0x01, struct.pack("<I", 4_000_000), critical=True)    # attach's required max_speed TLV
MARK_KIND = reg.COMMON.enum["mark_kind"]


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
        tail = m.tlv(reg.CORE.tlv["open"]["owner"], owner) if owner else b""
        return self.raw(0, m.OP_OPEN, struct.pack("<IIB", self.session, lease, force) + tail, session=False)

    def ok(self, fn, op, payload=b""):
        r = self.raw(fn, op, payload)
        assert r.succeeded, r.describe()
        return r.payload


def slot_item(n, wire, pair, attach=ATTACH["at_boot"], retry=1, mech=2, name=None, lock=None, max_speed=0, idle=0):
    """A slot item (probe.config §1.1): ... retry_ms(u32) max_speed_hz(u32) ...; `retry` in seconds here."""
    raw = (name or f"s{n}").encode()
    value = struct.pack("<BHHHBIIBBB", n, wire, *pair, attach, 1000 * retry if attach == ATTACH["at_boot"] else 0,
                        max_speed, idle, mech, len(raw)) + raw
    value += b"\x00" if lock is None else bytes([1 + 2 * len(lock[0]), 1]) + lock[0] + lock[1]   # lock_len, scheme 1
    return m.tlv(ITEM["slot"], value)


def bind_item(port, mode, streams, selected=0):
    return m.tlv(ITEM["bind"], struct.pack("<BBBB", port, mode, selected, len(streams))
                 + b"".join(struct.pack("<BBH", 3, k, i) for k, i in streams))


def describe(ep, fn):
    h = Host(ep)
    r = h.raw(0, m.OP_DESCRIBE, struct.pack("<HH", fn, 0), session=False)
    return m.split_tlvs(r.payload[1:])


def state(ep, fn=6, first_slot=0, first_bind=0):
    """probe.config's state op (lock-free): -> (more, storage_state, storage_hash, reason, {slot: raw slot_state},
    [raw bind_state])."""
    rd = m.Reader(Host(ep).raw(fn, CFG.op["state"], bytes([first_slot, first_bind]), session=False).payload)
    more, storage, h, why = rd.take("BBIB")
    slots = {}
    for _ in range(rd.u8()):
        e = rd.element()
        slots[e.data[0]] = e.data
    binds = [rd.element().data for _ in range(rd.u8())]
    rd.tail()
    return more, storage, h, why, slots, binds


# ---- the resend table (core §5.2) --------------------------------------------------------------------

def test_a_resent_request_gets_its_remembered_result_and_runs_once():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    h = Host(ep)
    h.open()
    toy = 7                                                         # a stand-in fn: 0x01 write(u32)
    first = h.raw(toy, 0x01, struct.pack("<I", 5))
    ep.values[toy] = 99                                             # the state moved on
    again = h.raw(toy, 0x01, struct.pack("<I", 5), corr=h.corr)
    assert again == first and ep.values[toy] == 99                  # not executed a second time
    assert h.raw(toy, 0x01, struct.pack("<I", 6), corr=h.corr).detail == m.CORR_REUSED
    h.raw(toy, 0x01, struct.pack("<I", 1))
    h.raw(toy, 0x01, struct.pack("<I", 2))
    for _ in range(10):
        h.raw(toy, 0x01, struct.pack("<I", 3))
    assert h.raw(toy, 0x01, struct.pack("<I", 1), corr=2).detail == m.RESULT_LOST   # fell out of the table


def test_a_long_result_is_not_remembered():
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    h = Host(ep)
    h.open()
    wire = 1
    r = h.raw(wire, 0x02, bytes([0]) + SPEED)                        # attach
    conn = struct.unpack_from("<H", r.payload)[0]
    ep.target.halted = True
    big = h.raw(2, 0x05, struct.pack("<HIH", conn, 0x20000000, 32))  # read_block: 32 words, over 72 bytes
    assert big.succeeded
    assert h.raw(2, 0x05, struct.pack("<HIH", conn, 0x20000000, 32), corr=h.corr).detail == m.RESULT_LOST


def test_open_empties_the_table():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    h = Host(ep)
    h.open()
    h.raw(7, 0x01, struct.pack("<I", 5))
    h.open()                                                        # resume: the table goes
    assert h.raw(7, 0x01, struct.pack("<I", 5), corr=h.corr - 1).succeeded   # an old corr is new again


# ---- the lock: owner and lease (core §6.4) -----------------------------------------------------------

def test_owner_is_named_by_lock_state_and_by_locked_but_never_the_session():
    clock = Clock()
    ep = endpoint.Endpoint(fake.esp32_v003(), clock)
    a, b = Host(ep, 0xA), Host(ep, 0xB)
    a.open(owner=b"ch32rv monitor pid 1234")
    r = b.raw(0, m.OP_LOCK_STATE, session=False)
    assert r.payload[0] == 1 and m.Tail.parse(r.payload[5:]).get(0x01) == b"ch32rv monitor pid 1234"
    refused = b.open()
    assert refused.detail == m.LOCKED and refused.payload[4:] == m.tlv(0x01, b"ch32rv monitor pid 1234")
    a.ok(0, m.OP_END)
    assert b.raw(0, m.OP_LOCK_STATE, session=False).payload == struct.pack("<BI", 0, 0)
    assert a.open().succeeded                                       # resumed without an owner: the old one stays
    assert m.Tail.parse(b.raw(0, m.OP_LOCK_STATE, session=False).payload[5:]).get(0x01) == b"ch32rv monitor pid 1234"


@pytest.mark.parametrize("asked,given", [(0, 3000), (1000, 1000), (60000, 60000), (700000, 600000)])
def test_lease(asked, given):
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    assert struct.unpack_from("<I", Host(ep).open(lease=asked).payload)[0] == given


# ---- describe ------------------------------------------------------------------------------------------

def test_core_describe_lists_the_transports_discoverable_and_max_op_ms():
    tlvs = describe(endpoint.Endpoint(fake.p4_x035(), Clock()), 0)
    tags = reg.CORE.tlv["describe"]
    kinds = [v[1] for t, v in tlvs if t == tags["transport"]]
    assert kinds == [3, 4, 5, 2] and (tags["discoverable"], b"\x01") in tlvs
    assert (tags["max_op_ms"], struct.pack("<I", 10000)) in tlvs and (tags["plan_roles"], struct.pack("<I", 32)) in tlvs
    assert any(t == tags["unit_id"] for t, _ in tlvs) and 0x48 not in [t for t, _ in tlvs]
    # declarations only (core §7.3): a label the settings give is not in the describe
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    Host(ep).open()
    Host(ep).ok(10, 0x02, m.tlv(ITEM["label"], struct.pack("<H", 20) + b"DUT"))
    assert all(v[2:] != b"DUT" for t, v in describe(ep, 0) if t == tags["label"])


# ---- slots, connections and the seat rule --------------------------------------------------------------

def bench(clock=None):
    ep = endpoint.Endpoint(fake.p4_bench(), clock or Clock())
    h = Host(ep)
    h.open()
    return ep, h


def test_at_boot_slots_attach_and_say_so_without_the_lock():
    ep, h = bench()
    pairs = ep.pairs[1]
    ep.targets[(1, pairs[1])].present = False
    ep.targets[(1, pairs[0])].target_id = 0x035E0601
    h.ok(6, 0x02, slot_item(0, 1, pairs[0], name="x035") + slot_item(1, 1, pairs[1], name="l103"))
    _, _, _, _, states, _ = state(ep)
    assert states[0][1] == STATE["connected"] and struct.unpack_from("<I", states[0], 14)[0] == 0x035E0601
    assert states[1][1] == STATE["absent"] and struct.unpack_from("<Q", states[1], 4)[0] == 0   # tried at 0 ns
    assert len(states[0]) == 18 and len(states[1]) == 14                        # slot state conn last_try_at_ns scheme len tid
    listed = h.raw(1, 0x05, session=False).payload                  # connections, lock-free
    assert listed[0] == 1 and listed[1] >= 14                       # count, then len(u8) of the entry (core §2.3)
    assert listed[2 + 10] == 0b10 and listed[2 + 11] == 0            # used by slot 0 only


def test_a_lock_that_does_not_match_lets_go():
    ep, h = bench()
    pair = ep.pairs[1][0]
    ep.targets[(1, pair)].target_id = 0x035E0601
    lock = (struct.pack("<I", 0xFFFFFF0F), struct.pack("<I", 0x00300500))   # another family
    h.ok(6, 0x02, slot_item(0, 1, pair, lock=lock))
    st = state(ep)[4][0]
    assert st[1] == STATE["lock_mismatch"] and not ep.conns
    assert h.raw(6, 0x02, slot_item(1, 1, ep.pairs[1][1], lock=(b"\xff\xff", b"\x00\x00"))).detail == m.MALFORMED   # not the scheme's 4 bytes


def test_the_seat_rule_closes_the_oldest_slot_only_connection():
    ep, h = bench()
    p = ep.pairs[1]
    h.ok(6, 0x02, slot_item(0, 1, p[0]) + slot_item(1, 1, p[1]))  # two seats, both taken by the slots
    r = h.raw(1, 0x02, bytes([0]) + SPEED + m.tlv(0x03, struct.pack("<HH", *p[2]), critical=True))
    assert r.succeeded                                              # slot 0's connection gave way
    assert {c.pair for c in ep.conns.values()} == {p[1], p[2]}
    ep.tick()
    assert ep._conn_at(1, p[0]) is None                             # evicted: no retry until a new cue
    r2 = h.raw(1, 0x02, bytes([0]) + SPEED + m.tlv(0x03, struct.pack("<HH", *p[0]), critical=True))
    assert r2.succeeded                                             # slot 1's connection goes the same way


def test_too_many_at_boot_slots_are_refused():
    ep, h = bench()
    p = ep.pairs[1]
    r = h.raw(6, 0x02, slot_item(0, 1, p[0]) + slot_item(1, 1, p[1]) + slot_item(2, 1, p[2]))
    assert r.detail == m.UNAVAILABLE and not ep.slots


@pytest.mark.parametrize("name", ["", "Upper", "a/b", "x" * 33])
def test_slot_names_are_url_safe(name):
    ep, h = bench()
    assert h.raw(6, 0x02, slot_item(0, 1, ep.pairs[1][0], name=name or None) if name else
                 m.tlv(ITEM["slot"], struct.pack("<BHHHBIIBBB", 0, 1, *ep.pairs[1][0], 1, 1000, 0, 0, 2, 0) + b"\x00")).detail == m.MALFORMED


def test_slot_refusals_follow_the_reason_table():
    """probe.config §2's table: a pair the wire does not offer -> unsupported, two slots on one place -> malformed,
    a bind to a slot without a console -> malformed, a port that is no serial port -> unsupported, the mechanism
    none -> a slot that opens no console."""
    ep, h = bench()
    p = ep.pairs[1]
    assert h.raw(6, 0x02, slot_item(0, 1, (9, 10))).detail == m.UNSUPPORTED
    assert h.raw(6, 0x02, slot_item(0, 1, p[0]) + slot_item(1, 1, p[0])).detail == m.MALFORMED
    assert h.raw(6, 0x02, slot_item(0, 1, p[0], mech=0xFF) + bind_item(0, MODE["manual"], [(KIND["slot_console"], 0)])).detail == m.MALFORMED
    assert h.raw(6, 0x02, slot_item(0, 1, p[0]) + bind_item(1, MODE["manual"], [(KIND["slot_console"], 0)])).detail == m.UNSUPPORTED
    assert h.raw(6, 0x02, slot_item(0, 1, p[0]) + bind_item(0, MODE["manual"], [(KIND["fixture_uart"], 4)])).detail == m.UNSUPPORTED
    assert h.raw(6, 0x02, slot_item(0, 1, p[0]) + bind_item(0, MODE["manual"], [(KIND["fixture_uart"], 99)])).detail == m.UNKNOWN_FUNCTION
    host_retry = m.tlv(ITEM["slot"], struct.pack("<BHHHBIIBBB", 0, 1, *p[0], ATTACH["host"], 1000, 0, 0, 2, 2) + b"s0\x00")
    assert h.raw(6, 0x02, host_retry).detail == m.MALFORMED         # retry_ms on a host slot
    h.ok(6, 0x02, slot_item(0, 1, p[0], mech=0xFF))
    assert ep._conn_at(1, p[0]) is not None and not ep.streams      # attached, no console opened


# ---- binds and the serial port ---------------------------------------------------------------------------

def test_bind_streams_carry_a_len_and_a_longer_ones_tail_is_skipped():
    ep, h = bench()
    head = struct.pack("<BBBB", 0, MODE["mixed"], 0, 1)
    longer = m.tlv(ITEM["bind"], head + struct.pack("<BBHH", 5, KIND["slot_console"], 0, 0xBEEF))
    assert h.raw(6, 0x02, slot_item(0, 1, ep.pairs[1][0]) + longer).succeeded
    assert ep.binds[0].streams == ((KIND["slot_console"], 0),)
    short = m.tlv(ITEM["bind"], head + struct.pack("<BBH", 2, KIND["slot_console"], 0))
    assert h.raw(6, 0x02, short).detail == m.MALFORMED


def framed(req):
    return cobs.frame(req.pack())


def test_raw_console_flows_until_a_session_and_resumes_from_its_last_reset():
    clock = Clock()
    ep = endpoint.Endpoint(fake.p4_bench(), clock)
    p = ep.pairs[1]
    ep.load_config([slot_item(0, 1, p[0], name="x035"), bind_item(0, MODE["last_reset"], [(KIND["slot_console"], 0)])])
    port = fake_serial.FakeSerialPort(ep, 0)
    ep.target_says(b"boot\n", ep.targets[(1, p[0])])
    assert port.output() == b"boot\n"
    port.feed(framed(m.Request(1, 0, m.OP_OPEN, struct.pack("<IIB", 9, 3000, 0))))
    out = port.output()
    assert out.startswith(b"\x00") and out.endswith(b"\x00")        # the answer, framed both sides
    assert m.Result.unpack(cobs.unframe(out[1:-1])).succeeded
    ep.target_says(b"held\n", ep.targets[(1, p[0])])
    assert port.output() == b""                                     # the session holds this port
    conn = ep._conn_at(1, p[0])
    port.feed(framed(m.Request(2, 2, 0x04, struct.pack("<HB", conn, 0), 9)))   # riscv-dm reset (mark reset detail 1)
    port.output()
    ep.target_says(b"after reset\n", ep.targets[(1, p[0])])
    port.feed(framed(m.Request(3, 0, m.OP_END, b"", 9)))
    out = port.output()
    assert out.endswith(b"\x00after reset\n")                      # the end's answer, then from the reset


def test_raw_bytes_from_the_host_reach_the_selected_console_and_a_broken_frame_is_raw_too():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    p = ep.pairs[1]
    ep.load_config([slot_item(0, 1, p[0]), bind_item(0, MODE["manual"], [(KIND["slot_console"], 0)])])
    port = fake_serial.FakeSerialPort(ep, 0)
    port.feed(b"hi\x00zz\x00")
    sid = ep.stream_keys[(ep._conn_at(1, p[0]), 2)]
    assert bytes(ep.streams[sid].written) == b"hi\x00zz"            # the closing 0x00 starts the next candidate


def test_a_candidate_that_stops_for_200_ms_is_raw():
    clock = Clock()
    ep = endpoint.Endpoint(fake.p4_bench(), clock)
    p = ep.pairs[1]
    ep.load_config([slot_item(0, 1, p[0]), bind_item(0, MODE["manual"], [(KIND["slot_console"], 0)])])
    port = fake_serial.FakeSerialPort(ep, 0)
    port.feed(b"\x00abc")
    clock.t += 250
    port.tick()
    sid = ep.stream_keys[(ep._conn_at(1, p[0]), 2)]
    assert bytes(ep.streams[sid].written) == b"\x00abc"


def test_mixed_marks_lines_with_the_slot_name_and_takes_no_input():
    clock = Clock()
    ep = endpoint.Endpoint(fake.p4_bench(), clock)
    p = ep.pairs[1]
    ep.load_config([slot_item(0, 1, p[0], name="x035"), slot_item(1, 1, p[1], name="l103"),
                    bind_item(3, MODE["mixed"], [(KIND["slot_console"], 0), (KIND["slot_console"], 1)])])
    port = fake_serial.FakeSerialPort(ep, 3)
    ep.target_says(b"one\ntw", ep.targets[(1, p[0])])
    ep.target_says(b"two\n", ep.targets[(1, p[1])])
    assert port.output() == b"[x035] one\n[l103] two\n"
    clock.t += 150
    assert port.output() == b"[x035] tw\n"                          # closed by quiet
    port.feed(b"typed")
    assert all(not s.written for s in ep.streams.values())


def test_last_reset_follows_the_target_the_host_reset():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    p = ep.pairs[1]
    ep.load_config([slot_item(0, 1, p[0]), slot_item(1, 1, p[1]),
                    bind_item(0, MODE["last_reset"], [(KIND["slot_console"], 0), (KIND["slot_console"], 1)])])
    h = Host(ep, transport=1)                                       # over vendor bulk: port 0 is not held
    h.open()
    h.ok(2, 0x04, struct.pack("<HB", ep._conn_at(1, p[1]), 0))
    ep.target_says(b"from b\n", ep.targets[(1, p[1])])
    ep.target_says(b"from a\n", ep.targets[(1, p[0])])
    port = fake_serial.FakeSerialPort(ep, 0)
    assert port.output() == b"from b\n"
    (bind_state,) = state(ep)[5]
    assert bind_state == bytes([0, MODE["last_reset"], 1, CFG.enum["bind_flow"]["streaming"]])


# ---- fake_serve on a pty -----------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux", reason="pty and TIOCEXCL as on Linux")
def test_fake_serve_pty_speaks_cobs_with_console_bytes_and_honours_tiocexcl():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--pty", "--profile", "p4-bench",
                             "--slot", "x035", "--bind", "last-reset", "--console", "tick %d\\n", "--every", "20"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        where = proc.stdout.readline().split()
        assert where[0] == "PTY"
        fd = os.open(where[1], os.O_RDWR | os.O_NOCTTY)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        if os.geteuid() != 0:
            with pytest.raises(OSError):
                os.open(where[1], os.O_RDWR | os.O_NOCTTY)          # the second open: EBUSY
        os.write(fd, cobs.frame(m.Request(1, 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([1, 1])).pack()))
        got, deadline = b"", time.monotonic() + 3
        while time.monotonic() < deadline and not (b"tick" in got and got.count(b"\x00") >= 2):
            got += os.read(fd, 4096)
        assert b"tick" in got                                        # the raw console, bound to this port
        frames = [f for f in got.split(b"\x00") if f]
        answers = []
        for f in frames:
            try:
                answers.append(m.Result.unpack(cobs.unframe(f)))
            except (cobs.CorruptFrame, ValueError):
                pass                                                 # console bytes: noise to the host
        assert [a.corr for a in answers] == [1] and answers[0].succeeded
        os.close(fd)
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_fake_serve_run_hook_from_a_file(tmp_path):
    from oep_client import fake_serve
    hook = tmp_path / "loader.py"
    hook.write_text("def run(target, pc, regs):\n    target.mem[0x100] = regs.get(0x100A, 0)\n    return True, pc + 4, 7\n")
    a = fake_serve.main.__globals__["argparse"].Namespace(
        profile="p4-x035", target_id=None, absent=[], slot=[], bind=None, port_index=0, run_hook=f"{hook}:run",
        uart_plan=False)
    ep = fake_serve.build(a)
    assert ep.target.run_hook(0x2000, {0x100A: 5}) == (True, 0x2004, 7) and ep.target.mem[0x100] == 5


def test_a_closed_console_stays_readable_and_the_same_place_opens_it_again_under_its_number():
    """console §2: a closed stream reads until its place is opened again; the same mechanism there brings it back under
    its number (flags bit0, mark attach), another mechanism makes it go. Connections and streams share one number
    space; a connection's number where a stream goes is unavailable cause 6."""
    ep, h = bench()
    p = ep.pairs[1]
    def attach(pair):
        r = h.raw(1, 0x02, bytes([0]) + SPEED + m.tlv(0x03, struct.pack("<HH", *pair), critical=True))
        return struct.unpack_from("<H", r.payload)[0]
    a = attach(p[0])
    sa = struct.unpack_from("<H", h.ok(3, 0x01, struct.pack("<HB", a, 2)))[0]
    assert a == 1 and sa == 2                                        # one space, from 1 (core §9)
    assert h.raw(3, 0x02, struct.pack("<HBQH", a, 1, 0, 16), session=False).detail == m.UNAVAILABLE   # a connection's number
    assert h.raw(2, 0x02, struct.pack("<H", sa)).detail == m.UNAVAILABLE                               # a stream's number
    assert h.raw(3, 0x02, struct.pack("<HBQH", 77, 1, 0, 16), session=False).detail == m.NO_CONNECTION  # unknown
    ep.emit(sa, b"bye")
    h.ok(1, 0x03, struct.pack("<H", a))                              # detach: the stream closes, stays readable
    b = attach(p[1])
    h.ok(3, 0x01, struct.pack("<HB", b, 2))                          # the same mechanism on another place
    assert h.raw(3, 0x02, struct.pack("<HBQH", sa, 1, 0, 16), session=False).succeeded
    a2 = attach(p[0])
    again = m.Reader(h.ok(3, 0x01, struct.pack("<HB", a2, 2)))
    assert again.take("HB") == (sa, 1)                               # the same place, mechanism: the same number, bit0
    rd = m.Reader(h.raw(3, 0x02, struct.pack("<HBQH", sa, 1, 0, 16), session=False).payload)
    assert rd.take("QB") == (0, 0) and rd.counted("H") == b"bye"     # position and bytes carried on
    kinds = [mk[2] for mk in ep.streams[sa].marks]
    assert kinds == [MARK_KIND["attach"], MARK_KIND["detach"], MARK_KIND["closed"], MARK_KIND["attach"]]
    h.ok(1, 0x03, struct.pack("<H", a2))
    a3 = attach(p[0])
    h.ok(3, 0x01, struct.pack("<HB", a3, 1))                         # another mechanism there: the old one goes
    assert h.raw(3, 0x02, struct.pack("<HBQH", sa, 1, 0, 16), session=False).detail == m.NO_CONNECTION
    rd = m.Reader(h.raw(3, 0x08, session=False).payload)             # streams: the live ones (b's and a3's), lock-free
    rows = [rd.element().take("HHBBB") for _ in range(rd.u8())]
    (row,) = [r for r in rows if r[1] == a3]
    assert len(rows) == 2 and row[2:] == (1, 1, 0) and row[0] not in (sa, a, a2, a3, b)   # stream connection mechanism users state


def test_a_stream_lives_while_anything_uses_it():
    """console §2: the stream's users are the session that opened it and the bound slot; close and a lease lapse take
    one share each (mark closed 1 / 2 when the last goes), a slot's removal takes its share (3)."""
    clock = Clock()
    ep = endpoint.Endpoint(fake.p4_bench(), clock)
    p = ep.pairs[1]
    ep.load_config([slot_item(0, 1, p[0], name="x035"), bind_item(0, MODE["manual"], [(KIND["slot_console"], 0)])])
    h = Host(ep, transport=1)
    h.open(lease=1000)
    conn = ep._conn_at(1, p[0])
    sid, flags = m.Reader(h.ok(3, 0x01, struct.pack("<HB", conn, 2))).take("HB")
    assert flags == 1 and ep.streams[sid].users == {"host", ("slot", 0)}
    rd = m.Reader(h.raw(3, 0x08, session=False).payload)
    assert rd.u8() == 1 and rd.element().take("HHBBB") == (sid, conn, 2, 0b11, 0)   # users: session and slot
    h.ok(3, 0x07, struct.pack("<H", sid))                            # close: the slot still uses it
    assert ep.streams[sid].users == {("slot", 0)} and not ep.streams[sid].closed
    h.ok(3, 0x01, struct.pack("<HB", conn, 2))
    clock.t = 5000
    ep.tick()                                                        # the lease lapsed: the session's share goes
    assert ep.streams[sid].users == {("slot", 0)} and not ep.streams[sid].closed
    h2 = Host(ep, 0x99, transport=1)
    h2.open()
    assert h2.raw(6, 0x05, bytes([1, 2, ITEM["slot"], 0])).detail == m.MALFORMED   # the bind would point at nothing
    h2.ok(6, 0x05, bytes([2, 2, ITEM["slot"], 0, 2, ITEM["bind"], 0]))   # unset slot and bind: the share goes, it closes
    assert ep.streams[sid].closed and ep.streams[sid].marks[-1][2:5:2] == (MARK_KIND["closed"], 3)
    h2.ok(3, 0x07, struct.pack("<H", sid))                           # closing a closed stream: ok


def test_fake_serve_uart_plan_and_rx():
    import time
    from oep_client import fixture, link
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--tcp", "0", "--framing", "length",
                             "--profile", "esp32-v003", "--uart-plan", "--uart-rx", "rx %d\\n", "--every", "10"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        port = proc.stdout.readline().split()[1]
        hst = link.open_host(f"tcp://127.0.0.1:{port}", timeout=1.0)
        hst.open(3000)
        uart = fixture.FixtureUartIO(hst)
        assert uart.configure(115200) > 0                            # the saved plan: configure works
        assert uart.configure(9600) > 0                              # and again, while it runs
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


def test_the_closing_0x00_of_a_frame_is_not_raw_after_the_gap():
    clock = Clock()
    ep = endpoint.Endpoint(fake.p4_bench(), clock)
    p = ep.pairs[1]
    ep.load_config([slot_item(0, 1, p[0]), bind_item(0, MODE["manual"], [(KIND["slot_console"], 0)])])
    port = fake_serial.FakeSerialPort(ep, 0)
    port.feed(framed(m.Request(1, 0, m.OP_LOCK_STATE, b"")))       # lock-free: the port is not held
    clock.t += 250
    port.tick()
    sid = ep.stream_keys[(ep._conn_at(1, p[0]), 2)]
    assert bytes(ep.streams[sid].written) == b""                   # no stray 0x00 at the target


def test_read_from_a_last_mark_that_is_not_there_is_from_now():
    ep, h = bench()
    p = ep.pairs[1][0]
    r = h.raw(1, 0x02, bytes([0]) + SPEED + m.tlv(0x03, struct.pack("<HH", *p), critical=True))
    uart_fn = 5
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", uart_fn, 1, 20)) + m.tlv(0x90, struct.pack("<HBH", uart_fn, 2, 21)))
    h.ok(uart_fn, 0x01, struct.pack("<I", 115200))
    ep.uart_rx(uart_fn, b"old output")
    rd = m.Reader(h.raw(uart_fn, 0x02, struct.pack("<BQH", 3, MARK_KIND["reset"], 64), session=False).payload)
    assert rd.take("QB") == (10, 0) and rd.counted("H") == b""      # no reset mark: from now, not the old bytes


def test_no_default_reset_line_only_the_declared_channels():
    """oep-if-debug §3: the attach's reset TLV names its channel; the probe takes only role 3 (reset) channels
    (unsupported, tag 0x85 otherwise), and not one a plan holds (unavailable). Op 0x04 is gone."""
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    h = Host(ep)
    h.open()
    assert ep.reset_channels[1] == {23}
    reset = lambda ch, hold=20: m.tlv(0x05, struct.pack("<HH", ch, hold), critical=True)
    for channel in (0xFFFF, 22):                                    # no default; 22 is not a reset line
        r = h.raw(1, 0x02, b"\x01" + SPEED + reset(channel))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([0x85]))
    rd = m.Reader(h.ok(1, 0x02, b"\x01" + SPEED + reset(23)))
    conn, _, flags, _ = rd.take("HIBI")
    assert flags & 0x08 and rd.tail().get(0x11) == struct.pack("<I", 0)   # halted at the reset vector: TLV dpc
    assert h.raw(1, 0x04, struct.pack("<HH", 23, 20)).detail == m.UNKNOWN_OPERATION
    assert h.raw(1, 0x02, b"\x01" + SPEED + reset(23, 20000)).detail == m.UNSUPPORTED   # hold_ms over max_op_ms
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 4, 1, 23)))   # fixture.gpio takes NRST
    assert h.raw(1, 0x02, b"\x01" + SPEED + reset(23)).detail == m.UNAVAILABLE
    assert h.raw(1, 0x02, b"\x01").detail == m.MALFORMED             # no max_speed


def test_idle_clock_is_rvswd_s_and_a_slot_carries_the_line_settings():
    ep, h = bench()
    pair = ep.pairs[1][0]
    idle_low = m.tlv(0x04, b"\x01", critical=True)
    h.ok(1, 0x02, b"\x01" + SPEED + m.tlv(0x03, struct.pack("<HH", *pair), critical=True) + idle_low)
    assert ep.conns[ep._conn_at(1, pair)].idle_clock == 1
    assert h.raw(1, 0x02, b"\x01" + SPEED + m.tlv(0x03, struct.pack("<HH", *pair), critical=True)
                 + m.tlv(0x04, b"\x02", critical=True)).detail == m.MALFORMED
    assert h.raw(1, 0x01, b"\x00" + m.tlv(0x04, b"\x01", critical=True) + m.tlv(0x01, SPEED[2:], critical=True)).succeeded   # scan takes them too
    ep2, h2 = bench()
    p2 = ep2.pairs[1][1]
    h2.ok(6, 0x02, slot_item(0, 1, p2, name="l103", max_speed=1_000_000, idle=1))
    c = ep2.conns[ep2._conn_at(1, p2)]
    assert (c.speed, c.idle_clock) == (1_000_000, 1)                 # the probe's own attach uses the slot's settings
    v003 = endpoint.Endpoint(fake.esp32_v003(), Clock())
    hv = Host(v003)
    hv.open()
    assert hv.raw(9, 0x02, slot_item(0, 1, v003.pairs[1][0], idle=1)).detail == m.UNSUPPORTED   # swio has no idle_clock
    r = hv.raw(1, 0x02, b"\x01" + SPEED + idle_low)
    assert r.resolution == m.REJECTED and r.detail == m.UNSUPPORTED  # an unknown critical TLV on swio


def pins_probe():
    ep = endpoint.Endpoint(fake.rp2350_pins(), Clock())
    h = Host(ep)
    h.open()
    return ep, h


def test_a_wire_takes_any_free_pair_the_host_names():
    """oep-if-debug §1: role_channels pairs; count 0 in swdio / swclk order; a live connection holds its pins."""
    ep, h = pins_probe()
    pins = lambda d, c: m.tlv(0x03, struct.pack("<HH", d, c), critical=True)
    r = m.Reader(h.ok(1, 0x01, b"\x00"))                              # count 0: the first 255 free pairs
    tried, count = r.take("BB")
    assert tried == 255 and count == 1 and r.element().take("BHHI")[1:3] == (0, 1)
    skip = lambda n: m.tlv(0x02, struct.pack("<H", n))               # scan's skip is TLV 0x02 (0x01 is max_speed)
    assert m.Reader(h.ok(1, 0x01, b"\x00" + skip(29 * 28 - 10))).take("B") == 10   # the last ten
    assert m.Reader(h.ok(1, 0x01, b"\x00" + skip(29 * 28))).take("B") == 0         # the end
    assert h.raw(1, 0x01, b"\x01" + struct.pack("<HH", 0, 1) + skip(1)).detail == m.MALFORMED
    conn = struct.unpack_from("<H", h.ok(1, 0x02, b"\x01" + SPEED + pins(0, 1)))[0]
    assert h.raw(1, 0x02, b"\x01" + SPEED + pins(1, 2)).detail == m.UNAVAILABLE    # GP1 is the live connection's
    assert h.raw(0, m.OP_PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 4, 1, 0))).detail == m.UNAVAILABLE
    assert m.Reader(h.ok(1, 0x01, b"\x00")).take("B") == 1           # its one seat is taken: the live pair only
    assert struct.unpack_from("<H", h.ok(1, 0x02, b"\x00" + SPEED))[0] == conn   # no pins: the one live connection
    assert h.raw(1, 0x01, b"\x01" + struct.pack("<HH", 2, 3)).detail == m.UNAVAILABLE
    h.ok(1, 0x03, struct.pack("<H", conn) + m.tlv(0x01, b""))       # detach (force): the pins go back
    h.ok(0, m.OP_PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 4, 1, 5)))
    assert h.raw(1, 0x01, b"\x01" + struct.pack("<HH", 5, 6)).detail == m.UNAVAILABLE   # GP5 is the plan's
    assert h.raw(1, 0x02, b"\x01" + SPEED + pins(3, 3)).detail == m.UNSUPPORTED         # one channel twice: no such pair
    assert h.raw(1, 0x02, b"\x01" + SPEED + pins(19, 3)).detail == m.UNSUPPORTED        # not offered (PSRAM CS)
    assert h.raw(1, 0x02, b"\x01" + SPEED).detail == m.UNAVAILABLE  # no pins, no live connection: the host chooses
    r = h.raw(1, 0x02, b"\x01" + SPEED + pins(7, 8))                # a free pair nothing answers on
    assert r.resolution == m.COMPLETED and not r.succeeded and r.payload == bytes([2])   # failed: status line [TLV]


def test_the_client_scan_walks_the_whole_count_0_list():
    from oep_client import host as hh, riscv as target
    ep = endpoint.Endpoint(fake.rp2350_pins(), Clock())
    hst = hh.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    ep._target(1, (28, 29)).present = True                        # the last pair of the list
    found = target.Wire(hst).scan(max_speed=1_000_000)
    assert [f.pins for f in found] == [(0, 1), (28, 29)]
    assert ep.requests[-1].payload.endswith(m.tlv(0x01, struct.pack("<I", 1_000_000), critical=True))


def test_dmi_and_run_answers_count_their_values_and_rejects_follow_the_order():
    """oep-if-debug §4: dmi answers done status nvals values; run answers ... nvals values; waits over max_op_ms are
    unsupported, a running hart's read_block is status state, an odd address malformed."""
    ep, h = bench()
    conn = struct.unpack_from("<H", h.ok(1, 0x02, b"\x00" + SPEED + m.tlv(0x03, struct.pack("<HH", *ep.pairs[1][0]), critical=True)))[0]
    ep.targets[(1, ep.pairs[1][0])].dmi[0x11] = 0x382
    r = h.ok(2, 0x01, struct.pack("<HH", conn, 2) + bytes([2, 0x11]) + bytes([2, 0x11]))
    assert r == struct.pack("<HBH", 2, 0, 2) + struct.pack("<II", 0x382, 0x382)
    assert h.raw(2, 0x01, struct.pack("<HH", conn, 1) + struct.pack("<BI", 4, 20_000_000)).detail == m.UNSUPPORTED
    r = h.raw(2, 0x05, struct.pack("<HIH", conn, 0x20000000, 2))   # read_block on a running hart
    assert r.detail == m.FAILED and r.payload == struct.pack("<HB", 0, 5)
    assert h.raw(2, 0x05, struct.pack("<HIH", conn, 0x20000001, 2)).detail == m.MALFORMED
