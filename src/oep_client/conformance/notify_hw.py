"""Subscriptions and notifications on a real probe (core §10, transports §3), over its USB vendor bulk.

The source is oep.fixture.logic in streaming mode on one channel the user names (a capture only listens), at the lowest
rate its describe offers. Checked:
  NOTIFY-LOCK      subscribe needs the lock: without a session it is not completed (core §10.3)
  NOTIFY-NONE      unsubscribe with no subscription succeeds (core §10.3)
  NOTIFY-SEQ       data frames of the fn: seq from 0, one by one, positions back to back (core §10.2)
  NOTIFY-FIRST     answers come while it streams, within the request's bound (core §10.4 1)
  NOTIFY-REPLACE   subscribe again: seq from 0 again (core §10.3)
  NOTIFY-DELAY     min_bytes 0, max_delay_ms 100: data comes in batches about that far apart (core §10.3)
  NOTIFY-ROUTE     another route of the probe (its HID) gets none of it (core §10.4 3, transports §3)
  NOTIFY-END       end: no notification after its answer, beyond a frame already begun (core §9, §10.3)
  NOTIFY-FORCE     another id's force open: the same (core §6.4, §10.3)
Numbers come from the SPEC registry. The run ends what it opens and releases its plan.

    python -m oep_client.conformance.notify_hw --unit UNIT --channel 20 --spec SPEC --out NEW.json --lock LOCK
"""
import argparse
import json
import os
import secrets
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

from . import Checks, Violation, checker_sources_sha256, require, spec_identity, tlvs
from .usb import UsbWire, frame

DELAY_MS = 100           # NOTIFY-DELAY's max_delay_ms
BATCH_GAP_S = 0.03       # frames closer than this are one batch (a batch may be split to fit max_frame)
QUIET_S = 0.3            # how long "nothing more" is watched


