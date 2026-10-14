"""Check the rule-by-rule trace (docs/trace/<doc>.json) against oep-spec and the test suite.

docs/trace/<doc>.json: {"spec_commit": "...", "units": {"<doc>:<section>:<n>": {"text": "<the unit's first 40 characters>",
"verify": [pytest node ids], "mutants": [pytest node ids], "note": "..."} | {"text": ..., "excluded": "<why>"}}}

Every unit tools/spec_units.py lists must be there with the same text; a unit not excluded needs a verify test; every
node id named must be collected and pass.

    python tools/check_trace.py <oep-spec checkout> [doc ...]
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from spec_units import DOCS, units  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    spec = Path(sys.argv[1])
    docs = sys.argv[2:] or list(DOCS)
    problems, nodes, counts = [], set(), {}
    for doc in docs:
        path = ROOT / "docs" / "trace" / f"{doc}.json"
        if not path.exists():
            problems.append(f"{doc}: no {path.relative_to(ROOT)}")
            continue
        trace = json.loads(path.read_text())["units"]
        want = {u["id"]: u["text"] for u in units(spec, doc)}
        for uid, text in want.items():
            row = trace.get(uid)
            if row is None:
                problems.append(f"{uid}: not traced ({text[:60]})")
                continue
            if not text.startswith(row.get("text", "\0")[:40]):
                problems.append(f"{uid}: text differs from the spec ({row.get('text', '')[:40]!r} vs {text[:40]!r})")
            if "excluded" in row:
                if not row["excluded"].strip():
                    problems.append(f"{uid}: excluded without a reason")
                continue
            if not row.get("verify"):
                problems.append(f"{uid}: no verify test")
            nodes.update(row.get("verify", []) + row.get("mutants", []))
        for uid in trace:
            if uid not in want:
                problems.append(f"{uid}: not a unit of the spec any more")
        counts[doc] = (len(want), sum(1 for u in want if "excluded" in trace.get(u, {})),
                       sum(len(trace.get(u, {}).get("mutants", [])) > 0 for u in want))
    if nodes:
        collected = {line.strip() for line in subprocess.run(       # one node id a line (ids may hold spaces)
            [sys.executable, "-m", "pytest", "-q", "--co", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=ROOT).stdout.splitlines() if "::" in line}
        missing = sorted(n for n in nodes if n not in collected)
        problems += [f"not a collected test: {n}" for n in missing]
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *sorted(nodes - set(missing))],
                             capture_output=True, text=True, cwd=ROOT)
        tail = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else run.stderr
        if run.returncode:
            problems.append(f"named tests do not all pass: {tail}")
        print(f"named tests: {len(nodes)} - {tail}")
    for doc, (n, excluded, mutated) in counts.items():
        print(f"{doc:13s} {n:4d} units, {excluded:3d} excluded, {mutated:3d} with a mutant")
    for p in problems:
        print("PROBLEM", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
