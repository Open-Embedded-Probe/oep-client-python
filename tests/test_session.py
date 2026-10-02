"""Draft v1 session rules end to end: messages, the lock table, watchdog, resume, force, long operations."""

import struct

import pytest

from oep_client import endpoint, fake, host, message as m

TOY = 10         # a fn the fake does not simulate (fake.with_stand_in(esp32_v003)) has the stand-in operations


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


@pytest.fixture
def bench():
    clock = Clock()
    ep = endpoint.Endpoint(fake.with_stand_in(fake.esp32_v003()), clock, lease_default_ms=1000)
    return clock, ep


def new_host(ep, seed):
    import random
    return host.Host(ep.handle, rng=random.Random(seed))


def write(h, value):
    return h.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", value))


def read(h):
    return struct.unpack_from("<I", h.request(TOY, endpoint.TOY_READ, locked=False).payload)[0]


# ---- messages -------------------------------------------------------------------------------------

def test_session_flag_is_role_bit7_and_adds_four_bytes():
    plain = m.Request(7, 3, 0x01, b"\xaa").pack()
    flagged = m.Request(7, 3, 0x01, b"\xaa", session=0xDEADBEEF).pack()
    assert plain[0] == 0x01 and flagged[0] == 0x81
    assert len(flagged) == len(plain) + 4
    assert m.Request.unpack(flagged).session == 0xDEADBEEF
    assert m.Request.unpack(plain).session is None


def test_a_48_byte_name_fits_a_64_byte_frame():
    name = "io.github.ch32-riscv-ug." + "x" * 24
    assert len(name) == 48
    probe = fake.FakeProbe("tiny", 64, [fake.Offered(0, 0, "oep.core"), fake.Offered(1, 1, name)])
    ep = endpoint.Endpoint(probe, Clock())
    from oep_client import catalog
    result = ep.handle(m.Request(1, 0, m.OP_LIST, catalog.pack_list_request(name, True, 0)).pack())
    assert len(result) == 64 and catalog.unpack_list_result(result[5:])[1][0].name == name   # oep-v1 frame budget (v1-core-wire-delta §6)


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
    assert b.request(0, m.OP_LIST, b"\x00\x00\x00\x00", locked=False).succeeded


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


def test_a_lapsed_lock_is_expired_and_open_says_the_resources_went(bench):
    """core §6.2 / §9: after the lease lapsed the same id's request is rejected expired (never resumed silently: the
    probe swept its plan and connections); its open answers resumed 2 and the host counts the loss."""
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=1000)
    write(a, 1)
    epoch = a.epoch
    clock.ms = 5000                            # lapsed; the last id is remembered as swept
    with pytest.raises(host.Expired) as e:
        write(a, 2)
    assert e.value.lease_ms == 1000 and "1000 ms" in str(e.value) and a.epoch == epoch + 1
    assert ep.holder is None
    with pytest.raises(host.Expired):
        write(a, 2)                            # still: nothing re-opens by itself
    opened = a.open(session=a.session)
    assert opened.resumed == 2 and opened.swept
    write(a, 3)
    assert ep.holder == a.session and read(a) == 3


def test_end_then_resume_keeps_the_lease_and_a_lapse_after_it_expires(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=2000)
    a.end()
    clock.ms = 100
    write(a, 1)                                # resumed after end: the lock is back with the lease of the open
    assert ep.holder == a.session and ep.expires_ms == 2100
    clock.ms = 5000
    with pytest.raises(host.Expired):
        write(a, 2)


def test_end_then_resume_is_allowed_until_someone_else_opens(bench):
    _, ep = bench
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open()
    saved = a.session
    a.end()
    one_shot = new_host(ep, 3)
    assert one_shot.open(session=saved).resumed      # a one-shot CLI resuming its saved id
    one_shot.end()
    b.open()
    b.end()
    with pytest.raises(host.NoSession):              # someone came in between: the saved id no longer works
        write(one_shot, 7)


