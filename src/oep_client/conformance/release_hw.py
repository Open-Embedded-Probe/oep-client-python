"""A released pin goes back to its idle state, whatever released it (core §8, §9, §6.4), seen electrically on a real
probe: the line's far end is a CH32 target's GPIO input, read over the probe's own debug wire.

The channel's idle is set (runtime, not saved) to pull-up, then pull-down; fixture gpio drives the line the other way
(output low, then high), and the target must read that drive - the line is wired and the probe drives it. Then the
plan is released one way: plan_release, end, another id's force, or the lease running out. The target must read the
pull again: the probe no longer drives it (core §8: a released pin is in its idle state, a pull is no drive). The
target is read with a halt and a resume (no reset, nothing written but its GPIO port's clock-enable bit, put back);
the idle setting is unset at the end. This uses the runtime client for the setup and the reading: what is judged is
the line, not the client.

    python -m oep_client.conformance.release_hw --probe oep://30eda0e31108 --channel 2 --wire-pins 15,14 \\
        --rcc 0x40021018:3 --gpio 0x40010C00 --bit 6 --lock LOCK --out NEW.json
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

PATHS = ('plan_release', 'end', 'force', 'lease')


def main(argv=None):
    from .. import core, link, registry as reg, riscv
    from ..fixture import Gpio
    from ..hardware import equipment_lock
    from ..settings import Settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', required=True)
    parser.add_argument('--channel', type=int, required=True, help="the probe's channel wired to the target's pin")
    parser.add_argument('--wire-pins', required=True, help='SWDIO,SWCLK of the rvswd attach')
    parser.add_argument('--rcc', required=True, help="ADDRESS:BIT of the target's GPIO port clock enable")
    parser.add_argument('--gpio', type=lambda v: int(v, 0), required=True, help="the target's GPIO port base")
    parser.add_argument('--bit', type=int, required=True, help="the pin's bit in the port's input register")
    parser.add_argument('--indr', type=lambda v: int(v, 0), default=8, help='input register offset')
    parser.add_argument('--lock', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    swdio, swclk = (int(x) for x in args.wire_pins.split(','))
    rcc_addr, rcc_bit = int(args.rcc.split(':')[0], 0), int(args.rcc.split(':')[1])
    role = reg.FIXTURE_GPIO.enum['role']['line']
    report = {'status': 'failed', 'probe': args.probe, 'channel': args.channel, 'rows': [],
              'started_at': datetime.now(timezone.utc).isoformat()}

    with Path(args.out).open('x') as artifact:
        try:
            with equipment_lock(args.lock):
                hst = link.open_host(args.probe)
                try:
                    label = f'channel.GPIO{args.channel}.idle'

                    def target_level():
                        """The target's pin, read halted (own short session when none is open)."""
                        own = hst.session is None
                        if own:
                            hst.open(10000)
                        w = riscv.Wire(hst)
                        conn, _ = w.attach(halt=False, pins=(swdio, swclk))
                        dm = riscv.RiscvDm(hst, conn)
                        try:
                            dm.halt()
                            try:
                                rcc = dm.read32(rcc_addr)
                                if not rcc >> rcc_bit & 1:
                                    dm.write32(rcc_addr, rcc | 1 << rcc_bit)
                                level = dm.read32(args.gpio + args.indr) >> args.bit & 1
                                if not rcc >> rcc_bit & 1:
                                    dm.write32(rcc_addr, rcc)          # put the clock enable back
                            finally:
                                dm.resume()
                        finally:
                            w.detach(conn)
                            if own:
                                hst.end()
                        return level

                    settings = Settings(hst)
                    for pull, drive, want_driven, want_idle in (('pull-up', Gpio.OUTPUT_LOW, 0, 1),
                                                                 ('pull-down', Gpio.OUTPUT_HIGH, 1, 0)):
                        for path in PATHS:
                            row = {'idle': pull, 'path': path}
                            report['rows'].append(row)
                            hst.open(1000 if path == 'lease' else 10000)
                            settings.set_named('core', {label: pull})
                            core.plan_apply(hst, [(Gpio(hst).fn, role, args.channel)])
                            Gpio(hst).configure(args.channel, drive)
                            row['driven'] = target_level()
                            if path == 'plan_release':
                                core.plan_release(hst, [Gpio(hst).fn])
                            elif path == 'end':
                                hst.end()
                            elif path == 'force':
                                hst.open(10000, force=True)      # another id: the old session's plan is released
                            else:
                                time.sleep(1.6)                    # the 1000 ms lease runs out
                                hst.session = None
                            row['released'] = target_level()
                            row['ok'] = row['driven'] == want_driven and row['released'] == want_idle
                            if hst.session is not None:
                                hst.end()
                    report['status'] = 'passed' if all(r['ok'] for r in report['rows']) else 'failed'
                finally:
                    try:
                        hst.open(10000, force=True)
                        Settings(hst).unset_named('core', [f'channel.GPIO{args.channel}.idle'])
                        hst.end()
                    finally:
                        hst.link.close()
        except Exception as exc:
            report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        json.dump(report, artifact, ensure_ascii=False, indent=2)
        artifact.write('\n')
    print(f"{report['status']}: {args.out}" + (f" ({report['error']})" if report.get('error') else ''))
    for r in report['rows']:
        print(f"  idle {r['idle']:9} {r['path']:12} driven {r.get('driven')} released {r.get('released')}"
              f" {'ok' if r.get('ok') else 'WRONG'}")
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
