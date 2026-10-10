"""Run raw bulk/HID checks on the core-v1 software USB model, never real USB.

Explicit unit/SPEC/new evidence/lock settings are shared with conformance CLI.
This model preserves transfer/report boundaries but is not a USB gadget.
"""
import argparse
import os
import time

from .conformance_usb import inspect


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('bulk', 'hid'), required=True)
    for name, env in [('unit', 'OEP_CONFORMANCE_UNIT_ID'), ('spec', 'OEP_CONFORMANCE_SPEC'),
                      ('out', 'OEP_CONFORMANCE_OUT'), ('lock', 'OEP_HW_LOCK')]:
        parser.add_argument('--' + name, default=os.environ.get(env))
    parser.add_argument('--report-id', type=int, default=6)
    parser.add_argument('--input-size', type=int, default=9)
    parser.add_argument('--output-size', type=int, default=11)
    args = parser.parse_args(argv)
    if not all(getattr(args, name) for name in ('unit', 'spec', 'out', 'lock')):
        parser.error('explicit unit, SPEC checkout, new output file and existing shared lock required')
    shape = dict(input_size=args.input_size, output_size=args.output_size, report_id=args.report_id) \
        if args.kind == 'hid' else dict(out_packet_size=8)
    def factory():
        from .endpoint import Endpoint
        from .virtual_bench import core_v1
        from .virtual_bench_usb import Usb
        origin = time.monotonic()
        ep = Endpoint(core_v1(), lambda: int((time.monotonic() - origin) * 1000))
        return Usb(ep, args.kind, **shape)
    report = inspect(factory, kind=args.kind, unit=args.unit, spec=args.spec, out=args.out,
                     lock=args.lock, adapter='core-v1 software USB model; no physical USB', **shape)
    print(f"{report['status']}: {args.out}")
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
