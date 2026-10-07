"""v1 session rules end to end: messages, the lock table, watchdog, no resume (end, lapse and force release
everything, core §6.2 / §9), force, long operations."""

import struct

import pytest

from oep_client import endpoint, virtual_bench, host, message as m

TOY = 13         # a fn the virtual bench does not simulate (virtual_bench.with_stand_in(esp32_v003)) has the stand-in operations


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


@pytest.fixture
def bench():
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), clock, lease_default_ms=1000)
    return clock, ep


def new_host(ep, seed):
    import random
    return host.Host(ep.handle, rng=random.Random(seed))


def write(h, value):
    return h.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", value))


def read(h):
    return struct.unpack_from("<I", h.request(TOY, endpoint.TOY_READ, locked=False).payload)[0]


# ---- messages -------------------------------------------------------------------------------------

def test_every_request_carries_a_session_id_and_0_is_none():
    """core §4.1: one 10-byte header, role 0x01, session_id always there (0 = no session)."""
    plain = m.Request(7, 3, 0x01, b"\xaa").pack()
    held = m.Request(7, 3, 0x01, b"\xaa", session=0xDEADBEEF).pack()
    assert plain[0] == held[0] == 0x01 and len(plain) == len(held) == 10 + 1
    assert plain[6:10] == bytes(4) and m.Request(7, 3, 0x01, b"\xaa", None).pack() == plain
    assert m.Request.unpack(held).session == 0xDEADBEEF and m.Request.unpack(plain).session == 0


def test_a_48_byte_name_fits_a_64_byte_frame():
    name = "io.github.ch32-riscv-ug." + "x" * 24
    assert len(name) == 48
    probe = virtual_bench.VirtualProbe("tiny", 64, [virtual_bench.Offered(0, 0, ""), virtual_bench.Offered(1, 1, name)])
    ep = endpoint.Endpoint(probe, Clock())
    from oep_client import catalog
    result = ep.handle(m.Request(1, 0, m.OP_LIST, catalog.pack_list_request(0)).pack())
    assert len(result) == 63 and catalog.unpack_list_result(result[5:])[1][0].name == name   # no element length (§2.3)


# ---- the lock table -------------------------------------------------------------------------------

def test_state_change_needs_a_session(bench):
    _, ep = bench
    h = new_host(ep, 1)
    with pytest.raises(host.Rejected, match="session required"):
        h.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5), locked=False)


def test_unknown_session_on_a_free_lock_is_told_to_open(bench):
    _, ep = bench
    h = new_host(ep, 1)
    h.session = 0x12345678
    with pytest.raises(host.NoSession):
        write(h, 1)


def test_locked_says_how_long_and_not_whose(bench):
    clock, ep = bench
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open(lease_ms=1000)
    clock.ms = 400
    b.session = 0x0BAD0BAD
    with pytest.raises(host.Locked) as e:
        write(b, 9)
    assert e.value.remaining_ms == 600
    assert struct.pack("<I", a.session) not in e.value.result.payload
    with pytest.raises(host.Locked):
        b.open()


def test_reads_need_no_lock_while_another_host_holds_it(bench):
    _, ep = bench
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open()
    write(a, 42)
    assert read(b) == 42                       # lock-free read, no session
    assert b.lock_state()[0] is True
    assert b.request(0, m.OP_LIST, struct.pack("<H", 0), locked=False).succeeded   # list: first(u16) (core §7.2)


def test_watchdog_counts_from_the_last_request(bench):
    clock, ep = bench
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open(lease_ms=1000)
    for t in (900, 1800, 2700):               # each request pushes the lapse 1000 ms further
        clock.ms = t
        write(a, t)
    clock.ms = 3600
    with pytest.raises(host.Locked):
        b.open()
    clock.ms = 3700                            # 1000 ms after the last request
    b.open()


def test_a_lapsed_lock_is_no_session_and_the_host_opens_anew(bench):
    """core §6.2 / §9: after the lease lapsed the probe released everything the session created; a request of the id
    is rejected no_session (no expired, no resume) and the host counts the loss and is out of the session."""
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=1000)
    write(a, 1)
    epoch, sid = a.epoch, a.session
    clock.ms = 5000                            # lapsed
    with pytest.raises(host.NoSession):
        write(a, 2)
    assert a.epoch == epoch + 1 and a.session is None and ep.holder is None
    with pytest.raises(host.Rejected, match="session required"):
        write(a, 2)                            # nothing re-opens by itself
    opened = a.open()
    assert opened.lease_ms == 1000 and a.session not in (None, sid)   # a new session under a new id
    write(a, 3)
    assert ep.holder == a.session and read(a) == 3


