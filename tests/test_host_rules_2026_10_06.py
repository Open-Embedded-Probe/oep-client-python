"""The host side of the rules added since the 2026-10-02 rule changes: docs/v1-rule-change-proposal-2026-10-06.md
(oep-spec b4b08f1, 40291a4) and 2e70f40 / 73a0c37 - confirm's bounds (C-20), max_op_ms's ceiling (C-47), the transfer
time before the first confirm answer (N-1), resumed = 0 for the last session_id (C-19), clock's boot_id and times, short
answers and wrong-direction roles (C-36), an unanswered resend fails the transport (C-38), the last page's storage
(PC-9), the optional ops (C-21), cs_setup_ns and i2c-target's reserved addresses."""

import struct
import time

import pytest

from oep_client import catalog, cobs, config, core, endpoint, fake, fixture, host as h, link, message as m
from oep_client import registry as reg, riscv

from test_fake_rules_2026_10_02 import Clock
from test_link_host import CONFIRM_V1, Stream, answering, frame, make_link, result
from test_link_serial import Scripted


def confirm_payload(max_frame=1024, window=65536, inflight=4, boot_id=0x11):
    return struct.pack("<4sBBHIBI", b"OEP!", 1, 0, max_frame, window, inflight, boot_id)


def in_process(probe=None, transport=0, **kw):
    ep = endpoint.Endpoint(probe or fake.p4_bench(), Clock(), **kw)
    sent = []

    def send(b):
        sent.append(b)
        return ep.handle(b, transport)
    return ep, h.Host(send), sent


# ---- C-20: confirm's bounds ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("max_frame, window, inflight", [(63, 4096, 4), (1024, 1000, 4), (1024, 4096, 0)])
def test_c20_a_confirm_outside_the_bounds_makes_the_transport_unusable(max_frame, window, inflight):
    sent = []

    def send(b):
        sent.append(b)
        return result(m.Request.unpack(b).corr, confirm_payload(max_frame, window, inflight))
    hst = h.Host(send)
    with pytest.raises(h.NotUsable) as e:
        hst.confirm()
    assert f"max_frame {max_frame}, window {window}, max_inflight {inflight}" in str(e.value)   # the values reported
    with pytest.raises(h.NotUsable):
        hst.request(0, m.OP_LIST, catalog.pack_list_request(), locked=False)
    assert len(sent) == 1                                                      # nothing more went out


def test_c20_on_a_link_the_probe_is_closed_and_nothing_more_is_sent():
    s = Stream(lambda msg: [result(m.Request.unpack(msg).corr, confirm_payload(1024, 512, 4))])
    lk = make_link(s)
    hst = h.Host(lk.send)
    with pytest.raises(link.NotOepProbe):
        lk.attach_host(hst)
    assert len(s.writes) == 1


# ---- C-47: max_op_ms 1..600000 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, usable", [(0, False), (1, True), (600_000, True), (600_001, False),
                                           (0xFFFFFFFF, False)])
def test_c47_a_max_op_ms_outside_1_to_600000_is_a_probe_not_used(value, usable):
    ep, hst, sent = in_process()
    ep.static[0] = tuple(catalog.u32(fake.CORE_MAX_OP_MS, value) if t[0] == fake.CORE_MAX_OP_MS else t
                         for t in ep.static[0])
    hst.confirm()
    if usable:
        assert core.max_op_ms(hst) == value
        return
    with pytest.raises(h.NotUsable, match=str(value)):
        core.max_op_ms(hst)
    n = len(sent)
    with pytest.raises(h.NotUsable):
        hst.open()
    assert len(sent) == n


# ---- N-1: min_max_frame until the first confirm answer, then the latest -------------------------------------------

def test_n1_the_transfer_time_counts_64_until_a_confirm_answer_then_the_latest():
    answers = [confirm_payload(1024)]

    def respond(msg):
        req = m.Request.unpack(msg)
        return [result(req.corr, answers[0] if req.op == m.OP_CONFIRM else b"")]
    lk = make_link(Stream(respond))
    assert lk.max_frame == reg.MIN_MAX_FRAME == 64
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    assert lk.max_frame == 1024
    answers[0] = confirm_payload(512, 4096)
    hst.confirm()
    assert lk.max_frame == 512 and lk.frames.max_frame == 512


# ---- C-19 under no resume (core §6.5, §9): the boot_id tells a reboot, no_session a session that ended ---------------