class NotifyWire:
    """Length-framed transfers from a raw USB backend, sorted by role: answers (role 0x02) by corr, notifications
    (0x03 / 0x04) kept with the time they arrived. Nothing is resent or repaired."""
    def __init__(self, backend, registry, max_frame=64):
        self.backend, self.max_frame = backend, max_frame
        self.roles = registry['roles']
        self.pending, self.answers, self.notes = bytearray(), [], []
        self.op_wait_s = 1.0

    def _pump(self, timeout):
        data = self.backend.read(timeout)
        if data is None:
            return
        self.pending.extend(data)
        while len(self.pending) >= 2:
            size = int.from_bytes(self.pending[:2], 'little')
            if size > self.max_frame:
                raise Violation('probe emitted an oversized message')
            if len(self.pending) < size + 2:
                return
            message = bytes(self.pending[2:2 + size])
            del self.pending[:size + 2]
            if not size:
                continue
            if message[0] == self.roles['result']:
                self.answers.append(message)
            else:
                self.notes.append((time.monotonic(), message))

    def send(self, message):
        corr = message[1:3]
        self.backend.write(frame(message))
        deadline = time.monotonic() + max(3.0, self.op_wait_s + 1)
        while time.monotonic() < deadline:
            for k, answer in enumerate(self.answers):
                if answer[1:3] == corr:
                    del self.answers[k]
                    return answer
            self._pump(0.01)
        raise TimeoutError('no answer')

    def watch(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._pump(min(0.01, max(0, end - time.monotonic())))

    def take(self):
        notes, self.notes = self.notes, []
        return notes


def iface(registry, name):
    spec = next(i for i in registry['interface'] if i['name'] == name)
    return spec, {o['name']: o['code'] for o in spec['op']}


def data_frames(notes, fn, roles):
    """(time, seq, position, len) of fn's data frames (core §10.2), in arrival order."""
    out = []
    for at, m in notes:
        if m[0] == roles['data'] and struct.unpack_from('<H', m, 1)[0] == fn:
            seq, position, size = struct.unpack_from('<HQH', m, 3)
            out.append((at, seq, position, size))
    return out


def seqs(notes, fn, roles):
    return [struct.unpack_from('<H', m, 3)[0] for _, m in notes
            if m[0] in (roles['data'], roles['event']) and struct.unpack_from('<H', m, 1)[0] == fn]


class Run:
    def __init__(self, check, wire, channel, peer_open):
        self.c, self.w, self.channel, self.peer_open = check, wire, channel, peer_open
        reg = check.reg
        self.roles = reg['roles']
        self.cap, self.cap_ops = iface(reg, 'oep.fixture.logic')
        _, self.plan_ops = iface(reg, 'oep.probe.plan')
        plan_spec, _ = iface(reg, 'oep.probe.plan')
        self.assignment = plan_spec['tlv']['plan_apply']['role_assignment']
        entries = check.list_snapshot()
        self.fn = next(e['fn'] for e in entries if e['name'] == 'oep.fixture.logic')
        self.plan_fn = next(e['fn'] for e in entries if e['name'] == 'oep.probe.plan')
        self.results, self.observed = [], {}
        rows = dict(reversed(check.describe(self.fn)))
        self.rate = struct.unpack_from('<I', rows[self.cap['tlv']['describe']['rate_range']])[0]   # min_hz
        self.observed['rate_hz'] = self.rate

    def req(self, op, payload=b'', fn=None, session=None):
        return self.c.request(op, payload, session=self.c.session if session is None else session,
                              fn=self.fn if fn is None else fn)

    def ok(self, op, payload=b'', fn=None):
        return self.c.success(self.req(op, payload, fn))

    def open(self, force=0):
        sid = secrets.randbelow(0xffffffff) + 1
        self.c.corr = 0
        p = self.c.success(self.c.request(self.c.core['open'], struct.pack('<IB', 60000, force), session=sid))
        require(len(p) >= 8, 'open payload')
        self.c.session = sid
        return sid

    def end(self):
        tlvs(self.c.success(self.c.request(self.c.core['end'], session=self.c.session)))
        self.c.session = None

    def stream(self, min_bytes=0, max_delay=0):
        """plan the channel as role 0, configure streaming, subscribe, start."""
        t = self.cap['tlv']['configure']
        tlvs(self.ok(self.plan_ops['plan_apply'], bytes((self.assignment,)) + struct.pack('<H', 5)
                     + struct.pack('<HBH', self.fn, 0, self.channel), fn=self.plan_fn))
        mode = self.cap['enum']['mode']['streaming']
        body = (bytes((t['mode'],)) + struct.pack('<H', 1) + bytes((mode,))
                + bytes((t['rate'],)) + struct.pack('<HI', 4, self.rate))
        self.ok(self.cap_ops['configure'], body)
        self.subscribe(min_bytes, max_delay)
        self.ok(self.cap_ops['start'])

    def subscribe(self, min_bytes, max_delay):
        tlvs(self.ok(self.cap_ops['subscribe'], struct.pack('<HI', min_bytes, max_delay)))

    def quiet_after(self, name):
        """After an answer that ends the subscription: nothing more beyond a frame already begun."""
        self.w.take()
        self.w.watch(QUIET_S)
        late = data_frames(self.w.take(), self.fn, self.roles)
        self.observed[name] = len(late)
        require(not late, f'{len(late)} data frames after the subscription ended')

    def check(self, name, clause, function):
        row = {'id': name, 'clause': clause}
        try:
            function()
            row['status'] = 'passed'
        except Exception as exc:
            row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        self.results.append(row)
        return row['status'] == 'passed'

    def lock(self):
        reply = self.c.exchange(self.req(self.cap_ops['subscribe'], struct.pack('<HI', 0, 0), session=0))
        require(reply[3] == self.c.reg['resolutions']['rejected'], 'subscribe without a session was completed')

    def none(self):
        tlvs(self.ok(self.cap_ops['unsubscribe']))

    def seq_and_first(self):
        self.stream()
        rounds = []
        for _ in range(5):
            self.w.watch(0.2)
            started = time.monotonic()
            self.c.clock(self.c.session)
            rounds.append(time.monotonic() - started)
        self.observed['clock_round_trips_s'] = rounds
        frames = data_frames(self.w.take(), self.fn, self.roles)
        self.observed['seq_frames'] = len(frames)
        require(len(frames) >= 3, f'only {len(frames)} data frames in a second of streaming')
        require([f[1] for f in frames] == list(range(len(frames))), 'seq not from 0 one by one')
        for a, b in zip(frames, frames[1:]):
            require(b[2] == a[2] + a[3], 'positions not back to back')
        self.first_ok = max(rounds) <= self.w.op_wait_s

    def first(self):
        require(self.first_ok, f'an answer took {max(self.observed["clock_round_trips_s"]):.3f} s while streaming')

    def replace(self):
        self.subscribe(0, 0)
        self.w.take()
        self.w.watch(0.5)
        got = seqs(self.w.take(), self.fn, self.roles)
        require(got and got[0] == 0, f'after subscribing again seq starts at {got[:1]}')

    def delay(self):
        self.subscribe(0, DELAY_MS)
        self.w.take()
        self.w.watch(1.5)
        frames = data_frames(self.w.take(), self.fn, self.roles)
        starts = [f[0] for k, f in enumerate(frames) if k == 0 or f[0] - frames[k - 1][0] > BATCH_GAP_S]
        gaps = [b - a for a, b in zip(starts, starts[1:])][1:]      # the first batch may come early (a part begun)
        self.observed['delay_batch_gaps_s'] = gaps
        require(len(gaps) >= 3, f'{len(starts)} batches in 1.5 s with max_delay_ms {DELAY_MS}')
        require(all(g >= DELAY_MS / 1000 * 0.5 for g in gaps), 'data sent in batches well inside max_delay_ms')
        require(all(g <= DELAY_MS / 1000 * 2 for g in gaps), 'data held well past max_delay_ms')
        self.subscribe(0, 0)

    def route(self):
        peer = self.peer_open()
        try:
            peer.watch(0.5)
            late = peer.take()
            self.observed['peer_notifications'] = len(late)
            require(not late, 'notifications reached a route that did not subscribe')
        finally:
            peer.backend.close()

    def ended(self):
        self.end()
        self.quiet_after('after_end')

    def forced(self):
        self.open()
        self.stream()
        self.w.watch(0.3)
        require(data_frames(self.w.take(), self.fn, self.roles), 'no data before the force')
        self.open(force=1)
        self.quiet_after('after_force')
        self.ok(self.plan_ops['plan_release'], bytes((0,)), fn=self.plan_fn)
        self.end()

    def run(self):
        self.check('NOTIFY-LOCK', 'core §10.3', self.lock)
        self.open()
        self.check('NOTIFY-NONE', 'core §10.3', self.none)
        if self.check('NOTIFY-SEQ', 'core §10.2', self.seq_and_first):
            self.check('NOTIFY-FIRST', 'core §10.4', self.first)
        self.check('NOTIFY-REPLACE', 'core §10.3', self.replace)
        self.check('NOTIFY-DELAY', 'core §10.3', self.delay)
        if self.peer_open is not None:
            self.check('NOTIFY-ROUTE', 'core §10.4; transports §3', self.route)
        self.check('NOTIFY-END', 'core §9, §10.3', self.ended)
        self.check('NOTIFY-FORCE', 'core §6.4, §10.3', self.forced)
        return self.results


def main(argv=None):
    from .. import __version__
    from ..hardware import equipment_lock
    from .usb_hw import BulkBackend, HidBackend, find_device
    parser = argparse.ArgumentParser(description=__doc__)
    for name, env in [('unit', 'OEP_CONFORMANCE_UNIT_ID'), ('spec', 'OEP_CONFORMANCE_SPEC'),
                      ('out', 'OEP_CONFORMANCE_OUT'), ('lock', 'OEP_HW_LOCK')]:
        parser.add_argument('--' + name, default=os.environ.get(env))
    parser.add_argument('--channel', type=int, required=True, help='a free channel the logic capture listens on')
    parser.add_argument('--no-peer', action='store_true', help='skip NOTIFY-ROUTE (no HID route)')
    args = parser.parse_args(argv)
    if not all(getattr(args, name) for name in ('unit', 'spec', 'out', 'lock')):
        parser.error('explicit unit, SPEC checkout, new output file and existing shared lock required')
    registry, identity = spec_identity(args.spec)
    report = {'status': 'failed', 'spec': identity, 'client_version': __version__,
              'checker_sources_sha256': checker_sources_sha256(), 'expected_unit': args.unit,
              'channel': args.channel, 'full_conformance': False,
              'started_at': datetime.now(timezone.utc).isoformat()}
    with Path(args.out).open('x') as artifact:
        try:
            with equipment_lock(args.lock):
                dev = find_device(registry['usb']['project_vid'], registry['usb']['project_pid'], args.unit)
                backend = BulkBackend(dev, registry['usb'])
                try:
                    wire = NotifyWire(backend, registry)
                    check = Checks(wire.send, registry, args.unit)
                    probe = UsbWire(None, 'bulk', **backend.shape)   # holds max_frame for confirm (unused for I/O)
                    check.usb_wire = probe
                    check.confirm()
                    wire.max_frame = probe.max_frame
                    check.identity()

                    def peer():
                        hid = HidBackend(dev, registry['usb'])
                        hid.owner = False
                        return NotifyWire(hid, registry, wire.max_frame)
                    run = Run(check, wire, args.channel, None if args.no_peer else peer)
                    try:
                        report['checks'] = run.run()
                    finally:
                        report['observed'] = run.observed
                        if check.session is not None:
                            try:
                                check.exchange(check.request(check.core['end'], session=check.session))
                            except Exception:
                                pass
                    report['status'] = 'passed' if all(r['status'] == 'passed' for r in report['checks']) else 'failed'
                finally:
                    backend.close()
        except Exception as exc:
            report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        json.dump(report, artifact, ensure_ascii=False, indent=2, default=str)
        artifact.write('\n')
    print(f"{report['status']}: {args.out}" + (f" ({report.get('error')})" if report.get('error') else ''))
    for row in report.get('checks', []):
        print(f"  {row['id']}: {row['status']}" + (f" - {row['error']}" if row.get('error') else ''))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
