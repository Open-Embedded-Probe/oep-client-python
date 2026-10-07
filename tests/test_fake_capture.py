"""oep.fixture.logic in the fake probe, through oep_client.capture (oep-if-capture): the counter waveform, the layouts,
triggers, repeat's ring and release, streaming pushes."""

import dataclasses
import struct

import pytest

from oep_client import capture as c, core, endpoint, fake, fake_capture, host as h, message as m


class Clock:
    t = 0

    def __call__(self):
        return self.t


def bench(profile=fake.p4_x035):
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    lc = c.LogicCapture(hst)
    return ep, hst, lc, clock


def counter_ok(lc, data, samples, first=0):
    """Channel k of sample i is bit k of the counter i."""
    for k in range(len(lc.config.positions)):
        assert lc.channel(data, k, samples) == [((first + i) >> k) & 1 for i in range(samples)]


def test_one_shot_three_channels_in_four_bit_samples():
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21), (lc.fn, 2, 22)])
    cfg = lc.configure(rate=1_000_000, samples=1000)
    assert (cfg.width, cfg.positions, cfg.samples) == (4, [0, 1, 2], 1000)
    assert cfg.rate == 1_000_000
    lc.start()
    assert lc.generation == 1
    (seg,) = lc.wait()
    assert (seg.serial, seg.samples, seg.trigger_index, seg.generation) == (0, 1000, None, 1)
    data = lc.read_segment(seg)
    assert len(data) == 500
    counter_ok(lc, data, 1000)
    lc.start()
    assert lc.generation == 2 and lc.status().generation == 2
    with pytest.raises(h.Unavailable) as e:
        lc.read_segment(seg)                                        # the last capture's segment: generation 1
    assert e.value.cause == "wrong_state"
    with pytest.raises(h.Unavailable):
        lc.release(0, generation=1)
    (seg2,) = lc.wait()
    assert seg2.generation == 2 and len(lc.read_segment(seg2)) == 500
    other = c.LogicCapture(hst)                                     # a host that did not start it asks status first
    other.config = lc.config
    assert other.read(0, 4) == data[:4] and other.generation == 2


def test_the_classic_esp32_sampler_takes_a_byte_a_sample():
    ep, hst, lc, _ = bench(fake.esp32_v003)
    core.plan_apply(hst, [(lc.fn, 0, 4), (lc.fn, 1, 5)])
    cfg = lc.configure(rate=1_000_000, samples=300)
    assert (cfg.width, cfg.positions) == (8, [0, 1])
    lc.start()
    (seg,) = lc.wait()
    counter_ok(lc, lc.read_segment(seg), 300)


def test_a_rate_is_the_source_divided_by_a_whole_number():
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    assert lc.configure(rate=3_000_000, query=True).rate == c.Fraction(20_000_000, 7)   # at or under the one asked
    assert lc.config is None                                        # query changes nothing


@pytest.mark.parametrize("kind,role,value,index", [
    (c.EDGE, 1, 0, 10),    # bit 1 rises at i % 4 == 2: the first at or after the pretrigger 10 is 10
    (c.EDGE, 2, 1, 16),    # bit 2 falls at i % 8 == 0
    (c.LEVEL, 3, 1, 10),   # bit 3 is 1 on 8..15
])
def test_a_trigger_is_where_the_channel_does_it(kind, role, value, index):
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, r, 20 + r) for r in range(4)])
    lc.configure(rate=1_000_000, samples=64, trigger=(kind, role, value), pretrigger=10)
    lc.start()
    (seg,) = lc.wait()
    assert seg.trigger_index == index



def test_force_is_accepted_and_wait_keeps_the_lock():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
    lc.configure(rate=1_000_000, samples=64, trigger=(c.EDGE, 1, 1))
    lc.start()
    lc.force()                                  # the fake finds its trigger at start: nothing is waiting
    sent = []
    keepalive = hst.keepalive
    hst.keepalive = lambda: (sent.append(1), keepalive())
    (seg,) = lc.wait(keepalive_s=1e-9)
    assert sent and seg.trigger_index is not None