def test_c19_after_a_reboot_with_a_repeated_boot_id_the_session_is_no_session_and_open_starts_anew():
    """core §6.5: open's boot_id is the host's sign of a reboot; a probe whose only source repeated it gives none, and
    the host learns its session ended from no_session (§6.2) - the resources are gone (epoch), the names stay (only a
    changed boot_id drops them)."""
    ep, hst, _ = in_process()
    hst.open(3000)
    gpio = core.find(hst, "oep.fixture.gpio")
    epoch = hst.epoch
    ep.reboot(ep.boot_id)                                                     # its only source repeated the boot_id
    with pytest.raises(h.NoSession):
        hst.keepalive()
    assert hst.epoch == epoch + 1 and hst.session is None and hst._fns
    assert hst.open(3000).boot_id == ep.boot_id and hst.epoch == epoch + 1
    assert core.find(hst, "oep.fixture.gpio") == gpio
    ep.reboot()                                                               # a new boot_id: names listed again
    hst.confirm()
    assert hst._fns == {} and hst._describes == {} and hst.epoch == epoch + 2


def test_c19_end_then_a_new_open_counts_the_loss_once_and_keeps_the_names():
    ep, hst, _ = in_process()
    hst.open(3000)
    core.find(hst, "oep.fixture.gpio")
    epoch = hst.epoch
    hst.end()                                                                 # the probe released everything (§9)
    assert hst.epoch == epoch + 1 and hst.session is None
    hst.open(3000)
    assert hst.epoch == epoch + 1 and hst._fns


# ---- clock: the probe's time and its boot_id (core §6.5, §7.7) ---------------------------------------------------

def test_clock_reads_the_probes_time_between_the_hosts_send_and_receive_and_watches_the_boot_id():
    boot = [0x11]
    ticks = iter(range(1000, 10**9, 1000))

    def respond(msg):
        req = m.Request.unpack(msg)
        if req.op == m.OP_CONFIRM:
            return [result(req.corr, CONFIRM_V1)]
        assert (req.fn, req.op, req.session, req.payload) == (0, m.OP_CLOCK, 0, b"")   # lock-free, session-free
        return [result(req.corr, struct.pack("<IQ", boot[0], 5_000_000_000))]
    lk = make_link(Stream(respond))
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    hst.session = 0x1234                                                      # a session open: clock still goes with 0
    r = hst.clock(now=lambda: next(ticks))
    assert (r.before_ns, r.after_ns, r.uptime_ns, r.boot_id, r.round_trip_ns) == (1000, 2000, 5_000_000_000, 0x11, 1000)
    assert (r.host_ns, r.uncertainty_ns) == (1500, 500) and hst.epoch == 0
    hst._fns["oep.fixture.gpio"] = 4
    boot[0] = 0x22                                                            # the probe rebooted
    assert hst.clock().boot_id == 0x22
    assert hst.epoch == 1 and hst._fns == {}


def test_clock_best_keeps_the_shortest_round_trip():
    trips = iter([5000, 900, 3000, 1200])
    t = [0]

    def now():
        t[0] += 1
        return t[0]
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())

    def send(b):
        t[0] += next(trips) - 1                                               # the round trip of this one
        return ep.handle(b, 1)
    hst = h.Host(send)
    best = hst.clock_best(4, now=now)
    assert best.round_trip_ns == 900 and best.boot_id == ep.boot_id
    with pytest.raises(ValueError):
        hst.clock_best(0)


def test_clock_from_the_fake_through_a_link_and_no_heartbeat_any_more():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    s = Stream(lambda msg: [r for r in [ep.handle(msg, 1)] if r] + ep.pushes())
    lk = make_link(s)
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    ep.now.t = 1000
    r = hst.clock()
    assert (r.boot_id, r.uptime_ns) == (ep.boot_id, 1_000_000_000) and r.before_ns <= r.after_ns
    assert not hasattr(lk, "heartbeats") and "heartbeat_default_ms" not in reg.TIMING


# ---- C-36: short answers, events and data; request roles ----------------------------------------------------------

def test_c36_a_short_answer_is_a_broken_frame_on_length_links_resynced_and_resent():
    calls = []

    def respond(msg):
        req = m.Request.unpack(msg)
        calls.append(req.op)
        if req.op == m.OP_CONFIRM:
            return [result(req.corr, CONFIRM_V1)]
        if calls.count(m.OP_KEEPALIVE) == 1:
            return [result(req.corr)[:4]]                                     # 4 bytes, its corr readable
        return [result(req.corr)]
    lk = make_link(Stream(respond))
    lk.wait_add_s = 0.0
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    assert hst.request(0, m.OP_KEEPALIVE, locked=False).succeeded
    assert lk.resyncs == 1 and lk.retries == 1


