"""OEP v0 command line: identity, read, program, verify, reset over a serial port.

  uv run python -m oep_client.v0 --port /run/board-identify/by-id/<probe> identity
  uv run python -m oep_client.v0 --port ... program firmware.bin [--result-json out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import serial

from . import codec
from .client import Client
from .flash_image import Target, program_image, read_image, verify_image
from .services import ProbeIdentity
from .transport import FrameTransport


def open_client(port: str, timeout: float) -> Client:
    """port: a serial device, or usb:VID:PID[:serial] for a vendor bulk probe (pyusb)."""
    if port.startswith("usb:"):
        from .transport import BulkTransport
        parts = port.split(":")
        transport = BulkTransport.open(int(parts[1], 16), int(parts[2], 16), parts[3] if len(parts) > 3 else None)
    else:
        # Opening a USB-Serial/JTAG port resets an ESP32-P4 probe; give the firmware time to come back.
        stream = serial.Serial(port, 115200, timeout=0.05)
        time.sleep(1.0)
        transport = FrameTransport(stream)
    transport.discard_input()
    client = Client(transport, timeout=timeout)
    for attempt in range(5):
        try:
            client.confirm()
            return client
        except (TimeoutError, Exception):
            time.sleep(0.3)
    raise SystemExit("probe did not answer OEP confirmation")


def main() -> None:
    parser = argparse.ArgumentParser(description="OEP v0 client")
    parser.add_argument("--port", required=True)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--result-json", metavar="FILE")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("identity")
    p = sub.add_parser("read"); p.add_argument("output")
    p = sub.add_parser("program"); p.add_argument("image"); p.add_argument("--no-verify", action="store_true")
    p = sub.add_parser("verify"); p.add_argument("image")
    sub.add_parser("reset")
    args = parser.parse_args()

    client = open_client(args.port, args.timeout)
    result: dict = {"port": args.port, "limits": client.limits.__dict__}
    if args.command == "identity":
        functions = client.list_functions()
        result["functions"] = [f"{f.owner:#06x}:{f.id:#06x} rev {f.revision} fn {f.function}" for f in functions]
        identity_fn = client.find(*codec.DEF_PROBE_IDENTITY[:2])
        if identity_fn:
            identity = ProbeIdentity(client, identity_fn.function).get()
            result["identity"] = {"profile_id": f"0x{identity.profile_id:08x}", "firmware": f"0x{identity.firmware_revision:08x}"}
    else:
        target = Target(client)
        if args.command == "read":
            data, timings = read_image(target)
            Path(args.output).write_bytes(data)
            result.update({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "timings_seconds": timings})
        elif args.command == "program":
            image = Path(args.image).read_bytes()
            outcome = program_image(target, image, verify=not args.no_verify)
            result.update(outcome.as_dict())
            result["sha256"] = hashlib.sha256(image).hexdigest()
            if not outcome.verified:
                print(json.dumps(result, indent=1)); sys.exit(1)
        elif args.command == "verify":
            outcome = verify_image(target, Path(args.image).read_bytes())
            result.update(outcome.as_dict())
            if not outcome.verified:
                print(json.dumps(result, indent=1)); sys.exit(1)
        elif args.command == "reset":
            report = target.control.reset()
            print(f"reset flags=0x{report.flags:02x} attempts={report.attempts} pc=0x{report.pc:08x}")
    print(json.dumps(result, indent=1))
    if args.result_json:
        Path(args.result_json).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
