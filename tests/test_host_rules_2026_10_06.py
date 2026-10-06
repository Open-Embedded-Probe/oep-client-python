"""The host side of the rules added since the 2026-10-02 rule changes: docs/v1-rule-change-proposal-2026-10-06.md
(oep-spec b4b08f1, 40291a4) and 2e70f40 / 73a0c37 - confirm's bounds (C-20), max_op_ms's ceiling (C-47), the transfer
time before the first confirm answer (N-1), resumed = 0 for the last session_id (C-19), the heartbeat's boot_id, short
answers and wrong-direction roles (C-36), an unanswered resend fails the transport (C-38), the last page's storage
(PC-9), the optional ops (C-21), cs_setup_ns and i2c-target's reserved addresses."""

import struct
import time

import pytest

from oep_client import catalog, cobs, config, core, endpoint, fake, fixture, host as h, link, message as m
from oep_client import registry as reg, riscv

from test_fake_spec import Clock
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


# ---- heartbeats: the boot_id watched (core §6.5, §11.2) ----------------------------------------------------------

def heartbeat(boot_id, uptime_ns, seq=0):
    return bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 0, seq, reg.CORE.event["heartbeat"]) + struct.pack("<IQ", boot_id,
                                                                                                         uptime_ns)


def test_heartbeats_are_read_and_a_changed_boot_id_means_a_reboot():
    boot = [0x11]

    def respond(msg):
        req = m.Request.unpack(msg)
        if req.op == m.OP_CONFIRM:
            return [result(req.corr, CONFIRM_V1)]
        return [heartbeat(boot[0], 5_000_000_000), result(req.corr, b"")]
    lk = make_link(Stream(respond))
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    hst.request(0, m.OP_KEEPALIVE, locked=False)
    assert (lk.heartbeats, hst.uptime_ns, hst.epoch) == (1, 5_000_000_000, 0)
    hst._fns["oep.fixture.gpio"] = 4
    boot[0] = 0x22                                                            # the probe rebooted
    hst.request(0, m.OP_KEEPALIVE, locked=False)
    assert hst.epoch == 1 and hst._fns == {} and len(lk.events) == 2         # kept with the other events


def test_heartbeats_from_the_fake_reach_the_host_through_pump():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    s = Stream(lambda msg: [r for r in [ep.handle(msg, 1)] if r] + ep.pushes())
    lk = make_link(s)
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    hst.open(3000)
    hst.subscribe(0, 0, 1000)
    ep.now.t += 1000
    s.rx += frame(ep.pushes()[0])
    lk.pump(0.05)
    assert lk.heartbeats == 1 and hst.uptime_ns == 1_000_000_000


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
        i2c.configure(address, 1)
    assert len(sent) == n