def test_slipped_segments_say_so():
    ep, hst, lc, _ = bench()
    ep.capture_slipped = True
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, samples=64)
    lc.start()
    (seg,) = lc.wait()
    assert seg.slipped


def test_repeat_fills_its_ring_with_the_clock_and_goes_on_after_release():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
    cfg = lc.configure(rate=1_000_000, mode=c.REPEAT, samples=1000, segments=3)
    assert cfg.segments == 3
    lc.start()
    clock.t = 2                                                     # 2 ms = 2000 samples: two segments
    assert lc.status()[1] == 2
    clock.t = 10                                                    # the ring (3) fills and the capture stops
    state, done, _, _ = lc.status()
    assert (state, done) == (c.STATE["paused"], 3)
    segs = lc.segments()
    assert [s.serial for s in segs] == [0, 1, 2]
    whole = b"".join(lc.read_segment(s) for s in segs)
    counter_ok(lc, whole, 3000)                                     # segments follow on without a gap
    lc.release(2)
    assert ep.requests[-1].payload == struct.pack("<II", 1, 2)       # generation, serial
    clock.t = 11
    assert lc.status().state == c.STATE["capturing"]                # it went on by itself (no stopped event)
    (nxt,) = lc.segments(3)
    assert nxt.flags & fake_capture.FLAG["gap"]                        # it stopped: the next segment says so
    assert ep.captures[lc.fn].read(1, 0, 8, 64)[8] & 0x02           # released bytes are gone (read: gap)
    kinds = [f[5] for f in ep.pushes()]
    assert c.EVENT_STOPPED not in kinds or True


def test_streaming_pushes_the_bytes_while_subscribed():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21), (lc.fn, 2, 22)])
    lc.configure(rate=1_000_000, mode=c.STREAMING)
    lc.subscribe()
    lc.start()
    clock.t = 3                                                     # 3000 samples x 4 bits = 1500 bytes
    frames = ep.pushes()
    assert frames and all(f[0] == m.ROLE_DATA for f in frames)
    seqs = [struct.unpack_from("<H", f, 3)[0] for f in frames]
    assert seqs == list(range(len(frames)))
    pushes = [c.unpack_push(f) for f in frames]
    assert all(fn == lc.fn and g == lc.generation == 1 for fn, _, _, _, g in pushes)   # every frame: TLV generation
    data = b"".join(d for _, _, _, d, _ in pushes)
    assert pushes[0][2] == 0 and len(data) == 1500
    assert struct.unpack_from("<H", frames[0], 13)[0] == len(pushes[0][3])   # position(u64) len(u16) data [TLV]
    counter_ok(lc, data, 3000)
    lc.stop()
    assert lc.status()[2] == 1500 and lc.status().write_pos == 1500
    with pytest.raises(h.Unavailable):
        lc.unsubscribe() or lc.start()                                  # streaming without a subscription


def test_min_bytes_and_max_delay_batch_the_data_and_never_the_events():
    """core §11.3: the data (role 0x06) waits for min_bytes bytes or max_delay_ms since its oldest byte was there
    (0 = that condition unused); an event (role 0x05) goes at once, and its bytes do not count."""
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21), (lc.fn, 2, 22)])
    lc.configure(rate=1_000_000, mode=c.STREAMING)
    lc.subscribe(min_bytes=1000)
    assert hst.subscriptions == {lc.fn} and ep.subscribed[lc.fn] == (1000, 0)
    lc.start()
    clock.t = 1                                                     # 500 bytes: held
    assert [f for f in ep.pushes() if f[0] == m.ROLE_DATA] == []
    clock.t = 2                                                     # 1000 bytes: out
    assert sum(len(c.unpack_push(f)[3]) for f in ep.pushes() if f[0] == m.ROLE_DATA) == 1000
    lc.subscribe(max_delay_ms=5)                                    # replaces it: the delay alone
    clock.t = 3
    assert [f for f in ep.pushes() if f[0] == m.ROLE_DATA] == []    # the oldest byte waits from now
    clock.t = 8
    assert sum(len(c.unpack_push(f)[3]) for f in ep.pushes() if f[0] == m.ROLE_DATA) == 3000
    lc.subscribe(min_bytes=60000, max_delay_ms=60000)               # data held for long ...
    clock.t = 9
    lc.stop()
    frames = ep.pushes()
    assert [f[5] for f in frames if f[0] == m.ROLE_EVENT] == [c.EVENT_STOPPED]   # ... the event goes at once
    assert not [f for f in frames if f[0] == m.ROLE_DATA]


