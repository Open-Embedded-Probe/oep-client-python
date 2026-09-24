"""Draft OEP capability discovery by name, on an in-process fake or a v1 draft probe.

  uv run python -m oep_client.v1 dump --port /run/board-identify/by-id/<probe>
  uv run python -m oep_client.v1 dump --fake p4-x035
  uv run python -m oep_client.v1 dump --fake esp32-v003 --prefix oep.fixture
  uv run python -m oep_client.v1 dump --fake p4-x035 --prefix oep.target --json
"""

from __future__ import annotations

import argparse
import sys

from . import dump, fake, host, link


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OEP capability discovery (draft)")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dump", help="list and describe every interface a probe offers")
    src = d.add_mutually_exclusive_group(required=True)
    src.add_argument("--fake", choices=sorted(fake.PROFILES), help="in-process example probe")
    src.add_argument("--port", help="a serial port with a v1 draft probe (lock-free reads only)")
    d.add_argument("--prefix", default="", help="only names under this namespace (label boundaries)")
    d.add_argument("--exact", action="store_true", help="the prefix is a whole name")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.fake:
        call = fake.PROFILES[args.fake]().call
    else:
        hst = host.Host(link.SerialLink(args.port).send)
        call = lambda fn, op, payload: hst.request(fn, op, payload, locked=False).payload   # noqa: E731
    caps = dump.collect(call, args.prefix, args.exact)
    sys.stdout.write(dump.to_json(caps) + "\n" if args.json else dump.to_text(caps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
