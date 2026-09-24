"""Draft OEP capability discovery by name - no hardware yet.

  uv run python -m oep_client.v1 dump --fake p4-x035
  uv run python -m oep_client.v1 dump --fake esp32-v003 --prefix oep.fixture
  uv run python -m oep_client.v1 dump --fake p4-x035 --prefix oep.target --json
"""

from __future__ import annotations

import argparse
import sys

from . import dump, fake


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OEP capability discovery (draft, fake probes only)")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dump", help="list and describe every interface a probe offers")
    d.add_argument("--fake", choices=sorted(fake.PROFILES), required=True, help="in-process example probe")
    d.add_argument("--prefix", default="", help="only names under this namespace (label boundaries)")
    d.add_argument("--exact", action="store_true", help="the prefix is a whole name")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    probe = fake.PROFILES[args.fake]()
    caps = dump.collect(probe.call, args.prefix, args.exact)
    sys.stdout.write(dump.to_json(caps) + "\n" if args.json else dump.to_text(caps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