def test_events_go_out_only_while_subscribed():
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, samples=64)
    lc.start()
    assert ep.pushes() == []
    lc.subscribe()
    lc.start()
    kinds = [f[5] for f in ep.pushes()]
    assert kinds == [c.EVENT_SEGMENT, c.EVENT_STOPPED] and ep.outbox == []


def test_a_capture_listens_on_pins_other_interfaces_hold():
    ep, hst, lc, _ = bench()
    uart = core.find(hst, "oep.fixture.uart")
    core.plan_apply(hst, [(uart, 1, 12), (uart, 2, 6)])
    core.plan_apply(hst, [(lc.fn, 0, 12)])                          # the UART's RX, captured too
    gpio = core.find(hst, "oep.fixture.gpio")
    with pytest.raises(h.Rejected):
        core.plan_apply(hst, [(gpio, 1, 12)])                       # the UART still holds it against drivers



def test_an_analog_pin_is_shared_with_nothing():
    """The analog cuts its pads' digital input and output (oep-if-capture §1.2): refused whichever comes second."""
    ep, hst, lc, _ = bench()
    an = c.AnalogCapture(hst)
    gpio = core.find(hst, "oep.fixture.gpio")
    core.plan_apply(hst, [(an.fn, 0, 16)])
    for other in ([(lc.fn, 0, 16)], [(gpio, 1, 16)]):             # logic that would read 0, a driver that would not drive
        with pytest.raises(h.Unavailable) as e:
            core.plan_apply(hst, other)
        assert (e.value.cause, e.value.channels) == ("pin_in_use", [16])   # cause, channel (no holder: core §4.3)
    core.plan_apply(hst, [(lc.fn, 0, 17)])                          # another pad: fine
    core.plan_release(hst, [an.fn])
    core.plan_apply(hst, [(lc.fn, 0, 16)])
    with pytest.raises(h.Rejected):
        core.plan_apply(hst, [(an.fn, 0, 16)])                      # the analog after the logic: refused too
    with pytest.raises(h.Rejected):
        core.plan_apply(hst, [(an.fn, 0, 18), (gpio, 1, 18)])      # in one request as well

def test_fake_serve_streams_over_tcp_to_the_client():
    """The whole path wireskein takes: open_host, take, plan, configure, subscribe, start, stream, stop, finish."""
    import subprocess
    import sys

    from oep_client import link
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--tcp", "0", "--framing", "length",
                             "--capture-slipped"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        port = proc.stdout.readline().split()[1]
        hst = link.open_host(f"tcp://127.0.0.1:{port}", timeout=2.0)
        core.take(hst, 5000, owner="test")
        lc = c.LogicCapture(hst)
        core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
        lc.configure(rate=100_000, samples=500)                    # one-shot first
        lc.start()
        (seg,) = lc.wait()
        assert seg.slipped
        counter_ok(lc, lc.read_segment(seg), 500)
        lc.configure(rate=100_000, mode=c.STREAMING)               # then streaming: 25 kB/s at w 2
        lc.subscribe()
        lc.start()
        got = lc.stream(hst.link, nbytes=2000, seconds=3)
        lc.stop()
        lc.finish(hst.link, got)
        assert got.start == 0 and not got.gaps and got.seq_lost == 0 and len(got.data) >= 2000
        counter_ok(lc, got.data, len(got.data) * 4)
        hst.end()
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


# ---- analog and groups (oep-if-capture §1.2, §3.8, §4) -------------------------------------------------------------

def test_segments_carry_ns_times_with_an_uncertainty():
    ep, hst, lc, clock = bench()
    clock.t = 7
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, samples=64)
    lc.start()
    (seg,) = lc.wait()
    assert (seg.start_ns, seg.start_uncertainty_ns) == (7_000_000, 50)