def test_end_releases_the_session_and_its_id_is_then_no_session(bench):
    """core §6.4, §9: end releases the lock and everything the session created; nothing resumes it, and a resent end
    is answered from the resend table (core §5.2)."""
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=2000)
    sid, epoch = a.session, a.epoch
    a.end()
    assert a.session is None and a.epoch == epoch + 1 and ep.holder is None
    end = m.Request(a._corr, 0, m.OP_END, b"", sid).pack()
    assert m.Result.unpack(ep.handle(end)).succeeded                  # the same end again: from the table
    a.session = sid                                                    # the old id, as a stale caller would
    with pytest.raises(host.NoSession):
        write(a, 1)
    clock.ms = 100
    assert ep.holder is None                                           # no lock came back


def test_open_has_no_session_argument_any_more():
    import inspect
    assert "session" not in inspect.signature(host.Host.open).parameters      # no resume by id (core §6.4)
    assert set(host.Opened.__dataclass_fields__) == {"lease_ms", "boot_id"}


def test_force_takes_the_lock_and_the_old_holder_is_refused(bench):
    _, ep = bench
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open()
    b.open(force=True)
    with pytest.raises(host.Locked):
        write(a, 1)
    write(b, 1)
    forcing = b.session
    b.end()
    with pytest.raises(host.NoSession):        # the lock is free: a's id is no session (core §6.2)
        write(a, 1)
    assert ep.last == forcing


def test_a_probe_reboot_forgets_the_last_id_and_boot_id_says_so(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    first = a.open()
    assert first.boot_id == ep.boot_id == a.confirmed()["boot_id"]   # confirm tells it too (core §7.1)
    rebooted = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), clock, boot_id=0x5555AAAA)
    a.send = rebooted.handle
    with pytest.raises(host.NoSession):
        write(a, 1)
    again = a.open()
    assert again.boot_id != first.boot_id


def test_subscriptions_survive_a_same_id_open_while_held_and_go_with_the_lock(bench):
    """core §6.2 / §11.3: subscribe is the emitting interface's own op (0x30, no target fn); an open of the id that
    holds the lock keeps the subscriptions; an fn that sends nothing (the toy, fn 0) has no subscribe -
    unknown_operation; unsubscribing nothing is ok; the subscriptions go with the lock."""
    clock, ep = bench
    logic = 6                                                                       # esp32-v003's oep.fixture.logic
    a = new_host(ep, 1)
    a.open(lease_ms=2000)
    a.subscribe(logic, max_delay_ms=500)
    assert ep.subscribed == {logic: (0, 500)} and a.subscriptions == {logic}
    assert ep.requests[-1].fn == logic and ep.requests[-1].op == m.OP_SUBSCRIBE
    assert ep.requests[-1].payload == struct.pack("<HI", 0, 500)                    # max_delay_ms is u32
    resent = m.Request(a.next_corr(), 0, m.OP_OPEN, struct.pack("<IB", 2000, 0), a.session).pack()
    assert m.Result.unpack(ep.handle(resent)).succeeded                            # the holder's open again: kept
    assert ep.subscribed == {logic: (0, 500)} and a.subscriptions == {logic}
    clock.ms = 500
    assert ep.pushes() == []                                                        # fn 0 sends nothing (no heartbeat)
    for fn in (TOY, 0, 4):                                                          # the toy, the core, gpio
        with pytest.raises(host.Rejected) as e:
            a.subscribe(fn)
        assert type(e.value) is host.Rejected and e.value.result.detail == m.UNKNOWN_OPERATION
    with pytest.raises(host.Rejected, match="unknown function"):
        a.subscribe(99)
    a.unsubscribe(logic)
    a.unsubscribe(logic)                                                            # nothing subscribed: ok
    assert ep.requests[-1].payload == b"" and ep.subscribed == {}
    a.subscribe(logic)
    a.end()
    assert ep.subscribed == {} and a.subscriptions == set()


def test_pipeline_keeps_order_and_reports_rejects_per_result(bench):
    _, ep = bench
    a = new_host(ep, 1)
    a.open()
    reqs = [(TOY, endpoint.TOY_WRITE, struct.pack("<I", v)) for v in (1, 2, 3)] + [(TOY, endpoint.TOY_READ, b"")]
    results = a.pipeline(reqs, lambda msgs: [ep.handle(x) for x in msgs])
    assert [r.succeeded for r in results] == [True] * 4
    assert struct.unpack("<I", results[-1].payload)[0] == 3
    a.session = 0x0BAD0BAD                        # a stale id: every result says so, nothing is raised
    results = a.pipeline(reqs[:2], lambda msgs: [ep.handle(x) for x in msgs])
    assert [r.detail for r in results] == [m.LOCKED, m.LOCKED]


