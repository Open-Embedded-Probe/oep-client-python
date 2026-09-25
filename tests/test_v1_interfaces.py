"""Revision 1 interface shapes end to end against the fake endpoint (oep-spec v1-core-wire-delta §5.4-§5.8): wire
attach, riscv-dm (dmi n + poll values + the done rule, block done/status, run n_out), no connection, console rev 1,
fixture.gpio / uart rev 1, and the interface revision check."""

import random
import struct

import pytest

from oep_client.v1 import capture, console, core, endpoint, fake, fixture, host, message as m, riscv

WIRE, DM, CONSOLE, GPIO, UART = 1, 2, 3, 4, 5          # p4_x035


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


@pytest.fixture
def bench():
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=10000)
    return ep, hst


@pytest.fixture
def dm(bench):
    ep, hst = bench
    conn, _ = riscv.Wire(hst).attach(halt=True)
    return ep, hst, riscv.RiscvDm(hst, conn)


# ---- revision check -----------------------------------------------------------------------------------------

def test_an_interface_in_another_revision_is_not_used():
    probe = fake.FakeProbe("old", 256, [fake.Offered(0, 0, "oep.core"),
                                        fake.Offered(1, 1, "oep.wire.rvswd", revision=0),
                                        fake.Offered(2, 1, "oep.target.console", revision=2)])
    hst = host.Host(endpoint.Endpoint(probe, Clock()).handle)
    with pytest.raises(core.UnsupportedRevision, match="revision 0 on this probe"):
        riscv.Wire(hst)
    with pytest.raises(core.UnsupportedRevision, match="revision 2"):
        console.Console(hst)


# ---- oep.wire.* ---------------------------------------------------------------------------------------------

def test_attach_twice_returns_the_same_connection_and_max_speed_is_critical(bench):
    ep, hst = bench
    wire = riscv.Wire(hst)
    conn, status = wire.attach(halt=True, max_speed=1_000_000)
    assert wire.had_reset and not wire.existing and wire.speed_hz == 1_000_000 and status & 0x300
    assert ep.requests[-1].payload == bytes([1, 0x81, 4]) + struct.pack("<I", 1_000_000)
    again, _ = wire.attach(halt=False)
    assert again == conn and wire.existing and not wire.had_reset


def test_no_connection_after_the_probe_lost_it(dm):
    ep, hst, d = dm
    ep.lose_connections()
    with pytest.raises(host.NoConnection):
        d.halt()
    with pytest.raises(host.NoConnection):
        riscv.Wire(hst).detach(d.conn)
    with pytest.raises(host.NoConnection):
        riscv.RiscvDm(hst, 77).read32(0)


# ---- oep.target.riscv-dm ------------------------------------------------------------------------------------

def test_dmi_counts_its_steps_and_polls_return_their_last_value(dm):
    ep, hst, d = dm
    ep.target.dmi[0x11] = 0x382
    ep.target.dmi_reads[0x16] = [0x1000, 0x1000, 0x0002]          # busy, busy, done
    steps = [d.step_write(0x04, 7), d.step_read(0x11), d.step_delay(5), d.step_poll(0x16, 0x1000, 0, 10)]
    done, values = d.dmi(steps)
    assert done == 4 and values == [0x382, 0x0002]
    sent = ep.requests[-1].payload
    assert sent[1:3] == struct.pack("<H", 4)                      # connection, then n(u16), then the steps
    assert d.dmi(b"".join(steps[:2])) == (2, [0x382])            # packed bytes are counted too


def test_a_poll_that_gives_up_stops_the_list_with_its_last_value(dm):
    ep, hst, d = dm
    ep.target.dmi[0x16] = 0x1000                                  # stays busy
    with pytest.raises(riscv.StepListError) as e:
        d.dmi([d.step_read(0x11), d.step_poll(0x16, 0x1000, 0, 3), d.step_read(0x11)])
    err = e.value
    assert err.done == 1 and err.status == riscv.STATUS["timeout"] and err.values == [0, 0x1000]
    assert err.result.detail == m.PARTIAL


def test_a_write_that_fails_on_the_line_adds_no_value(dm):
    ep, hst, d = dm
    ep.target.fail_write.add(0x10)
    with pytest.raises(riscv.StepListError) as e:
        d.dmi([d.step_write(0x10, 1), d.step_read(0x11)])
    assert (e.value.done, e.value.status, e.value.values) == (0, riscv.STATUS["line"], [])
    assert e.value.result.detail == m.FAILED


