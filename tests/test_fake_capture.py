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
