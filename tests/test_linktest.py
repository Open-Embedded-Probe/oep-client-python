"""oep_client.linktest: traffic patterns over a link, counted; the matrix switches rates with port_speed."""
import re
import subprocess
import sys

import pytest

from oep_client import core, link, linktest


@pytest.fixture
def pty():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--pty", "--profile", "esp32-v003",
                             "--broken-rate", "230400:1:in"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    yield re.search(r"/dev/pts/\d+", proc.stdout.readline()).group(0)
    proc.stdin.close()
    proc.wait(5)


@pytest.fixture
def pty_probe(pty):
    hst = link.open_host(pty, timeout=1.0)
    core.take(hst, 30000, owner="linktest")
    yield hst
    try:
        hst.end()
    finally:
        hst.link.close()


def test_run_counts_whole_frames(pty_probe):
    cell = linktest.run(pty_probe, "duplex", 2, 38, frames=40)          # 64 - 26: what one source answer carries
    assert cell.frames == 40 and cell.ok == 40 and cell.broken == 0 and cell.lost == 0 and cell.kb_s > 0
    assert "duplex x2" in cell.text() and "0.00 %" in cell.text()


def test_matrix_runs_at_the_speed_in_force_and_at_a_rate(pty_probe):
    results = list(linktest.matrix(pty_probe, rates=[None, 500000, 230400], patterns=["in", "out"], inflight=[1, 8],
                                   sizes=[16, 38, 4096], frames=20))
    now, fast, bad = results
    assert now.rate == 115200 and not now.switched and len(now.cells) == 2 * 1 * 3   # in-flight 8 skipped (max 1)
    assert any(c.error.startswith("over what one source answer carries") for c in now.cells)   # oep-if-link §2
    assert fast.rate == 500000 and fast.switched and fast.actual == 500000 and all(c.ok == 20 for c in fast.cells if not c.error)
    assert bad.rate == 230400 and bad.why == "no confirm at the new rate" and not bad.cells
    assert pty_probe.link.baud == 115200
    pty_probe.confirm()


def test_cli_linktest_prints_cells(pty):
    from oep_client.__main__ import main
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["linktest", pty, "--patterns", "in", "--frames", "10", "--sizes", "16"]) == 0
    assert "rate 115200" in out.getvalue() and "in     x1" in out.getvalue()
