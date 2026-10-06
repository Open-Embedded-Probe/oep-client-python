"""D1 (oep-spec 22e4319, core §6.2 / §9, common §2, console §2, debug §2): when a session's lock ends - end, lease
expiry, force - the probe releases everything the session created and passes nothing to the next session; only open
takes the lock. What lasts is the probe's: a console stream per place and mechanism (closed, readable until the next
open there, which returns its number with its position and marks), a slot's connection (an attach on that live
combination returns it), the settings."""

import random
import struct

import pytest

from oep_client import config, console, core, endpoint, fake, fixture, host as h, message as m, registry as reg, riscv

CLOSED = reg.COMMON.enum["mark_kind"]["closed"]
SESSION_ENDED = reg.COMMON.enum["mark_detail_closed"]["session_ended"]


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench(profile=fake.p4_bench):
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    return clock, ep


def new_host(ep, seed):
    return h.Host(lambda b: ep.handle(b, 1), rng=random.Random(seed))


@pytest.mark.parametrize("how", ["end", "lapse", "force"])
def test_a_session_end_of_any_kind_releases_what_it_created(how):
    clock, ep = bench(fake.p4_x035)                                           # with a logic capture to subscribe to
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open(1000)
    gpio = core.find(a, "oep.fixture.gpio")
    core.plan_apply(a, [(gpio, 1, 20)])
    fixture.Gpio(a, gpio).set([(20, fixture.Gpio.OUTPUT_HIGH)])
    conn, _ = riscv.Wire(a).attach(halt=False, pins=ep.pairs[1][0])
    con = console.Console(a)
    sid = con.open(conn)
    a.subscribe(core.find(a, "oep.fixture.logic"))                            # its own subscribe (core §11.3)
    assert ep.subscribed
    ep.emit(sid, b"last words")
    if how == "end":
        a.end()
    elif how == "lapse":
        clock.ms = 5000
        ep.tick()
    else:
        b.open(1000, force=True)
    assert not any(fn == gpio for fn, _, _ in ep.plan) and ep.parked[20] == 0   # the pin back to its idle state
    assert conn not in ep.conns and ep.subscribed == {}
    s = ep.streams[sid]
    assert s.closed and s.marks[-1][2] == CLOSED and s.marks[-1][4] == SESSION_ENDED   # closed before the connection
    reader = new_host(ep, 3)                                                  # no session: lock-free reads go on
    c2 = console.Console(reader)
    c2.stream = sid
    assert c2.read(c2.FROM_OLDEST).data == b"last words"                      # readable until the next open there


def test_no_resume_the_ended_id_is_no_session_and_open_is_the_only_way_in():
    clock, ep = bench()
    a = new_host(ep, 1)
    a.open(3000)
    sid = a.session
    a.end()
    stale = m.Request(a.next_corr(), 0, m.OP_KEEPALIVE, b"", sid).pack()
    assert m.Result.unpack(ep.handle(stale)).detail == m.NO_SESSION and ep.holder is None
    toy = m.Request(a.next_corr(), 4, reg.FIXTURE_GPIO.op["read"], bytes([0]), sid).pack()
    assert m.Result.unpack(ep.handle(toy)).detail == m.NO_SESSION          # a lock-free op with the id: checked too
    assert m.Result.unpack(ep.handle(m.Request(a.next_corr(), 4, reg.FIXTURE_GPIO.op["read"], bytes([0])).pack())).succeeded
    a.open(3000)
    assert ep.holder == a.session != sid


def test_the_next_session_reopens_the_console_under_its_number_with_position_and_marks():
    """console §2: a one-command-one-process host loses no first line - a command opens the console, resets the
    target and ends; the next command's open at the same place and mechanism returns the stream and reads on."""
    clock, ep = bench()
    pair = ep.pairs[1][0]
    first = new_host(ep, 1)
    first.open(3000)
    conn, _ = riscv.Wire(first).attach(halt=False, pins=pair)
    con = console.Console(first)
    sid = con.open(conn)
    ep.emit(sid, b"boot banner\n")
    marks = len(ep.streams[sid].marks)
    first.end()
    second = new_host(ep, 2)
    second.open(3000)
    conn2, _ = riscv.Wire(second).attach(halt=False, pins=pair)
    con2 = console.Console(second)
    assert con2.open(conn2) == sid and con2.existing                          # its number, flags bit0
    assert con2.read(con2.FROM_OLDEST).data == b"boot banner\n"
    kinds = [mk.kind for mk in con2.marks()]
    assert len(kinds) == marks + 2 and kinds[-2:] == [CLOSED, reg.COMMON.enum["mark_kind"]["attach"]]   # closed, attach


def test_a_slots_connection_outlives_every_session_and_an_attach_returns_it():
    """common §2: a session's share goes at its end; a slot keeps the connection, and a later session's attach on that
    live combination joins it (flags bit1)."""
    clock, ep = bench()
    pair = ep.pairs[1][0]
    a = new_host(ep, 1)
    a.open(3000)
    config.ProbeConfig(a).set([config.Slot(slot=0, wire_fn=1, pins=pair, name="dut", attach="at-boot")])
    conn = ep._conn_at(1, pair)
    wire = riscv.Wire(a)
    assert wire.attach(halt=False, pins=pair)[0] == conn and wire.existing
    a.end()
    assert conn in ep.conns and ep.conns[conn].users == {("slot", 0)}       # the session's share went
    b = new_host(ep, 2)
    b.open(3000)
    wire = riscv.Wire(b)
    assert wire.attach(halt=False, pins=pair)[0] == conn and wire.existing


def test_a_resent_end_is_answered_from_the_table_and_a_resent_open_restarts_the_lease():
    clock, ep = bench()
    a = new_host(ep, 1)
    a.open(2000)
    sid = a.session
    clock.ms = 1500
    again = m.Request(a.next_corr(), 0, m.OP_OPEN, struct.pack("<IB", 2000, 0), sid).pack()
    assert m.Result.unpack(ep.handle(again)).succeeded and ep.expires_ms == 3500   # the same id: lease from now
    end = m.Request(a.next_corr(), 0, m.OP_END, b"", sid).pack()
    first = ep.handle(end)
    assert ep.handle(end) == first and ep.holder is None                       # replayed, nothing re-taken
