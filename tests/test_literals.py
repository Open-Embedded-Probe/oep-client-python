"""The literal protocol numbers in the tests, the virtual models and the checks only go down (tools/check_literals.py):
a file whose count grows past tools/literals_baseline.json fails - new code looks numbers up by name."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_literals  # noqa: E402


def test_no_file_gains_literal_protocol_numbers():
    baseline = json.loads(check_literals.BASELINE.read_text())
    grown = {rel: (baseline.get(rel, 0), n) for rel, n in check_literals.counts().items() if n > baseline.get(rel, 0)}
    assert not grown, ("literal protocol numbers added (baseline, now) - look them up by name instead "
                       f"(tests/spec_names.py, the registry, describe for named lists): {grown}")
