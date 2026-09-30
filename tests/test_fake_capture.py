"""oep.fixture.capture in the fake probe, through oep_client.capture (oep-if-capture): the counter waveform, the layouts,
triggers, repeat's ring and release, streaming pushes."""

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
    (seg,) = lc.wait()
    assert (seg.serial, seg.samples, seg.trigger_index) == (0, 1000, None)
    data = lc.read_segment(seg)
    assert len(data) == 500
    counter_ok(lc, data, 1000)


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
    clock.t = 11
    (nxt,) = lc.segments(3)
    assert nxt.flags & fake_capture.FLAG["gap"]                        # it stopped: the next segment says so
    assert ep.captures[lc.fn].read(0, 8, 64)[8] & 0x02              # released bytes are gone (read: gap)


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
    data = b"".join(f[13:] for f in frames)
    assert struct.unpack_from("<Q", frames[0], 5)[0] == 0 and len(data) == 1500
    counter_ok(lc, data, 3000)
    lc.stop()
    assert lc.status()[2] == 1500


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
    assert cfg.rate_measured and cfg.rate_ppm == 1500
    an.start()
    (seg,) = an.wait()
    data = an.read_segment(seg)
    assert an.values(data, 0, 256) == [fake_capture.analog_value(0, i) for i in range(256)]   # a square
    assert an.values(data, 1, 256) == [fake_capture.analog_value(1, i) for i in range(256)]   # a sine
    assert an.millivolts(0, 4095) == pytest.approx(3100, abs=1)
    cal = an.calibration()
    assert [f[0] for f in cal.factory] == [0, 1, 2, 3] and cal.factory[0][1] == "org.example.fake.two-point"
    assert cal.vrefint == (1365, seg.start_ns)


def test_a_group_starts_logic_and_analog_together_and_marks_the_trigger_on_both():
    ep, hst, lc, clock = bench()
    an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
    clock.t = 3
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21), (an.fn, 0, 16)])
    lc.configure(rate=1_000_000, samples=10_000, trigger=(c.EDGE, 1, 0), pretrigger=4000)
    an.configure(rate=10_000, samples=100)
    grp.bind([lc, an], trigger=lc)
    with pytest.raises(h.Rejected):
        an.start()                                                  # bound: the group starts it
    _, start_ns = grp.start([lc, an])
    st = grp.wait()
    assert st.start_ns == start_ns == 3_000_000 and st.trigger_fn == lc.fn
    (ls,), (as_,) = lc.segments(), an.segments()
    assert ls.start_ns - start_ns == 0 and as_.start_ns - start_ns == 5000          # the analog starts 5 us later
    # bit 1 rises at i % 4 == 2: the first at or after 4000 is 4002 (4.002 ms); the analog's (9259 Hz) nearest is 37
    assert ls.trigger_index == 4002 and st.trigger_ns == start_ns + 4_002_000
    assert as_.trigger_index == round((st.trigger_ns - as_.start_ns) * an.config.rate / 1_000_000_000) == 37
    grp.bind([])
    an.start()                                                      # unbound: its own again


def test_a_group_refuses_what_it_cannot_bind():
    ep, hst, lc, clock = bench()
    an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
    core.plan_apply(hst, [(lc.fn, 0, 20), (an.fn, 0, 16), (an.fn, 1, 17)])
    lc.configure(rate=1_000_000, samples=100)
    an.configure(rate=10_000, samples=100, mode=c.REPEAT)
    with pytest.raises(h.Rejected):
        grp.bind([lc, an])                                          # the modes differ
    an.configure(rate=41_666, samples=100)                          # 2 channels x 41.6 kHz = the ADC's whole budget
    grp.bind([lc, an])
    assert an.config.rate * 2 <= 83_333
    with pytest.raises(h.Rejected):
        lc.configure(rate=1_000_000, samples=100)                   # bound: configure again after unbinding
    grp.bind([])
    lc.configure(rate=1_000_000, samples=100, trigger=(c.EDGE, 0, 0))
    with pytest.raises(h.Rejected):
        grp.bind([lc, an], trigger=an)                              # only the trigger track may have a trigger