def test_dmi_value_count_rule():
    kinds = [riscv.STEP_READ, riscv.STEP_WRITE, riscv.STEP_POLL_US, riscv.STEP_WAIT_US]
    assert riscv.dmi_value_count(kinds, 4, riscv.OK) == 2
    assert riscv.dmi_value_count(kinds, 2, riscv.STATUS["timeout"]) == 2    # the failed poll adds its last value
    assert riscv.dmi_value_count(kinds, 1, riscv.STATUS["line"]) == 1       # a failed write adds nothing
    assert riscv.dmi_value_count(kinds, 2, riscv.STATUS["line"]) == 1       # a poll whose read failed adds nothing


def test_block_access_reports_how_far_it_got(dm):
    ep, hst, d = dm
    d.write_block(0x20000000, struct.pack("<3I", 1, 2, 3))
    assert d.read_block(0x20000000, 3) == struct.pack("<3I", 1, 2, 3)
    assert ep.requests[-2].payload[1:7] == struct.pack("<IH", 0x20000000, 3)   # address, count
    ep.target.fault_at.add(0x20000008)
    with pytest.raises(riscv.TargetError) as e:
        d.read_block(0x20000000, 4)
    assert e.value.done == 2 and e.value.data == struct.pack("<2I", 1, 2) and e.value.status == riscv.STATUS["fault"]
    with pytest.raises(riscv.TargetError) as e:
        d.write_block(0x20000000, bytes(16))
    assert e.value.done == 2 and e.value.result.detail == m.PARTIAL


def test_run_returns_the_registers_asked_for(dm):
    ep, hst, d = dm

    def hook(pc, regs):
        regs[0x100A], regs[0x100B] = 0, 0x08000100
        return True, pc + 0xB0, 1234
    ep.target.run_hook = hook
    r = d.run(0x20000000, [(0x100A, 5)], timeout_ms=None, outs=(0x100A, 0x100B))
    assert r.stopped and r.dpc == 0x200000B0 and r.elapsed_us == 1234 and r.values == [0, 0x08000100]
    body = ep.requests[-1].payload
    assert body[5:9] == b"\xff\xff\xff\xff"                       # timeout_ms u32, no limit
    assert d.run(0x20000000, [], outs=()).values == []           # n_out 0: no values

    ep.target.run_hook = lambda pc, regs: (False, pc + 8, 200000)
    r = d.run(0x20000000, [], timeout_ms=200)
    assert not r.stopped and r.status == riscv.STATUS["timeout"]  # returned, not raised


def test_state_status_and_unknown_status_are_failures(dm):
    ep, hst, d = dm
    d.resume()
    with pytest.raises(riscv.TargetError, match="state"):
        d.step()
    with pytest.raises(riscv.TargetError, match="state"):
        d.run(0x20000000, [])
    odd = m.Result(1, m.COMPLETED, m.SUCCESS, bytes([0x42]))       # a status this host does not know, outcome success
    with pytest.raises(riscv.TargetError, match="unknown status 0x42"):
        riscv.check("halt", odd, 0x42)


def test_reset_method_goes_critical_and_an_unknown_value_is_refused(dm):
    ep, hst, d = dm
    assert d.reset_halt(method=riscv.RiscvDm.METHOD_NDMRESET) == 0
    with pytest.raises(host.Unsupported) as e:
        d.reset(method=9)
    assert e.value.tag == 0x81
    flags, attempts, pc = d.reset(confirm=True)
    assert flags & 2 and attempts == 1


# ---- oep.target.console -------------------------------------------------------------------------------------

def test_console_open_returns_an_existing_stream_and_reads_by_position(dm):
    ep, hst, d = dm
    con = console.Console(hst)
    sid = con.open(d.conn, console.Console.DMSEQ)
    assert not con.existing
    io = console.ConsoleIO(con)
    ep.emit(sid, b"hello ")
    assert io.read() == b"hello "
    again = console.Console(hst)
    assert again.open(d.conn, console.Console.DMSEQ) == sid and again.existing   # a one-shot process carries on
    ep.emit(sid, b"world")
    assert io.read() == b"world" and io.position == 11
    first = con.read(console.Console.FROM_OLDEST, 0, 5)
    assert tuple(first) == (0, True, False, b"hello")
    with pytest.raises(host.Unsupported) as e:
        con.open(d.conn, 9)
    assert e.value.tag is None                                    # a fixed-part value: no payload


def test_console_marks_follow_serials_and_more(dm):
    ep, hst, d = dm
    con = console.Console(hst)
    con.open(d.conn)
    for v in range(6):
        con.mark(v)                                               # several marks at the same position
    marks = con.marks()
    assert [mk.serial for mk in marks] == list(range(7))          # attach + 6 host marks, over 3 answers
    assert [mk.kind for mk in marks] == [3] + [7] * 6 and [mk.detail for mk in marks[1:]] == list(range(6))
    assert console.MARK_NAMES[8] == "link-lost"
    page, more = con.marks_page(5)
    assert [mk.serial for mk in page] == [5, 6] and not more


