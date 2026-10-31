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
    res = acquire.capture_at(probe(), acquire.Request([("A", "10"), ("B", "11"), ("C", "12")], 20_000_000, 1000))
    assert res.complete and res.tick_hz == Fraction(20_000_000) and res.parts is None
    (part,) = res.all_parts
    i = np.arange(1000)
    for k, c in enumerate(part.channels):                  # the virtual bench's waveform: sample i is the counter i
        assert isinstance(c, acquire.Bits) and c.acquisition == {"pin": 10 + k}
        assert np.array_equal(levels(c), (i >> k) & 1), k
    assert "start_ns" in part.meta and "start_uncertainty_ns" in part.meta
    p = res.meta["probe"]
    assert p["unit_id"] and p["address"] == f"oep://{p['unit_id']}" and "boot_id" in p   # the probe, not the route


def test_on_a_session_the_caller_holds(probe):
    hst = link.open_host(probe())
    try:
        core.take(hst, 30_000, owner="test")
        req = acquire.Request([("A", "10")], 20_000_000, 200)
        first, second = acquire.capture(hst, req), acquire.capture(hst, req)    # the session stays the caller's
        assert first.complete and second.complete and first.meta["probe"]["boot_id"] == second.meta["probe"]["boot_id"]
        assert np.array_equal(levels(first.channels[0]), np.arange(200) & 1)
    finally:
        hst.end()
        hst.link.close()


def test_trigger_logic_and_analog_in_a_group(probe):
    req = acquire.Request([("A", "10"), ("B", "11")], 20_000_000, 2000, trigger=("B", "rise"), pretrigger=100,
                          analog=[("SQ", "16", None)], analog_rate=48_000, analog_samples=64)
    res = acquire.capture_at(probe(), req)
    a, b, sq = res.channels
    t = res.meta["trigger_tick"]
    assert t == 100 and levels(b)[t] == 1 and levels(b)[t - 1] == 0
    assert isinstance(sq, acquire.Analog) and sq.n == 64 and sq.value_bits == 12 and len(set(sq.values())) == 2
    assert "trigger_ns" in res.meta["probe"] and "group_start_ns" in res.meta["probe"]


def test_repeat_gives_one_part_per_segment(probe):
    req = acquire.Request([("A", "10")], 20_000_000, 100, mode="repeat", segments=5)
    res = acquire.capture_at(probe(), req)
    assert res.complete and res.layout == "timeline" and len(res.parts) == 5
    assert [p.tick0 for p in res.parts] == sorted(p.tick0 for p in res.parts)


def test_a_stream_is_put_together_on_a_timeline(probe, tmp_path):
    req = acquire.Request([("A", "10"), ("B", "11")], 1_000_000, 0, mode="stream", duration=0.2,
                          spool_dir=str(tmp_path))
    res = acquire.capture_at(probe(), req)
    assert res.complete and res.layout == "timeline" and res.parts
    n = sum(p.channels[0].n for p in res.parts)
    assert n > 0 and all(isinstance(c, acquire.Bits) for p in res.parts for c in p.channels)


def test_format_1_lines_as_their_changes(probe):
    req = acquire.Request([("A", "10"), ("B", "11")], 20_000_000, 1000, resolution={"B": 1})
    res = acquire.capture_at(probe(), req)
    a, b = res.channels
    assert isinstance(a, acquire.Bits) and isinstance(b, acquire.Changes)
    ks, lv, ct = b.decode()
    assert ct == [1] * len(ks) and ks[:2] == [2, 4]        # B (bit 1 of the counter) changes every second sample


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
