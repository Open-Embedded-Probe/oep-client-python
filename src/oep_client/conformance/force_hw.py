"""force releases the resources of the session it takes from (core §6.4, §9; debug-common §2), on a real probe.

A session S attaches to a target over a wire (a debug connection: a numbered resource), then a session T of another id
opens with force. Then: S's connection is gone from connections, a detach of its number is rejected no_resource, S's
own requests are not completed, and T's attach to the same pins gets a new, larger number (numbers are never re-used
within a boot). T detaches and ends. The wire, its pins and the speed are named by the user (the target's wiring); the
route is the probe's USB vendor bulk, opened as conformance.usb_hw opens it. Numbers come from the SPEC registry.

    python -m oep_client.conformance.force_hw --unit UNIT --wire oep.wire.rvswd --swdio 14 --swclk 15 \\
        --spec SPEC --out NEW.json --lock LOCK
"""
import argparse
import json
import os
import secrets
import struct
from datetime import datetime, timezone
from pathlib import Path

from . import Checks, checker_sources_sha256, require, spec_identity, tlvs
from .usb import UsbWire


def interface(registry, name):
    spec = next(i for i in registry['interface'] if i['name'] == name)
    return {o['name']: o['code'] for o in spec['op']}


def connections(check, fn, op):
    """The live connection numbers of the wire `fn` (lock-free, session 0; paged by first, debug §2.1)."""
    first, found = 0, []
    while True:
        p = check.success(check.request(op, bytes((first,)), fn=fn))
        require(len(p) >= 2, 'connections fixed part')
        more, count = p[0], p[1]
        at = 2
        for _ in range(count):
            require(len(p) >= at + 14, 'connections entry')
            found.append(struct.unpack_from('<I', p, at)[0])
            tid_len = p[at + 13]
            at += 14 + tid_len
        if not more or not count:
            return found
        first += count


def run(check, *, wire_name, swdio, swclk, speed):
    ops = interface(check.reg, wire_name)
    entries = check.list_snapshot()
    fn = next((e['fn'] for e in entries if e['name'] == wire_name), None)
    require(fn is not None, f'{wire_name} is not listed')
    attach_body = struct.pack('<BHHI', 0, swdio, swclk, speed)    # method 0: run, do not halt (debug §1)
    out = {'wire_fn': fn}

    def open_session(force):
        sid = secrets.randbelow(0xffffffff) + 1
        check.corr = 0
        p = check.success(check.request(check.core['open'], struct.pack('<IB', 60000, force), session=sid))
        require(len(p) >= 8 and struct.unpack_from('<I', p, 4)[0] == check.boot, 'open boot')
        check.session = sid
        return sid

    def attach(sid):
        p = check.success(check.request(ops['attach'], attach_body, session=sid, fn=fn))
        require(len(p) >= 13, 'attach fixed part')
        return struct.unpack_from('<I', p)[0]

    s = open_session(0)
    first = attach(s)
    out['s_connection'] = first
    require(first in connections(check, fn, ops['connections']), 'the attach is not in connections')
    t = open_session(1)                                        # another id, force: takes the lock from S (core §6.4)
    out['t_session_differs'] = t != s
    live = connections(check, fn, ops['connections'])
    out['after_force'] = live
    require(first not in live, "force left S's connection open (core §9)")
    check.rejected(check.request(ops['detach'], struct.pack('<I', first), session=t, fn=fn), 'no_resource')
    saved, check.corr = check.corr, 1000                           # S's own numbers, far from T's
    reply = check.exchange(check.request(check.core['keepalive'], session=s))
    check.corr = saved
    out['s_keepalive'] = reply[3:5].hex()
    require(reply[3] == check.reg['resolutions']['rejected'], 'S kept a session after the force')
    second = attach(t)
    out['t_connection'] = second
    require(second != first and second > first, 'a connection number was re-used or went back (core §9)')
    tlvs(check.success(check.request(ops['detach'], struct.pack('<I', second), session=t, fn=fn)))
    require(second not in connections(check, fn, ops['connections']), 'detach left the connection')
    tlvs(check.success(check.request(check.core['end'], session=t)))
    check.session = None
    return out


def main(argv=None):
    from .. import __version__
    from ..hardware import equipment_lock
    from .usb_hw import BulkBackend, find_device
    parser = argparse.ArgumentParser(description=__doc__)
    for name, env in [('unit', 'OEP_CONFORMANCE_UNIT_ID'), ('spec', 'OEP_CONFORMANCE_SPEC'),
                      ('out', 'OEP_CONFORMANCE_OUT'), ('lock', 'OEP_HW_LOCK')]:
        parser.add_argument('--' + name, default=os.environ.get(env))
    parser.add_argument('--wire', required=True, help='the wire interface name, e.g. oep.wire.rvswd')
    parser.add_argument('--swdio', type=int, required=True, help="the target's SWDIO / SWIO channel")
    parser.add_argument('--swclk', type=int, default=0xFFFF, help="the target's SWCLK channel (0xFFFF: none)")
    parser.add_argument('--speed', type=int, default=1_000_000, help='attach max_speed (Hz)')
    args = parser.parse_args(argv)
    if not all(getattr(args, name) for name in ('unit', 'spec', 'out', 'lock')):
        parser.error('explicit unit, SPEC checkout, new output file and existing shared lock required')
    registry, identity = spec_identity(args.spec)
    report = {'status': 'failed', 'spec': identity, 'client_version': __version__,
              'checker_sources_sha256': checker_sources_sha256(), 'expected_unit': args.unit,
              'wire': args.wire, 'pins': [args.swdio, args.swclk], 'full_conformance': False,
              'started_at': datetime.now(timezone.utc).isoformat()}
    with Path(args.out).open('x') as artifact:
        try:
            with equipment_lock(args.lock):
                dev = find_device(registry['usb']['project_vid'], registry['usb']['project_pid'], args.unit)
                backend = BulkBackend(dev, registry['usb'])
                try:
                    wire = UsbWire(backend, 'bulk', **backend.shape)
                    check = Checks(wire.send, registry, args.unit)
                    check.usb_wire = wire
                    check.confirm()
                    check.identity()
                    try:
                        report['observed'] = run(check, wire_name=args.wire, swdio=args.swdio, swclk=args.swclk,
                                                 speed=args.speed)
                        report['status'] = 'passed'
                    finally:
                        if check.session is not None:          # a failed run: end what this run holds
                            try:
                                check.exchange(check.request(check.core['end'], session=check.session))
                            except Exception:
                                pass
                        report['trace'] = check.trace
                finally:
                    backend.close()
        except Exception as exc:
            report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        json.dump(report, artifact, ensure_ascii=False, indent=2, default=str)
        artifact.write('\n')
    print(f"{report['status']}: {args.out}" + (f" ({report.get('error')})" if report.get('error') else ''))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