def test_console_write_partial_and_a_closed_stream_stays_readable(dm):
    ep, hst, d = dm
    con = console.Console(hst)
    sid = con.open(d.conn)
    ep.console_accept = 3
    assert con.write(b"PING\n") == 3                              # completed partial: not an error
    assert bytes(ep.streams[sid].written) == b"PIN"
    ep.emit(sid, b"last words")
    ep.lose_connections()
    assert con.read().data == b"last words"
    assert con.marks()[-1].kind == 8                              # link-lost
    with pytest.raises(host.Rejected, match="unavailable"):
        con.write(b"x")


# ---- oep.fixture.gpio ---------------------------------------------------------------------------------------

def test_gpio_set_is_a_list_in_order_and_only_planned_channels(bench):
    ep, hst = bench
    core.plan_apply(hst, [(GPIO, 1, 23), (GPIO, 1, 5)])
    g = fixture.Gpio(hst, GPIO)
    g.set([(23, g.OPEN_DRAIN_LOW), (5, g.OUTPUT_HIGH), (23, g.OPEN_DRAIN_RELEASE)])
    assert ep.gpio_log == [(23, 5), (5, 4), (23, 6)]
    assert ep.requests[-1].payload == bytes([3]) + struct.pack("<HBHBHB", 23, 5, 5, 4, 23, 6)
    assert g.read([23, 5]) == [1, 1]
    with pytest.raises(host.Rejected, match="unavailable") as e:
        g.set([(5, g.OUTPUT_LOW), (40, g.OUTPUT_LOW)])
    assert e.value.result.payload == bytes([1])                   # the index of the channel it refused
    assert ep.gpio_modes[5] == g.OUTPUT_HIGH                      # nothing done
    g.pulse_low(23, 0)
    assert ep.gpio_log[-2:] == [(23, 5), (23, 6)]
    core.plan_release(hst)
    with pytest.raises(host.Rejected):
        g.read([23])


# ---- oep.fixture.uart ---------------------------------------------------------------------------------------

def test_uart_configure_format_and_reads_that_do_not_consume(bench):
    ep, hst = bench
    core.plan_apply(hst, [(UART, 1, 20), (UART, 2, 21)])
    io = fixture.FixtureUartIO(hst, UART)
    assert io.configure(115200, fixture.FixtureUart.format_byte(8, "E", 2)) == 80_000_000 // (80_000_000 // 115200)
    assert ep.uart_baud[UART][1] == 0b010100
    assert ep.requests[-2].payload[4:] == bytes([0x81, 1, 0b010100])     # format: a critical TLV
    ep.uart_rx(UART, b"READY\n")
    assert io.read() == b"READY\n" and io.read() == b""
    assert io.uart.read(io.uart.FROM_OLDEST).data == b"READY\n"          # still there: reading does not consume
    with pytest.raises(host.Unsupported) as e:
        io.uart.configure(9600, 0x80)
    assert e.value.tag == 0x81
    ep.uart_accept = 2
    assert io.uart.write(b"abc") == 2
    io.uart.mark(7)
    assert io.uart.marks()[-1].detail == 7


def test_capture_critical_tag_it_cannot_honour_is_unsupported():
    from test_v1_target_parts import ScriptedHost

    def configure(p):
        for tag, _ in m.split_tlvs(p):
            if tag == capture.TRIGGER | capture.CRITICAL:
                return m.REJECTED, m.UNSUPPORTED, bytes([tag])
        return m.COMPLETED, m.SUCCESS, bytes([m.TAG_IGNORED, 1, capture.PRETRIGGER])

    hst = ScriptedHost({(7, capture.LogicCapture.CONFIGURE): configure}, revisions={7: 1})
    cap = capture.LogicCapture(hst, 7)
    with pytest.raises(host.Unsupported) as e:
        cap.configure(rate=1_000_000, samples=100, trigger=(capture.EDGE, 0, 0), critical={capture.TRIGGER})
    assert e.value.tag == capture.TRIGGER | capture.CRITICAL
    assert cap.configure(rate=1_000_000, samples=100, pretrigger=10).ignored == [capture.PRETRIGGER]


def test_stream_io_fits_a_64_byte_frame():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())          # max_frame 64
    hst = host.Host(ep.handle, rng=random.Random(2))
    hst.open()
    core.plan_apply(hst, [(5, 1, 21), (5, 2, 22)])
    io = fixture.FixtureUartIO(hst, 5)
    io.configure(115200)
    ep.uart_accept = 1000
    io.write(bytes(120))
    writes = [len(r.payload) for r in ep.requests if r.fn == 5 and r.op == fixture.FixtureUart.WRITE]
    assert max(writes) + 10 <= 64 and sum(w - 2 for w in writes) == 120
    ep.uart_rx(5, bytes(range(100)))
    got = io.read(512)
    assert 0 < len(got) <= 54 and got == bytes(range(len(got)))