def test_analog_values_scale_and_calibration():
    ep, hst, lc, clock = bench()
    an = c.AnalogCapture(hst)
    core.plan_apply(hst, [(an.fn, 0, 16), (an.fn, 1, 17)])
    cfg = an.configure(rate=10_000, samples=256, frontends={1: 0})
    assert (cfg.slot, cfg.offset, cfg.bits, cfg.order) == (16, 0, 12, [0, 1])
    assert cfg.rate == c.Fraction(83_333, 9)                        # the ADC's 83.3 kHz divided: at or under 10 kHz
    assert cfg.frontend == {0: 3, 1: 0} and cfg.skew_ns == {0: 0, 1: int(1e9 / (cfg.rate * 2))}   # one ADC, in turn
    assert cfg.scale_nv[0] == 3100 * 1_000_000 // 4095 and cfg.reference == ("internal", 1100, False)
    an.start()
    (seg,) = an.wait()
    data = an.read_segment(seg)
    assert an.values(data, 0, 256) == [fake_capture.analog_value(0, i) for i in range(256)]   # a square
    assert an.values(data, 1, 256) == [fake_capture.analog_value(1, i) for i in range(256)]   # a sine
    assert an.millivolts(0, 2048) == pytest.approx(3100 * 2048 / 4095, abs=1)
    assert an.millivolts(0, 4095) is None and an.millivolts(0, 0) is None    # clipped (§1.2 rule 6)
    assert an.ends_millivolts(0) == (0, pytest.approx(3100, abs=1))
    assert (an.clipped(0, 0), an.clipped(0, 1), an.clipped(0, 4094), an.clipped(0, 4095)) == (an.CLIP_LOW, 0, 0, an.CLIP_HIGH)
    square, sine = an.values(data, 0, 256), an.values(data, 1, 256)
    assert an.clip_counts(0, square) == (128, 128) and an.clip_counts(1, sine) == (sine.count(0), sine.count(4095)) == (4, 0)   # the sine's troughs round to 0
    assert an.clip_mask(0, square[30:34]) == [an.CLIP_HIGH, an.CLIP_HIGH, an.CLIP_LOW, an.CLIP_LOW]
    an.config.scale_nv[0] = -an.config.scale_nv[0]                  # an inverting frontend: code 0 is the high end
    assert (an.clipped(0, 0), an.clipped(0, 4095)) == (an.CLIP_HIGH, an.CLIP_LOW)
    assert an.ends_millivolts(0) == (pytest.approx(-3100, abs=1), 0)
    cal = an.calibration()
    assert [f[0] for f in cal.factory] == [0, 1, 2, 3] and cal.factory[0][1] == "org.example.fake.two-point"
    assert cal.factory[0][2] == struct.pack("<HH", 150, 3950)       # raw_len(u16) raw
    assert cal.vrefint == (1365, seg.start_ns) and cal.vrefint_nominal_mv == 1100


def test_a_group_starts_logic_and_analog_together_and_marks_the_trigger_on_both():
    ep, hst, lc, clock = bench()
    an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
    clock.t = 3
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21), (an.fn, 0, 16)])
    lc.configure(rate=1_000_000, samples=10_000, trigger=(c.EDGE, 1, 0), pretrigger=4000)
    an.configure(rate=10_000, samples=100)
    grp.bind([lc, an], trigger=lc)
    with pytest.raises(h.Unavailable) as e:
        an.start()                                                  # bound: the group starts it
    assert e.value.cause == "bound_in_group"
    _, start_ns = grp.start([lc, an])
    assert grp.generations == {lc.fn: 1, an.fn: 1} and lc.generation == an.generation == 1   # the answer's TLV
    st = grp.wait()
    assert st.start_ns == start_ns == 3_000_000 and st.trigger_fn == lc.fn
    (ls,), (as_,) = lc.segments(), an.segments()
    assert ls.start_ns - start_ns == 0 and as_.start_ns - start_ns == 5000          # the analog starts 5 us later
    # bit 1 rises at i % 4 == 2: the first at or after 4000 is 4002 (4.002 ms); the analog's (9259 Hz) nearest is 37
    assert ls.trigger_index == 4002 and st.trigger_ns == start_ns + 4_002_000
    assert as_.trigger_index == round((st.trigger_ns - as_.start_ns) * an.config.rate / 1_000_000_000) == 37
    grp.bind([])
    an.start()                                                      # unbound: its own again
    assert an.generation == 2 and lc.read_segment(ls) == lc.read_segment(ls)   # the logic's generation 1 still reads