def test_c36_short_events_and_request_roles_from_the_probe_are_dropped_on_a_serial_port():
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", 0.3)
    s.rx += (cobs.frame(bytes([m.ROLE_EVENT, 0, 0, 0])) + cobs.frame(m.Request(7, 0, m.OP_KEEPALIVE).pack())
             + cobs.frame(result(7)))
    assert m.Result.unpack(lk.send(m.Request(7, 0, m.OP_LOCK_STATE, b"").pack())).corr == 7
    assert lk.noise == 4 and lk.dropped == 1 and not lk.events


def test_c36_a_short_answer_on_a_held_serial_port_is_resent_at_once():
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", 0.3)
    lk.held = lambda: True
    writes = []

    def probe(data):
        writes.append(data)
        s.rx += cobs.frame(result(9)[:4] if len(writes) == 1 else result(9))
    s.on_write = probe
    t0 = time.monotonic()
    assert m.Result.unpack(lk.send(m.Request(9, 0, m.OP_KEEPALIVE, b"", session=1).pack())).succeeded
    assert lk.retries == 1 and time.monotonic() - t0 < 0.5                   # not waited out


# ---- C-38: an unanswered resend fails the transport; recover with a confirm, COBS included -----------------------

class Probe:
    """A COBS probe stand-in on a Scripted stream: answers confirms (with its boot_id) and other requests, or nothing
    while `silent`."""

    def __init__(self, s):
        self.s, self.silent, self.boot_id, self.seen = s, False, 0x11, []
        s.on_write = self.write

    def write(self, data):
        for part in data.split(b"\x00"):
            if not part:
                continue
            msg = cobs.unframe(part)
            req = m.Request.unpack(msg)
            self.seen.append(req.op)
            if self.silent:
                continue
            body = confirm_payload(boot_id=self.boot_id) if req.op == m.OP_CONFIRM else b""
            self.s.rx += cobs.frame(result(req.corr, body))


def cobs_host():
    s = Scripted()
    p = Probe(s)
    lk = link.SerialLink.on_stream(s, "cobs", 0.1)
    lk.wait_add_s = 0.0
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    return lk, hst, p


def test_c38_an_unanswered_resend_fails_the_transport_and_the_next_request_confirms_first():
    lk, hst, p = cobs_host()
    p.silent = True
    with pytest.raises(link.TransportFailed):
        hst.request(0, m.OP_KEEPALIVE, locked=False)
    assert lk.failed_transport and p.seen[-2:] == [m.OP_KEEPALIVE, m.OP_KEEPALIVE]   # the request and its one resend
    p.silent, p.boot_id = False, 0x99                                        # back, and it had rebooted
    p.seen.clear()
    assert hst.request(0, m.OP_LOCK_STATE, locked=False).succeeded
    assert p.seen == [m.OP_CONFIRM, m.OP_LOCK_STATE]                         # the confirm went first
    assert not lk.failed_transport and lk.recoveries == 1 and hst.epoch == 1 # the changed boot_id: a reboot


def test_c38_no_confirm_in_the_recovery_is_a_connection_error_and_stays_failed():
    lk, hst, p = cobs_host()
    p.silent = True
    with pytest.raises(link.TransportFailed):
        hst.request(0, m.OP_KEEPALIVE, locked=False)
    with pytest.raises(ConnectionError):
        hst.request(0, m.OP_KEEPALIVE, locked=False)
    assert lk.failed_transport and m.OP_KEEPALIVE not in p.seen[-3:]         # only confirms went out


def test_c38_pipelined_requests_fail_together():
    lk, hst, p = cobs_host()
    p.silent = True
    with pytest.raises(link.TransportFailed):
        hst.pipeline([(0, m.OP_KEEPALIVE, b""), (0, m.OP_LOCK_STATE, b"")], locked=False)
    assert lk.failed_transport


# ---- PC-9: the last page's storage ------------------------------------------------------------------------------

def test_pc9_state_keeps_the_last_pages_storage():
    ep, hst, _ = in_process()
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Slot(slot=n, wire_fn=1, pins=ep.pairs[1][n], name=f"s{n}", attach="host") for n in range(3)])
    pages = []
    real = cfg._call

    def call(op, body=b"", **kw):
        r = real(op, body, **kw)
        if op == cfg.STATE:
            pages.append(body)
            if len(pages) == 1:
                ep.saved = dict(ep.config)                                    # saved between the pages
                r = m.Result(r.corr, r.resolution, r.detail, b"\x01" + r.payload[1:])   # say there is more
        return r
    cfg._call = call
    st = cfg.state()
    assert len(pages) == 2 and st.storage == "applied"                       # the second (last) page's


# ---- C-21: optional ops are used when declared ------------------------------------------------------------------