# ---- confirm and the v1 gate (oep-core §7.1, §4.1) ---------------------------------------------------------------

def test_confirm_sends_a_range_and_reads_the_v1_answer(bench):
    _, ep = bench
    a = new_host(ep, 1)
    limits = a.confirm()
    assert ep.requests[-1].payload == b"OEP?\x01\x01"
    assert limits["revision"] == 1 and limits["flags"] == 0 and limits["max_frame"] == 512
    assert limits["window"] == 1 << 18 and limits["max_inflight"] == 4            # window is u32 now
    with pytest.raises(host.Unsupported):
        a.confirm(2, 3)                                                             # nothing in the range


def test_no_session_request_to_a_v0_probe(bench):
    clock, _ = bench
    ep = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), clock, revision=0)
    a = new_host(ep, 1)
    with pytest.raises(host.NotV1):
        a.open()
    assert a.revision == 0 and a.limits["window"] == 0xFFFF                        # read in the v0 shape
    a.session = 0x1234                                                              # a saved id, say
    with pytest.raises(host.NotV1):
        write(a, 1)
    assert read(a) == 0                                                             # session_id 0 still goes
    assert ep.dropped == 0 and all(r.session == 0 for r in ep.requests)


def test_a_probe_that_refuses_the_ranged_confirm_is_not_v1():
    def send(raw):
        req = m.Request.unpack(raw)
        return m.Result(req.corr, m.REJECTED, m.MALFORMED).pack()
    a = host.Host(send)
    with pytest.raises(host.NotV1):
        a.open()
    assert a.revision == 0


def test_the_first_session_request_confirms_first(bench):
    _, ep = bench
    a = new_host(ep, 1)
    a.open()
    assert [(r.fn, r.op) for r in ep.requests[:2]] == [(0, m.OP_CONFIRM), (0, m.OP_OPEN)]


# ---- §0 tails ---------------------------------------------------------------------------------------------

def test_the_host_skips_tlvs_it_does_not_know_after_every_result(bench):
    clock, _ = bench
    ep = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), clock, tail=m.tlv(0x6D, b"new!") + m.tlv(0x6E, b""))
    a = new_host(ep, 1)
    assert a.confirm()["tail"].get(0x6D) == b"new!"
    opened = a.open(lease_ms=1000)
    assert opened.lease_ms == 1000
    write(a, 9)
    assert read(a) == 9 and a.lock_state() == (True, 1000)
    from oep_client import capture, config, console, core, fixture, riscv
    wire = riscv.Wire(a, "oep.wire.swio")
    conn, _ = wire.attach()
    dm = riscv.RiscvDm(a, conn)
    dm.halt()
    assert dm.dmi([dm.step_read(0x11)])[0] == 1                 # dmi: done status nvals values [TLV]
    dm.write_block(0x20000000, bytes(8))
    assert dm.read_block(0x20000000, 2) == bytes(8)              # done status done x word [TLV]
    assert dm.run(0x20000000, []).stopped                        # ... nvals values [TLV]
    assert wire.connections()[0].connection == conn
    con = console.Console(a)
    con.open(conn)
    ep.emit(con.stream, b"hi")
    assert con.read().data == b"hi" and con.marks()[0].kind == 3 and con.streams()[0].stream == con.stream
    assert con.write(b"x") == 1
    gpio = fixture.Gpio(a, core.find(a, "oep.fixture.gpio"))
    core.plan_apply(a, [(gpio.fn, 1, 4), (5, 1, 21)])
    assert gpio.read([4]) == [0]                                 # n(u8) n x level [TLV]
    uart = fixture.FixtureUart(a, 5)
    assert uart.configure(9600) == 9600 and uart.status().baud == 9600 and uart.read().data == b""
    lc = capture.LogicCapture(a)
    core.plan_apply(a, [(lc.fn, 0, 5)])
    lc.configure(rate=1_000_000, samples=64)
    lc.start()
    (seg,) = lc.wait()
    assert len(lc.read_segment(seg)) == 64 and lc.status().state == capture.STATE["done"]
    cfg = config.ProbeConfig(a)
    assert cfg.set([config.Label(channel=4, text="x")]) == cfg.get()[0] and cfg.state().storage == "none"
    assert cfg.describe().slots_max == 1