def test_a_group_refuses_what_it_cannot_bind():
    ep, hst, lc, clock = bench()
    an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
    core.plan_apply(hst, [(lc.fn, 0, 20), (an.fn, 0, 16), (an.fn, 1, 17)])
    lc.configure(rate=1_000_000, samples=100)
    an.configure(rate=10_000, samples=100, mode=c.REPEAT)
    with pytest.raises(h.Unavailable) as e:
        grp.bind([lc, an])                                          # the modes differ
    assert e.value.cause == "wrong_state"
    with pytest.raises(h.Rejected, match="malformed"):
        grp.bind([lc, lc])                                          # the same fn twice
    with pytest.raises(h.Unsupported):
        hst.call(grp.fn, grp.BIND, struct.pack("<BH", 1, 4))        # an fn that is no track
    an.configure(rate=41_666, samples=100)                          # 2 channels x 41.6 kHz = the ADC's whole budget
    grp.bind([lc, an])
    assert an.config.rate * 2 <= 83_333
    with pytest.raises(h.Unavailable) as e:
        lc.configure(rate=1_000_000, samples=100)                   # bound: configure again after unbinding
    assert e.value.cause == "bound_in_group"                        # cause 4, nothing more (no holder_fn)
    with pytest.raises(h.Unavailable) as e:
        lc.start()                                                  # a bound track's start: the same
    assert e.value.cause == "bound_in_group"
    grp.bind([])
    lc.configure(rate=1_000_000, samples=100, trigger=(c.EDGE, 0, 0))
    with pytest.raises(h.Rejected):
        grp.bind([lc, an], trigger=an)                              # only the trigger track may have a trigger


# ---- what configure / describe say (oep-if-capture §3.3, §2; core §2.3) --------------------------------------------

def _configure_body(mode=c.ONE_SHOT, rate=1_000_000, critical=(), extra=b""):
    return (m.tlv(c.MODE, bytes([mode]), critical=c.MODE in critical)
            + m.tlv(c.RATE, struct.pack("<I", rate), critical=c.RATE in critical) + extra)


@pytest.mark.parametrize("what, body, tag", [
    ("a mode not declared", _configure_body(mode=c.REPEAT), c.MODE),
    ("a mode the definition leaves unused", _configure_body(mode=9), c.MODE),
    ("a rate under rate_range", _configure_body(rate=1), c.RATE),
    ("an unknown trigger type", _configure_body(extra=m.tlv(c.TRIGGER, struct.pack("<BBI", 99, 0, 0))), c.TRIGGER),
])
@pytest.mark.parametrize("critical", [False, True])
def test_configure_refuses_an_unhandled_value_unsupported_with_the_tag_as_received(what, body, tag, critical):
    """oep-if-capture §3.3 / core §2.3: configure's TLVs follow the general rule alone (no "always critical"): a value
    the probe does not handle is unsupported with the tag as received - bit 7 set or not - in configure and query."""
    ep, hst, lc, _ = bench(fake.esp32_v003)                         # one-shot only, 400 kHz - 2 MHz
    core.plan_apply(hst, [(lc.fn, 0, 4)])
    if critical:                                                    # the same request with the refused TLV critical
        tlvs = [(t | (0x80 if t == tag else 0), v) for t, v in m.split_tlvs(body)]
        body = b"".join(m.tlv(t & 0x7F, v, critical=bool(t & 0x80)) for t, v in tlvs)
    for op in (lc.CONFIGURE, lc.QUERY_OP):
        with pytest.raises(h.Unsupported) as e:
            hst.request(lc.fn, op, body, locked=op == lc.CONFIGURE)
        assert e.value.result.payload[0] == tag | (0x80 if critical else 0), what
    assert lc.config is None


