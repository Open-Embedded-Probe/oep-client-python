"""acquire: one capture put together - parts, losses, times and the probe's facts - on the virtual bench over TCP,
opened by acquire itself (capture_at) or on a session the caller holds (capture)."""

import re
import subprocess
import sys
from fractions import Fraction

import pytest

from oep_client import acquire, core, link

np = pytest.importorskip("numpy")
SERVE = "oep_client.virtual_bench_serve"


@pytest.fixture
def probe():
    procs = []

    def start(*extra, profile="p4-x035"):
        p = subprocess.Popen([sys.executable, "-m", SERVE, "--tcp", "0", "--framing", "length", "--profile", profile,
                              *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(p)
        return f"tcp://127.0.0.1:{re.search(r'PORT (\d+)', p.stdout.readline()).group(1)}"
    yield start
    for p in procs:
        p.stdin.close()
        p.wait(5)


def levels(c):
    return np.unpackbits(np.frombuffer(c.bits, np.uint8), bitorder="little")[: c.n]


def test_one_shot_lines_and_the_probes_facts(probe):
    res = acquire.capture_at(probe(), acquire.Request([("A", "GPIO10"), ("B", "GPIO11"), ("C", "GPIO12")], 20_000_000, 1000))
    assert res.complete and res.tick_hz == Fraction(20_000_000) and res.parts is None
    (part,) = res.all_parts
    i = np.arange(1000)
    for k, c in enumerate(part.channels):                  # the virtual bench's waveform: sample i is the counter i
        assert isinstance(c, acquire.Bits) and c.acquisition == {"pin": 10 + k}
        assert np.array_equal(levels(c), (i >> k) & 1), k
    assert "start_ns" in part.meta and "start_uncertainty_ns" in part.meta
    p = res.meta["probe"]
    assert p["unit_id"] and p["address"] == f"oep://{p['unit_id']}" and "boot_id" in p   # the probe, not the route


def test_glitch_filter_on_the_logic_lines(probe):
    # the virtual bench's counter at 20 MHz: line k changes every 2^k samples (50 ns x 2^k); a 100 ns filter keeps a
    # level once it has held 2 samples (capture §3.8): line 0's one-sample levels go, line 2's four-sample ones stay
    req = acquire.Request([("A", "GPIO10"), ("C", "GPIO12")], 20_000_000, 1000, glitch_filter_ns=100)
    res = acquire.capture_at(probe(), req)
    a, c = res.all_parts[0].channels
    assert np.count_nonzero(np.diff(levels(a))) < 10                  # line 0: its 1-sample levels filtered away
    assert np.count_nonzero(np.diff(levels(c))) > 200                 # line 2: kept
    with pytest.raises(ValueError):
        acquire.Request([("A", "GPIO10")], 20_000_000, 1000, glitch_filter_ns=0)


def test_on_a_session_the_caller_holds(probe):
    hst = link.open_host(probe())
    try:
        core.take(hst, 30_000, owner="test")
        req = acquire.Request([("A", "GPIO10")], 20_000_000, 200)
        first, second = acquire.capture(hst, req), acquire.capture(hst, req)    # the session stays the caller's
        assert first.complete and second.complete and first.meta["probe"]["boot_id"] == second.meta["probe"]["boot_id"]
        assert np.array_equal(levels(first.channels[0]), np.arange(200) & 1)
    finally:
        hst.end()
        hst.link.close()


def test_trigger_logic_and_analog_in_a_group(probe):
    req = acquire.Request([("A", "GPIO10"), ("B", "GPIO11")], 20_000_000, 2000, trigger=("B", "rise"), pretrigger=100,
                          analog=[("SQ", "GPIO16", None)], analog_rate=48_000, analog_samples=64)
    res = acquire.capture_at(probe(), req)
    a, b, sq = res.channels
    t = res.meta["trigger_tick"]
    assert t == 100 and levels(b)[t] == 1 and levels(b)[t - 1] == 0
    assert isinstance(sq, acquire.Analog) and sq.n == 64 and sq.value_bits == 12 and len(set(sq.values())) == 2
    assert "trigger_ns" in res.meta["probe"] and "group_start_ns" in res.meta["probe"]


def test_repeat_gives_one_part_per_segment(probe):
    req = acquire.Request([("A", "GPIO10")], 20_000_000, 100, mode="repeat", segments=5)
    res = acquire.capture_at(probe(), req)
    assert res.complete and res.layout == "timeline" and len(res.parts) == 5
    assert [p.tick0 for p in res.parts] == sorted(p.tick0 for p in res.parts)


def test_a_stream_is_put_together_on_a_timeline(probe, tmp_path):
    req = acquire.Request([("A", "GPIO10"), ("B", "GPIO11")], 1_000_000, 0, mode="stream", seconds=0.2,
                          spool_dir=str(tmp_path))
    res = acquire.capture_at(probe(), req)
    assert res.complete and res.layout == "timeline" and res.parts
    n = sum(p.channels[0].n for p in res.parts)
    assert n > 0 and all(isinstance(c, acquire.Bits) for p in res.parts for c in p.channels)


def test_a_stream_runs_while_the_caller_works_on_the_same_session(probe, tmp_path):
    hst = link.open_host(probe())
    try:
        core.take(hst, 30_000, owner="test")
        seen = []

        def steps(wait):                                   # sections: a request on the session, then collect meanwhile
            for _ in range(3):
                seen.append(hst.clock_best(2).uptime_ns)
                wait(0.1)
        req = acquire.Request([("A", "GPIO10")], 1_000_000, 0, mode="stream", during=steps, spool_dir=str(tmp_path))
        res = acquire.capture(hst, req)
        assert res.complete and res.parts and len(seen) == 3 and seen == sorted(seen)
        assert sum(p.channels[0].n for p in res.parts) > 0
        with pytest.raises(RuntimeError):                  # the caller's own failure stops the stream and comes out
            acquire.capture(hst, acquire.Request([("A", "GPIO10")], 1_000_000, 0, mode="stream",
                                                 during=lambda wait: (wait(0.05), (_ for _ in ()).throw(RuntimeError()))))
        assert acquire.capture(hst, acquire.Request([("A", "GPIO10")], 1_000_000, 0, mode="stream", seconds=0.1)).complete
    finally:
        hst.end()
        hst.link.close()


def test_during_is_for_a_stream_in_place_of_seconds():
    for kw in ({"mode": "oneshot"}, {"mode": "stream", "seconds": 1.0}):
        with pytest.raises(ValueError):
            acquire.Request([("A", "GPIO10")], 1_000_000, 100, during=lambda wait: None, **kw)


def test_a_format_1_stream_on_disk(probe, tmp_path):
    """Streamed in format 1, raw and reduced lines, put together on disk as it comes: every segment joined in order."""
    for resolution in ({"B": ("change_latch", 4)}, {"A": ("sample", 4), "B": ("any_high", 8)}):
        req = acquire.Request([("A", "GPIO10"), ("B", "GPIO11")], 1_000_000, 0, mode="stream", seconds=0.3,
                              resolution=resolution, spool_dir=str(tmp_path))
        res = acquire.capture_at(probe(), req)
        assert res.complete and len(res.parts) == 1, resolution


def test_format_1_lines_reduced(probe):
    """A sampled line comes as bits at its D, another reduction as Reduced: A (bit 0 of the counter, a new level every
    sample) sampled every 2nd sample is one level throughout; B (bit 1, a change every 2nd sample) has a change in every
    interval of 4."""
    req = acquire.Request([("A", "GPIO10"), ("B", "GPIO11")], 20_000_000, 1000,
                          resolution={"A": ("sample", 2), "B": ("change_latch", 4)})
    res = acquire.capture_at(probe(), req)
    a, b = res.channels
    assert isinstance(a, acquire.Bits) and a.step == 2 and a.n == 500
    levels = [(a.bits[i // 8] >> (i % 8)) & 1 for i in range(a.n)]
    assert len(set(levels)) == 1
    assert isinstance(b, acquire.Reduced) and b.kind == "change_latch" and b.step == 4 and b.n == 250
    assert all(v >> 1 for v in b.values())
    with pytest.raises(ValueError):                       # a resolution names a reduction and D 2 or more
        acquire.Request([("A", "GPIO10")], 1_000_000, 100, resolution={"A": 4})
    with pytest.raises(ValueError):
        acquire.Request([("A", "GPIO10")], 1_000_000, 100, resolution={"A": ("sample", 1)})


def test_uart_link_speed_is_asked_for_unless_turned_off():
    import types
    calls = []

    class Link:
        @staticmethod
        def raise_speed(hst, rates, *, flows=None, record=False, max_tries=None):
            calls.append((rates, record, max_tries))
            assert flows == acquire.FLOWS                                 # a capture verifies the read-back way

    assert acquire._raise(Link, "H", [1_500_000, 921_600]) == [1_500_000, 921_600]
    assert acquire._raise(Link, "H", []) == []
    assert calls == [([1_500_000, 921_600], False, None)]                  # asked rates: tried as given
    calls.clear()
    hst = types.SimpleNamespace(link=types.SimpleNamespace(speed=types.SimpleNamespace(skipped=[1_500_000], retried=None)))
    assert acquire._raise_default(Link, hst, "/dev/X") == (acquire.FAST, True)     # the record shaped them
    assert calls == [(acquire.FAST, True, acquire.PER_SESSION)]            # the default: the client's record decides
    assert acquire._raise_default(Link, hst, None) == ([], False)          # not a serial port this host opened
    assert acquire.FAST[0] == 1_500_000


def test_probe_clock_against_the_host():
    import types
    r = lambda host, up, unc, boot=7: types.SimpleNamespace(host_ns=host, uptime_ns=up, boot_id=boot, uncertainty_ns=unc)  # noqa: E731
    info = acquire._clock_info(r(1_000_000_000, 5_000_000_000, 50_000), r(2_000_000_000, 6_000_100_000, 50_000))
    assert info["before"]["uptime_ns"] == 5_000_000_000 and info["after"]["host_ns"] == 2_000_000_000
    assert abs(info["rate_ppm"] - 100) < 1e-6 and abs(info["rate_ppm_uncertainty"] - 100) < 1e-6
    assert "rate_ppm" not in acquire._clock_info(r(1, 5, 1), r(2, 6, 1, boot=8))   # restarted between: no rate
    assert acquire._clock_info(None, None) == {}
    assert acquire._clock(types.SimpleNamespace()) is None                 # no reading: the capture goes on


def test_lines_are_named_by_channel_name_or_label(probe):
    src = probe("--label", "13=bus.sda", "--label", "12=bus@PB9")
    res = acquire.capture_at(src, acquire.Request([("D", "bus.SDA"), ("E", "gpio11"), ("F", "BUS@pb9")], 20_000_000, 100))
    assert [c.acquisition["pin"] for c in res.channels] == [13, 11, 12]      # by role, by name, by the target pin (S@P)
    assert res.meta["probe_channels"] == {"D": 13, "E": 11, "F": 12}
    with pytest.raises(ValueError, match="no channel of that name"):
        acquire.capture_at(probe(), acquire.Request([("A", "10")], 20_000_000, 100))     # a number is not a name
    with pytest.raises(ValueError, match="no channel carries that label"):
        acquire.capture_at(probe(), acquire.Request([("A", "x035.nrst")], 20_000_000, 100))


def test_stream_segments_join_bit_by_bit(tmp_path):
    """A stream's segments need not end on a whole byte of each line (2 lines in 2-bit samples, 65332 samples a
    segment on the P4): they join into one part, bit by bit - parts are cut only where data was lost."""
    import random
    import types
    from oep_client.capture import SegmentInfo
    rnd = random.Random(1)
    n_seg, per = 3, 12                                  # 12 samples: 3 bytes of the stream, 1.5 bytes a line
    a = [rnd.randrange(2) for _ in range(n_seg * per)]
    b = [rnd.randrange(2) for _ in range(n_seg * per)]
    data = bytearray()
    for i in range(0, len(a), 4):                       # sample i: bits 2i (A) and 2i + 1 (B) of the stream
        v = 0
        for j in range(4):
            v |= (a[i + j] | b[i + j] << 1) << (2 * j)
        data.append(v)
    segs = {k * 3: SegmentInfo(k * 3, per, 1_000_000 + k * per * 200, 50) for k in range(n_seg)}
    got = types.SimpleNamespace(data=data, segments=segs, gaps=[], start=0)
    cfg = types.SimpleNamespace(rate=5_000_000, width=2, positions=None, event_d={})
    req = acquire.Request([("A", "x"), ("B", "y")], 5_000_000, 0, mode="stream", seconds=1)
    parts, lost = acquire._stream_parts(None, cfg, got, ["A", "B"], [10, 11], req)
    assert not lost and len(parts) == 1
    got_a, got_b = (list(levels(c)) for c in parts[0].channels)
    assert got_a == a and got_b == b


def test_bits_written_across_bytes(tmp_path):
    import io
    rnd = __import__("random").Random(2)
    want, f, carry = [], io.BytesIO(), None
    for n in (5, 12, 3, 16, 7):
        bits = [rnd.randrange(2) for _ in range(n)]
        want += bits
        col = bytes(sum(bits[i + j] << j for j in range(min(8, n - i))) for i in range(0, n, 8))
        carry = acquire._write_bits(f, carry, col, n)
    acquire._flush_bits(f, carry)
    out = np.unpackbits(np.frombuffer(f.getvalue(), np.uint8), bitorder="little")[: len(want)]
    assert list(out) == want and len(f.getvalue()) == (len(want) + 7) // 8


def test_parts_say_their_samples(probe, tmp_path):
    """Part.samples: a part's ticks - a part of reduced lines only ends inside its last interval, so its lines do not
    say it. One-shot: Result.samples and all_parts[0].samples; a stream's parts add up."""
    req = acquire.Request([("A", "GPIO10")], 20_000_000, 1000, resolution={"A": ("any_low", 64)})
    res = acquire.capture_at(probe(), req)
    assert res.samples == 1000 and res.all_parts[0].samples == 1000 and res.channels[0].n == 16
    req = acquire.Request([("A", "GPIO10"), ("B", "GPIO11")], 1_000_000, 0, mode="stream", seconds=0.2,
                          resolution={"A": ("change_latch", 8), "B": ("sample", 8)}, spool_dir=str(tmp_path))
    res = acquire.capture_at(probe(), req)
    for p in res.all_parts:
        assert p.samples and -(-p.samples // 8) == p.channels[0].n
