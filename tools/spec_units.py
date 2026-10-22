"""List the normative units of oep-spec documents - every bullet, numbered step and table data row, under its numbered
section - with stable ids (<doc>:<section>:<n>), for the rule-by-rule trace (docs/trace/*.json, tools/check_trace.py).

    python tools/spec_units.py <oep-spec checkout> [doc ...]   -> JSON on stdout
"""
import json
import re
import sys
from pathlib import Path

DOCS = {"core": "docs/oep-core.ja.md", "transports": "docs/oep-transports.ja.md", "common": "interfaces/oep-if-common.ja.md",
        "plan": "interfaces/oep-if-plan.ja.md", "restart": "interfaces/oep-if-restart.ja.md", "link": "interfaces/oep-if-link.ja.md",
        "fixture": "interfaces/oep-if-fixture.ja.md", "debug": "interfaces/oep-if-debug.ja.md",
        "console": "interfaces/oep-if-console.ja.md",
        "capture": "interfaces/oep-if-capture.ja.md"}


def units(spec: Path, doc: str) -> list[dict]:
    out, section, n, in_code = [], "pre", 0, False             # "pre": before the first numbered section
    for line in (spec / DOCS[doc]).read_text().splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        h = re.match(r"^#{2,4} (\d+(?:\.\d+)*)[.\s]", line)
        if h:
            section, n = h.group(1), 0
            continue
        s = line.strip()
        is_row = s.startswith("|") and not re.fullmatch(r"\|[\s:|-]*\|", s)
        if s.startswith(("- ", "* ")) or re.match(r"^\d+\. ", s) or is_row:
            if is_row and re.match(r"^\|\s*(op|tag|名前|状況|フィールド|値|モード|state|仕様|結果|欄|構成|送るもの|現在のphase|モード|kind)\s*\|", s):
                continue                                              # a header row
            n += 1
            out.append({"id": f"{doc}:{section}:{n}", "text": s})
    return out


if __name__ == "__main__":
    spec = Path(sys.argv[1])
    docs = sys.argv[2:] or list(DOCS)
    json.dump({d: units(spec, d) for d in docs}, sys.stdout, ensure_ascii=False, indent=1)
