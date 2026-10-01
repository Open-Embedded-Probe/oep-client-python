"""tests/hw: the hardware tests run only when OEP_HW_BOARDS names boards; otherwise every `hw` test is skipped and no
device is touched. Tests are grouped per board (one connection, one results file each)."""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from . import boards, record

_RUNS: list[record.Run] = []


def pytest_configure(config):
    config.addinivalue_line("markers", "hw: needs a probe board (OEP_HW_BOARDS=<board-identify ids>); skipped otherwise")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("OEP_HW_BOARDS"):
        return
    skip = pytest.mark.skip(reason="OEP_HW_BOARDS is not set: no hardware test (tests/hw/README.md)")
    for item in items:
        if "hw" in item.keywords:
            item.add_marker(skip)


def pytest_generate_tests(metafunc):
    if "board_id" in metafunc.fixturenames:
        ids = [s.strip() for s in os.environ.get("OEP_HW_BOARDS", "").split(",") if s.strip()]
        metafunc.parametrize("board_id", ids, scope="module")


@pytest.fixture(scope="module")
def run(board_id):
    """One board's run: the connection stays open from the flash step to the last test; the results file is written
    when the board's tests are done."""
    board = next(b for b in boards.selected() if b.id == board_id)
    r = record.Run(board)
    if board.kind == "fake":
        proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--pty", "--profile", board.fake_profile],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        r.fake = proc
        r.port = re.search(r"/dev/pts/\d+", proc.stdout.readline()).group(0)
    else:
        try:
            r.port = boards.resolve_port(board)
        except FileNotFoundError as e:
            r.port = board.port
            r.record("flash", port_lookup=str(e))
    _RUNS.append(r)
    yield r
    r.close_host()
    if r.fake is not None:
        r.fake.stdin.close()
        r.fake.wait(5)
    path = r.write()
    r.record("_results", path=str(path))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    r = getattr(item, "funcargs", {}).get("run")
    if r is None or rep.when not in ("call", "setup"):
        return
    if rep.when == "setup" and rep.outcome == "passed":
        return
    why = ""
    if rep.outcome == "skipped" and isinstance(rep.longrepr, tuple):
        why = rep.longrepr[2]
    elif rep.outcome == "failed":
        why = str(rep.longrepr).strip().splitlines()[-1] if rep.longrepr else ""
    name = item.originalname or item.name.split("[")[0]
    r.verdict(name[5:] if name.startswith("test_") else name, rep.outcome, why)


def pytest_terminal_summary(terminalreporter):
    if not _RUNS:
        return
    tr = terminalreporter
    tr.section("OEP hardware test summary")
    for r in _RUNS:
        for line in r.summary():
            tr.write_line(line)
        path = r.tests.get("_results", {}).get("path")
        if path:
            tr.write_line(f"  results: {os.path.relpath(path)}")
