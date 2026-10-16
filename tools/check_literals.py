"""Count the OEP protocol numbers written as literals in the tests, the virtual models and the checks.

Tests, checks, fixtures and models look every protocol number up by name - the registry (src/oep_client/registry.py,
tests/spec/oep-v1.toml through tests/spec_names.py) or the probe's describe for a named list (oep-spec core §7.7) -
never as a bare number: a bare number says nothing at a glance and goes silently wrong when the numbering moves
(oep-spec core §2.5). The spec's byte vectors are the one exception (their bytes are the artifact).

This counts the patterns that are almost always a protocol number: a TLV built or matched with a numeric tag
(`tlv(0x05,`, `tags[5]`, `row[0]==7`), an op compared with a number (`op==17`, `op in (16, 24)`), a refusal built with a
number (`Reject(11,`), an unavailable cause or a critical tag byte (`b'\\x85'`). It is a ratchet: tests/test_literals.py
fails when a file's count grows past tools/literals_baseline.json, so new code uses names and the old count only goes
down (`--update` writes the new, lower baseline).

    python tools/check_literals.py            # the counts, per file, largest first
    python tools/check_literals.py --update   # write the current counts as the baseline
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "literals_baseline.json"
SCOPE = ["tests/*.py", "tests/hw/*.py", "src/oep_client/virtual_*.py", "src/oep_client/conformance*.py"]
PATTERNS = [
    re.compile(r"\btlv\(\s*(0x[0-9a-fA-F]+|\d+)\s*,"),                 # tlv(0x05, ...) / m.tlv(5, ...)
    re.compile(r"\bop\s*[!=]=\s*(0x[0-9a-fA-F]+|\d+)\b"),               # op==17
    re.compile(r"\bop\s+(?:not\s+)?in\s*\(\s*(0x[0-9a-fA-F]+|\d+)\s*,"),  # op in (16, 24)
    re.compile(r"\bReject\(\s*\d+\s*[,)]"),                              # Reject(11, ...)
    re.compile(r"\b(?:tags|values)\[\s*(0x[0-9a-fA-F]+|\d+)\s*\]"),     # tags[5]
    re.compile(r"\brow\[0\]\s*[!=]=\s*(0x[0-9a-fA-F]+|\d+)\b"),         # row[0]==7 (a describe TLV's tag)
    re.compile(r"\bcause\s*=\s*\d+\b"),                                  # cause=6
    re.compile(r"b['\"]\\x8[0-9a-fA-F]['\"]"),                           # b'\x85': a critical tag as received
]
SKIP = {"tests/test_literals.py", "tools/check_literals.py"}


def counts() -> dict[str, int]:
    out = {}
    for pattern in SCOPE:
        for path in sorted(ROOT.glob(pattern)):
            rel = str(path.relative_to(ROOT))
            if rel in SKIP:
                continue
            text = path.read_text()
            n = sum(len(p.findall(text)) for p in PATTERNS)
            if n:
                out[rel] = n
    return out


def main(argv: list[str]) -> int:
    now = counts()
    if "--update" in argv:
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        print(f"baseline: {sum(now.values())} literals in {len(now)} files")
        return 0
    for rel, n in sorted(now.items(), key=lambda kv: -kv[1]):
        print(f"{n:5}  {rel}")
    print(f"{sum(now.values())} literals in {len(now)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