def test_request_tails_critical_refused_non_critical_ignored(bench):
    """core §2.3: an unknown non-critical TLV is ignored silently (the answer is the one without it); an unknown
    critical one is rejected unsupported with the tag as received; 0x00 / 0x7F are never tags (so unknown ones)."""
    _, ep = bench
    a = new_host(ep, 1)
    a.open()
    plain = a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 4))
    r = a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + m.tlv(0x21, b"\x01") + m.tlv(0x22, b""))
    assert r.succeeded and r.payload == plain.payload and read(a) == 5
    with pytest.raises(host.Unsupported) as e:
        a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 6) + m.tlv(0x21, b"\x01", critical=True))
    assert e.value.tag == 0xA1
    assert read(a) == 5                                                  # refused: nothing written
    with pytest.raises(host.Unsupported) as e:
        a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 7) + bytes([0xFF, 0, 0]))   # 0x7F critical: unknown
    assert e.value.tag == 0xFF and read(a) == 5
    assert a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 7) + bytes([0x7F, 0, 0])).payload == plain.payload
    with pytest.raises(host.Rejected, match="malformed"):
        a.request(TOY, endpoint.TOY_WRITE, b"\x01\x02")                  # shorter than the fixed part
    with pytest.raises(ValueError):
        m.tlv(0x7F, b"")                                                 # 0x7F is never a TLV tag (core §2.2)


def test_tlv_one_form_round_trip():
    """core §2.2: every TLV is tag(u8) len(u16) value, whatever the value's length."""
    big = bytes(range(256)) * 2
    t = m.tlv(0x41, big)
    assert t[:3] == bytes([0x41, 0x00, 0x02]) and m.split_tlvs(t + m.tlv(0x42, b"x")) == [(0x41, big), (0x42, b"x")]
    assert m.tlv(0x41, bytes(254))[1:3] == bytes([254, 0]) and len(m.tlv(0x41, bytes(255))) == 255 + 3
    assert m.tlv(0x41, b"") == bytes([0x41, 0, 0])
    with pytest.raises(m.ShortPayload):
        m.split_tlvs(bytes([0x41, 0x00, 0x02]) + bytes(100))           # len past the end
    with pytest.raises(m.ShortPayload):
        m.split_tlvs(bytes([0x41, 0x01]))                               # the header cut short
    ep = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), Clock())
    a = new_host(ep, 1)
    a.open()
    plain = a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 4))
    r = a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + m.tlv(0x21, big))   # a long non-critical TLV: ignored
    assert r.payload == plain.payload
    with pytest.raises(host.Rejected, match="malformed"):
        a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + bytes([0x21, 5, 0, 9]))  # len past the request's end
    assert a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + bytes([0x00, 0, 0])).payload == plain.payload
    # tag 0x00 is never a TLV tag: an unknown one, ignored when not critical (core §2.2, §2.3)


def test_a_result_shorter_than_its_fixed_part_is_broken():
    def send(raw):
        req = m.Request.unpack(raw)
        return m.Result(req.corr, m.COMPLETED, m.SUCCESS, b"\x01\x00").pack()     # lock_state needs 5 bytes
    with pytest.raises(host.ProtocolError):
        host.Host(send).lock_state()
    with pytest.raises(host.ProtocolError):
        m.Tail.parse(bytes([0x40, 5, 0, 1]))                             # a truncated TLV


def test_serial_arithmetic_on_wrapping_values():
    assert m.serial_diff(2, 0xFFFFFFFE) == 4 and m.serial_diff(0xFFFFFFFE, 2) == -4
    assert m.serial_diff(1, 0xFFFF, bits=16) == 2


# ---- §3: connections and the plan lost ----------------------------------------------------------------------

def test_no_session_means_this_sessions_resources_are_gone(bench):
    """core §9: no_session = no session holds the lock - this one ended (lapse, a force and the forcing session's
    end) or the probe rebooted (boot_id 0 is an ordinary value now): either way nothing of this session is left."""
    clock, _ = bench
    ep = endpoint.Endpoint(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), clock, boot_id=0)
    a = new_host(ep, 1)
    a.open()
    epoch = a.epoch
    ep.reboot(0)
    with pytest.raises(host.NoSession):
        write(a, 1)
    assert a.epoch == epoch + 1


def test_a_changed_boot_id_in_a_heartbeat_means_everything_is_lost(bench):
    _, ep = bench
    a = new_host(ep, 1)
    a.open()
    a.boot_id_seen(ep.boot_id)
    assert a.epoch == 0
    a.boot_id_seen(0x77777777)
    assert a.epoch == 1
