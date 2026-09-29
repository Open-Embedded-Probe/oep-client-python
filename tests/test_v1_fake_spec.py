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

from oep_client.v1 import cobs, endpoint, fake, fake_serial, message as m, registry as reg

CFG = reg.PROBE_CONFIG
ITEM, ATTACH, MODE, KIND = CFG.tlv["item"], CFG.enum["slot_attach"], CFG.enum["bind_mode"], CFG.enum["bind_stream"]
STATE = CFG.enum["slot_state"]


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


def slot_item(n, wire, pair, attach=ATTACH["at_boot"], retry=1, mech=2, name=None, lock=None):
    raw = (name or f"s{n}").encode()
    value = struct.pack("<BHHHBHBB", n, wire, *pair, attach, retry if attach == ATTACH["at_boot"] else 0, mech,
                        len(raw)) + raw
    value += b"\x00" if lock is None else bytes([1]) + lock[0] + lock[1]
    return m.tlv(ITEM["slot"], value)


def bind_item(port, mode, streams, selected=0):
    return m.tlv(ITEM["bind"], struct.pack("<BBBB", port, mode, selected, len(streams))
                 + b"".join(struct.pack("<BH", k, i) for k, i in streams))


def describe(ep, fn):
    h = Host(ep)
    r = h.raw(0, m.OP_DESCRIBE, struct.pack("<HH", fn, 0), session=False)
    return m.split_tlvs(r.payload[1:])


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
    r = h.raw(wire, 0x02, bytes([0]))                                # attach
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

def test_core_describe_lists_the_transports_and_the_oep_pid():
    tlvs = describe(endpoint.Endpoint(fake.p4_x035(), Clock()), 0)
    tags = reg.CORE.tlv["describe"]
    kinds = [v[1] for t, v in tlvs if t == tags["transport"]]
    assert kinds == [3, 4, 5, 2] and (tags["oep_pid"], b"\x01") in tlvs
    assert any(t == tags["unit_id"] for t, _ in tlvs) and 0x48 not in [t for t, _ in tlvs]


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
    states = {v[0]: v for t, v in describe(ep, 6) if t == CFG.tlv["describe"]["slot_state"]}
    assert states[0][1] == STATE["connected"] and struct.unpack_from("<I", states[0], 10)[0] == 0x035E0601
    assert states[1][1] == STATE["absent"] and struct.unpack_from("<I", states[1], 4)[0] < 10
    listed = h.raw(1, 0x05, session=False).payload                  # connections, lock-free
    assert listed[0] == 1 and listed[1 + 10] == 0b10 and listed[1 + 11] == 0   # used by slot 0 only


def test_a_lock_that_does_not_match_lets_go():
    ep, h = bench()
    pair = ep.pairs[1][0]
    ep.targets[(1, pair)].target_id = 0x035E0601
    lock = (struct.pack("<I", 0xFFFFFF0F), struct.pack("<I", 0x00300500))   # another family
    h.ok(6, 0x02, slot_item(0, 1, pair, lock=lock))
    state = next(v for t, v in describe(ep, 6) if t == CFG.tlv["describe"]["slot_state"])
    assert state[1] == STATE["lock_mismatch"] and not ep.conns


def test_the_seat_rule_closes_the_oldest_slot_only_connection():
    ep, h = bench()
    p = ep.pairs[1]
    h.ok(6, 0x02, slot_item(0, 1, p[0]) + slot_item(1, 1, p[1]))  # two seats, both taken by the slots
    r = h.raw(1, 0x02, bytes([0]) + m.tlv(0x03, struct.pack("<HH", *p[2]), critical=True))
    assert r.succeeded                                              # slot 0's connection gave way
    assert {c.pair for c in ep.conns.values()} == {p[1], p[2]}
    ep.tick()
    assert ep._conn_at(1, p[0]) is None                             # evicted: no retry until a new cue
    r2 = h.raw(1, 0x02, bytes([0]) + m.tlv(0x03, struct.pack("<HH", *p[0]), critical=True))
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
                 m.tlv(ITEM["slot"], struct.pack("<BHHHBHBB", 0, 1, *ep.pairs[1][0], 1, 1, 2, 0) + b"\x00")).detail == m.MALFORMED


# ---- binds and the serial port ---------------------------------------------------------------------------

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
    port.feed(framed(m.Request(2, 2, 0x04, struct.pack("<HB", conn, 0), 9)))   # riscv-dm reset
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
    state = next(v for t, v in describe(ep, 6) if t == CFG.tlv["describe"]["bind_state"])
    assert state == bytes([0, MODE["last_reset"], 1, CFG.enum["bind_flow"]["streaming"]])


# ---- fake_serve on a pty -----------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux", reason="pty and TIOCEXCL as on Linux")
def test_fake_serve_pty_speaks_cobs_with_console_bytes_and_honours_tiocexcl():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.v1.fake_serve", "--pty", "--profile", "p4-bench",
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
    from oep_client.v1 import fake_serve
    hook = tmp_path / "loader.py"
    hook.write_text("def run(target, pc, regs):\n    target.mem[0x100] = regs.get(0x100A, 0)\n    return True, pc + 4, 7\n")
    a = fake_serve.main.__globals__["argparse"].Namespace(
        profile="p4-x035", target_id=None, absent=[], slot=[], bind=None, port_index=0, run_hook=f"{hook}:run",
        uart_plan=False)
    ep = fake_serve.build(a)
    assert ep.target.run_hook(0x2000, {0x100A: 5}) == (True, 0x2004, 7) and ep.target.mem[0x100] == 5


def test_a_closed_console_stays_readable_until_the_same_place_opens_again():
    ep, h = bench()
    p = ep.pairs[1]
    def attach(pair):
        r = h.raw(1, 0x02, bytes([0]) + m.tlv(0x03, struct.pack("<HH", *pair), critical=True))
        return struct.unpack_from("<H", r.payload)[0]
    a = attach(p[0])
    sa = struct.unpack_from("<H", h.ok(3, 0x01, struct.pack("<HB", a, 2)))[0]
    h.ok(1, 0x03, struct.pack("<H", a))                              # detach: the stream closes, stays readable
    b = attach(p[1])
    h.ok(3, 0x01, struct.pack("<HB", b, 2))                          # the same mechanism on another place
    assert h.raw(3, 0x02, struct.pack("<HBQH", sa, 1, 0, 16), session=False).succeeded
    a2 = attach(p[0])
    h.ok(3, 0x01, struct.pack("<HB", a2, 2))                         # the same place again: the old one goes
    assert h.raw(3, 0x02, struct.pack("<HBQH", sa, 1, 0, 16), session=False).detail == m.UNAVAILABLE


def test_fake_serve_uart_plan_and_rx():
    import time
    from oep_client.v1 import fixture, link
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.v1.fake_serve", "--tcp", "0", "--framing", "length",
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