def test_force_takes_the_lock_and_the_old_holder_is_refused(bench):
    _, ep = bench
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open()
    b.open(force=True)
    with pytest.raises(host.Locked):
        write(a, 1)
    write(b, 1)
    b.end()
    with pytest.raises(host.NoSession):        # the forcing session is the last one now: a's id is not it (never expired)
        write(a, 1)
    assert ep.last == b.session and not ep.last_swept


def test_a_probe_reboot_forgets_the_last_id_and_boot_id_says_so(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    first = a.open()
    assert first.boot_id == ep.boot_id == a.confirmed()["boot_id"]   # confirm tells it too (core §7.1)
    rebooted = endpoint.Endpoint(fake.with_stand_in(fake.esp32_v003()), clock, boot_id=0x5555AAAA)
    a.send = rebooted.handle
    with pytest.raises(host.NoSession):
        write(a, 1)
    again = a.open(session=a.session)
    assert again.boot_id != first.boot_id and not again.resumed


def test_subscriptions_survive_a_same_id_open_while_held_and_go_with_the_lock(bench):
    """core §6.2 / §11.3: an open of the id that holds the lock keeps the subscriptions; fn 0 is always subscribable
    (its heartbeat is boot_id uptime_ns); an fn that emits nothing is unsupported; unsubscribing nothing is ok."""
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=2000)
    a.subscribe(0, max_delay_ms=500)
    assert ep.subscribed == {0} and a.subscriptions == {0}
    a.open(session=a.session).resumed == 1
    assert ep.subscribed == {0} and a.subscriptions == {0}
    clock.ms = 500
    (hb,) = ep.pushes()
    assert hb[0] == m.ROLE_EVENT and hb[5] == 1 and hb[6:] == struct.pack("<IQ", ep.boot_id, 500_000_000)
    with pytest.raises(host.Unsupported):
        a.subscribe(TOY)                                                            # the toy fn emits nothing
    with pytest.raises(host.Rejected, match="unknown function"):
        a.subscribe(99)
    a.unsubscribe(5)                                                                # nothing subscribed: ok
    assert ep.requests[-3].payload == struct.pack("<HHI", TOY, 0, 0)                # max_delay_ms is u32
    a.end()
    assert ep.subscribed == set() and a.subscriptions == set()


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


# ---- confirm and the role 0x81 gate (oep-core §7.1, §4.1) -----------------------------------------------------

def test_confirm_sends_a_range_and_reads_the_v1_answer(bench):
    _, ep = bench
    a = new_host(ep, 1)
    limits = a.confirm()
    assert ep.requests[-1].payload == b"OEP?\x01\x01"
    assert limits["revision"] == 1 and limits["flags"] == 0 and limits["max_frame"] == 64
    assert limits["window"] == 1 << 18 and limits["max_inflight"] == 4            # window is u32 now
    with pytest.raises(host.Unsupported):
        a.confirm(2, 3)                                                             # nothing in the range