def test_c21_riscv_dm_says_which_optional_ops_the_probe_offers():
    ep, hst, _ = in_process(fake.esp32_v003())
    hst.open(3000)
    wire = riscv.Wire(hst, "oep.wire.swio")
    conn, _ = wire.attach(halt=True)
    dm = riscv.RiscvDm(hst, conn)
    assert dm.declared() == {"block", "run", "reset"}
    with pytest.raises(h.Rejected) as e:
        dm.step()
    assert e.value.result.detail == m.UNKNOWN_OPERATION


# ---- fixture: cs_setup_ns shown; i2c-target's reserved addresses ------------------------------------------------

def test_cs_setup_ns_is_read_and_shown():
    from oep_client import dump
    ep, hst, _ = in_process(fake.esp32_v003())
    spi = fixture.SpiTarget(hst)
    assert spi.cs_setup_ns == 4000
    ep2, hst2, _ = in_process(fake.p4_x035())
    assert fixture.SpiTarget(hst2).cs_setup_ns == 0
    caps = dump.collect(lambda fn, op, p: hst.request(fn, op, p, locked=False).payload)
    assert "CS setup ns: 4000" in dump.to_text(caps)


@pytest.mark.parametrize("address", [0x00, 0x07, 0x78, 0x7F])
def test_i2c_target_refuses_a_reserved_address_before_sending(address):
    ep, hst, sent = in_process(fake.p4_x035())
    i2c = fixture.I2cTarget(hst)
    n = len(sent)
    with pytest.raises(ValueError):
        i2c.configure(address)
    assert len(sent) == n


# ---- the ops encoding (core §7.4): a broken ops is not used ------------------------------------------------------

def _with_ops(name: str, value: bytes) -> fake.FakeProbe:
    probe = fake.p4_bench()
    return fake.FakeProbe(probe.label, probe.max_frame, [
        fake.Offered(o.fn, o.instance, o.name, (catalog.tlv(catalog.OPS, value),) + tuple(
            t for t in o.tlvs if t[0] != catalog.OPS)) if o.name == name else o for o in probe.offered], fill_ops=False)


@pytest.mark.parametrize("value", ["f901", "f80100", "01", "ff0000"])
def test_an_fn_whose_ops_break_the_encoding_is_not_used(value):
    """core §7.4 (rule review 2026-10-07): base(u8) + a bitmap of at least 1 byte, base + 8 x bytes <= 256 - nothing
    more (no bit-0 or last-byte rule)."""
    ep = endpoint.Endpoint(_with_ops("oep.fixture.gpio", bytes.fromhex(value)), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    with pytest.raises(core.UnusableFunction, match="core §7.4"):
        core.ops(hst, 4)
    with pytest.raises(core.UnusableFunction):
        fixture.Gpio(hst)                                                     # a client is never built on it
    assert core.ops(hst, 0) == set(reg.CORE.op.values())                     # the rest of the probe is used
    assert not hst.unusable


@pytest.mark.parametrize("value, ops", [("010300", {1, 2}), ("0003", {0, 1}), ("f801", {0xF8}), ("010100", {1})])
def test_ops_encodings_the_old_one_encoding_rule_refused_are_read(value, ops):
    """core §7.4: trailing zero bytes, bit 0 set, base 0xF8 with one byte - all valid encodings now."""
    ep = endpoint.Endpoint(_with_ops("oep.fixture.gpio", bytes.fromhex(value)), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    assert core.ops(hst, 4) == ops


def test_a_probe_whose_fn_0_ops_break_the_encoding_is_not_used():
    core_ops = catalog.pack_ops(reg.CORE.op.values())
    broken = core_ops + bytes(33 - len(core_ops))                             # base 1 + 8 x 32 bytes: past op 0xFF
    ep = endpoint.Endpoint(_with_ops(fake.CORE_NAME, broken), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    with pytest.raises(h.NotUsable, match="core §7.4"):
        core.describe(hst)
    with pytest.raises(h.NotUsable):
        hst.confirm()                                                         # nothing more is sent
    padded = core_ops + bytes(32 - len(core_ops))                             # base 1 + 8 x 31 bytes = 249: valid
    ep = endpoint.Endpoint(_with_ops(fake.CORE_NAME, padded), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    assert core.ops(hst, 0) == set(reg.CORE.op.values())


def test_a_list_entry_with_fn_0_is_left_out():
    """The core is never listed (core §7.2): a probe that lists an fn 0 anyway is not taken at its word."""
    entries = [catalog.ListEntry(0, 0, 1, 0, "oep.core"), catalog.ListEntry(3, 0, 1, 0, "oep.fixture.gpio")]
    hst = h.Host(lambda b: m.Result(m.Request.unpack(b).corr, m.COMPLETED, m.SUCCESS,
                                    catalog.pack_list_result(2, entries)).pack())
    assert [e.fn for e in core.list_entries(hst)] == [3]