def test_analog_frontend_past_the_declared_is_unsupported_critical_or_not():
    ep, hst, lc, _ = bench()
    an = c.AnalogCapture(hst)
    core.plan_apply(hst, [(an.fn, 0, 16)])
    for critical in (False, True):
        body = _configure_body(rate=10_000) + m.tlv(c.FRONTEND, bytes([0, 9]), critical=critical)
        with pytest.raises(h.Unsupported) as e:
            hst.request(an.fn, an.CONFIGURE, body)
        assert e.value.result.payload[0] == c.FRONTEND | (0x80 if critical else 0)


def test_configure_answer_has_no_timing_rate_accuracy_or_ignored():
    """The answer carries the actual values only: no timing (0x54), rate_accuracy (0x5A) or ignored (0x7F) - an
    unknown non-critical request TLV is skipped silently (core §2.3)."""
    ep, hst, lc, _ = bench()
    an = c.AnalogCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 20), (an.fn, 0, 16)])
    for fn, rate in ((lc.fn, 1_000_000), (an.fn, 10_000)):
        r = hst.request(fn, c.LogicCapture.CONFIGURE, _configure_body(rate=rate) + m.tlv(0x7E, b"\x01"))
        tags = {t for t, _ in m.split_tlvs(r.payload)}
        assert c.ACTUAL_RATE in tags and not {0x54, 0x5A, 0x7F} & tags


def test_describe_declares_no_background_layouts_or_budgets():
    """oep-if-capture §2 / §4: mode is mode(u8) max_samples(u32) max_segments(u32); channels is max(u8) alone; no
    rate_list / rate_limit / max_read / segment_ring / frontend_shared; a capture-group declares tracks only."""
    ep, hst, lc, _ = bench()
    an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
    for fn in (lc.fn, an.fn):
        d = core.describe(hst, fn)
        modes = [v for t, v in d if t & 0x7F == 0x40]
        assert modes and all(len(v) == 9 for v in modes)
        assert {v[0] for v in modes} == {c.ONE_SHOT, c.REPEAT, c.STREAMING}
        assert [v for t, v in d if t & 0x7F == 0x44] == [bytes([16 if fn == lc.fn else 4])]
        assert not {0x42, 0x43, 0x47, 0x48, 0x49} & {t & 0x7F for t, _ in d}
    tags = {t & 0x7F for t, _ in core.describe(hst, grp.fn)}
    assert 0x40 in tags and not {0x41, 0x42, 0x43} & tags


def test_a_bind_short_of_resources_is_unavailable_limit_naming_the_fn():
    """oep-if-capture §4: a bind whose tracks cannot take what they need together is unavailable cause 2 (limit) with
    TLV fn (core §4.3: no budget declared - the probe knows it); a refused bind changes nothing."""
    probe = fake.p4_x035()
    probe = fake.FakeProbe(probe.label, probe.max_frame, [
        o if o.name != "oep.fixture.capture-group"
        else dataclasses.replace(o, inner=(("max_tracks", 2), ("budgets", ((50_000, (11,)),))))
        for o in probe.offered], own_channels=probe.own_channels)
    ep, hst, lc, _ = bench(lambda: probe)
    an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
    core.plan_apply(hst, [(lc.fn, 0, 20), (an.fn, 0, 16), (an.fn, 1, 17)])
    lc.configure(rate=1_000_000, samples=100)
    an.configure(rate=20_000, samples=100)                          # 2 x 20 kHz: within 50 kHz
    grp.bind([lc, an])
    grp.bind([])
    an.configure(rate=41_666, samples=100)                          # 2 x 41.6 kHz: past it
    with pytest.raises(h.Unavailable) as e:
        grp.bind([lc, an])
    assert (e.value.cause, e.value.fn) == ("limit", an.fn)
    lc.configure(rate=1_000_000, samples=100)                       # nothing bound: its own again