def test_no_role_0x81_to_a_v0_probe(bench):
    clock, _ = bench
    ep = endpoint.Endpoint(fake.with_stand_in(fake.esp32_v003()), clock, revision=0)
    a = new_host(ep, 1)
    with pytest.raises(host.NotV1):
        a.open()
    assert a.revision == 0 and a.limits["window"] == 0xFFFF                        # read in the v0 shape
    a.session = 0x1234                                                              # a saved id, say
    with pytest.raises(host.NotV1):
        write(a, 1)
    assert read(a) == 0                                                             # role 0x01 still goes
    assert ep.dropped == 0 and all(r.session is None for r in ep.requests)


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
    ep = endpoint.Endpoint(fake.with_stand_in(fake.esp32_v003()), clock, tail=m.tlv(0x6D, b"new!") + m.tlv(0x6E, b""))
    a = new_host(ep, 1)
    assert a.confirm()["tail"].get(0x6D) == b"new!"
    opened = a.open(lease_ms=1000)
    assert opened.lease_ms == 1000 and not opened.resumed
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
    _, ep = bench
    a = new_host(ep, 1)
    a.open()
    r = a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + m.tlv(0x21, b"\x01") + m.tlv(0x22, b""))
    assert r.succeeded and m.Reader(r.payload).tail().ignored == [0x21, 0x22]
    with pytest.raises(host.Unsupported) as e:
        a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 6) + m.tlv(0x21, b"\x01", critical=True))
    assert e.value.tag == 0xA1
    assert read(a) == 5                                                  # refused: nothing written
    with pytest.raises(host.Rejected, match="malformed"):
        a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 7) + bytes([0xFF, 0]))    # tag 0xFF is invalid
    with pytest.raises(host.Rejected, match="malformed"):
        a.request(TOY, endpoint.TOY_WRITE, b"\x01\x02")                  # shorter than the fixed part
    with pytest.raises(ValueError):
        m.tlv(0x7F, b"")                                                 # 0x7F is the ignored list


def test_tlv_long_form_round_trip_and_the_one_encoding():
    """core §2.2: a value of 255 bytes or more goes as tag 0xFF len(u16) value; under 255 the short form - the long
    form with a short value is malformed (BadTlv here, rejected malformed by the probe)."""
    big = bytes(range(256)) * 2
    t = m.tlv(0x41, big)
    assert t[:4] == bytes([0x41, 0xFF, 0x00, 0x02]) and m.split_tlvs(t + m.tlv(0x42, b"x")) == [(0x41, big), (0x42, b"x")]
    assert m.tlv(0x41, bytes(254))[1] == 254 and len(m.tlv(0x41, bytes(255))) == 255 + 4
    with pytest.raises(m.BadTlv):
        m.split_tlvs(bytes([0x41, 0xFF, 3, 0]) + b"abc")
    with pytest.raises(m.ShortPayload):
        m.split_tlvs(bytes([0x41, 0xFF, 0x00, 0x02]) + bytes(100))
    ep = endpoint.Endpoint(fake.with_stand_in(fake.esp32_v003()), Clock())
    a = new_host(ep, 1)
    a.open()
    r = a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + m.tlv(0x21, big))   # a long non-critical TLV: ignored
    assert m.Reader(r.payload).tail().ignored == [0x21]
    with pytest.raises(host.Rejected, match="malformed"):
        a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + bytes([0x21, 0xFF, 1, 0, 9]))
    with pytest.raises(host.Rejected, match="malformed"):
        a.request(TOY, endpoint.TOY_WRITE, struct.pack("<I", 5) + bytes([0x00, 0]))        # tag 0x00 is reserved


def test_a_result_shorter_than_its_fixed_part_is_broken():
    def send(raw):
        req = m.Request.unpack(raw)
        return m.Result(req.corr, m.COMPLETED, m.SUCCESS, b"\x01\x00").pack()     # lock_state needs 5 bytes
    with pytest.raises(host.ProtocolError):
        host.Host(send).lock_state()
    with pytest.raises(host.ProtocolError):
        m.Tail.parse(bytes([0x40, 5, 1]))                                # a truncated TLV


def test_serial_arithmetic_on_wrapping_values():
    assert m.serial_diff(2, 0xFFFFFFFE) == 4 and m.serial_diff(0xFFFFFFFE, 2) == -4
    assert m.serial_diff(1, 0xFFFF, bits=16) == 2


# ---- §3: connections and the plan lost ----------------------------------------------------------------------

def test_no_session_means_this_sessions_resources_are_gone(bench):
    """core §9: no_session = another session came in between (and took the resources over), or the probe rebooted
    (boot_id 0 is an ordinary value now): either way nothing of this session is left."""
    clock, _ = bench
    ep = endpoint.Endpoint(fake.with_stand_in(fake.esp32_v003()), clock, boot_id=0)
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
