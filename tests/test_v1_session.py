"""Draft v1 session rules end to end: messages, the lock table, watchdog, resume, force, long operations."""

import struct

import pytest

from oep_client.v1 import endpoint, fake, host, message as m

TOY = 7          # a fn the fake does not simulate (esp32_v003: the i2c-target) has the stand-in operations


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


@pytest.fixture
def bench():
    clock = Clock()
    ep = endpoint.Endpoint(fake.esp32_v003(), clock, lease_default_ms=1000)
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
    from oep_client.v1 import catalog
    result = ep.handle(m.Request(1, 0, m.OP_LIST, catalog.pack_list_request(name, True, 0)).pack())
    assert len(result) == 63 and catalog.unpack_list_result(result[5:])[1][0].name == name   # v1 wire §6


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


def test_a_lapsed_lock_resumes_with_the_same_id_and_proves_nobody_came(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=1000)
    write(a, 1)
    clock.ms = 5000                            # lapsed; the last id is remembered
    write(a, 2)                                # same id on a free lock: re-locked, goes through
    assert ep.holder == a.session


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


def test_a_probe_reboot_forgets_the_last_id_and_boot_id_says_so(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    first = a.open()
    rebooted = endpoint.Endpoint(fake.esp32_v003(), clock, boot_id=0x5555AAAA)
    a.send = rebooted.handle
    with pytest.raises(host.NoSession):
        write(a, 1)
    again = a.open(session=a.session)
    assert again.boot_id != first.boot_id and not again.resumed


# ---- long operations ------------------------------------------------------------------------------

def start_long(h, ms):
    r = h.request(TOY, endpoint.TOY_LONG, struct.pack("<I", ms))
    assert r.resolution == m.ACCEPTED
    return struct.unpack_from("<H", r.payload)[0]


def test_long_operation_is_polled_with_progress(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=10000)
    write(a, 77)
    ref = start_long(a, 300)

    def tick():
        clock.ms += 100
    final, progress = a.wait(ref, tick)
    assert progress == [(0, 300), (100, 300), (200, 300)]
    assert final.succeeded and struct.unpack("<I", final.payload)[0] == 77


def test_conflicting_requests_are_busy_and_reads_are_not(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=10000)
    start_long(a, 1000)
    with pytest.raises(host.Busy):
        write(a, 1)
    assert read(a) == 0
    a.keepalive()                               # core session ops still go through


def test_the_operation_outlives_its_host_and_its_result_waits_for_the_same_id(bench):
    clock, ep = bench
    a, b = new_host(ep, 1), new_host(ep, 2)
    a.open(lease_ms=500)
    write(a, 5)
    ref = start_long(a, 2000)
    clock.ms = 10000                            # the host vanished; the lock lapsed long ago
    saved = a.session
    back = new_host(ep, 3)
    back.open(session=saved)                    # same id: the result is still there
    assert back.status(ref).succeeded
    back.end()
    b.open()                                    # a new id takes the lock: the last result goes
    with pytest.raises(host.Rejected, match="unavailable"):
        b.status(ref)


def test_cancel(bench):
    clock, ep = bench
    a = new_host(ep, 1)
    a.open(lease_ms=10000)
    ref = start_long(a, 1000)
    a.cancel(ref)
    r = a.status(ref)
    assert r.resolution == m.COMPLETED and r.detail == m.FAILED
    write(a, 3)                                 # no longer busy


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


# ---- confirm and the role 0x81 gate (v1 wire §5, §2) -----------------------------------------------------

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
    ep = endpoint.Endpoint(fake.esp32_v003(), clock, revision=0)
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
    ep = endpoint.Endpoint(fake.esp32_v003(), clock, tail=m.tlv(0x55, b"new!") + m.tlv(0x56, b""))
    a = new_host(ep, 1)
    assert a.confirm()["tail"].get(0x55) == b"new!"
    opened = a.open(lease_ms=1000)
    assert opened.lease_ms == 1000 and not opened.resumed
    write(a, 9)
    assert read(a) == 9 and a.lock_state() == (True, 1000)
    from oep_client.v1 import riscv
    wire = riscv.Wire(a, "oep.wire.swio")
    conn, _ = wire.attach()
    riscv.RiscvDm(a, conn).halt()


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

def test_no_session_from_a_probe_without_a_boot_id_means_everything_is_lost(bench):
    clock, _ = bench
    ep = endpoint.Endpoint(fake.esp32_v003(), clock, boot_id=0)
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
