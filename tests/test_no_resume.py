"""core §6.2 / §9, common §2, console §2, debug §2: when a session's lock ends - end, lease expiry, force - the probe
releases everything the session created (its plan, its console streams and then its connections, its subscriptions)
and passes nothing to the next session; only open takes the lock. What lasts is the settings."""

import random
import struct

import pytest

from oep_client import console, core, endpoint, virtual_bench, fixture, host as h, message as m, registry as reg, riscv



class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench(profile=virtual_bench.p4_bench):
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    return clock, ep


def new_host(ep, seed):
    return h.Host(lambda b: ep.handle(b, 1), rng=random.Random(seed))


@pytest.mark.parametrize("how", ["end", "lapse", "force"])
def test_a_session_end_of_any_kind_releases_what_it_created(how):
    clock, ep = bench(virtual_bench.p4_x035)                                           # with a logic capture to subscribe to
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
    assert sid not in ep.streams                                             # released with its buffer (console §2)
    reader = new_host(ep, 3)                                                  # no session: lock-free reads
    c2 = console.Console(reader)
    c2.stream = sid
    with pytest.raises(h.NoResource):
        c2.read(c2.FROM_OLDEST)                                               # a closed number: no_resource


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


def test_the_next_session_opens_a_new_stream_under_a_new_number():
    """console §2, core §9: the next session's open makes a new stream; the old number is never used again. A host
    that keeps the output across commands reads it before the session ends (console §2)."""
    clock, ep = bench()
    pair = ep.pairs[1][0]
    first = new_host(ep, 1)
    first.open(3000)
    conn, _ = riscv.Wire(first).attach(halt=False, pins=pair)
    con = console.Console(first)
    sid = con.open(conn)
    ep.emit(sid, b"boot banner\n")
    assert con.read(con.FROM_OLDEST).data == b"boot banner\n"
    first.end()
    second = new_host(ep, 2)
    second.open(3000)
    conn2, _ = riscv.Wire(second).attach(halt=False, pins=pair)
    con2 = console.Console(second)
    sid2 = con2.open(conn2)
    assert conn2 not in (conn,) and sid2 not in (sid, conn, conn2)
    assert con2.read(con2.FROM_OLDEST).data == b"" and con2.streams() == [console.StreamInfo(sid2, conn2, con2.DMSEQ)]


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
