"""Host routing/sequence/deadline checks, driven by an explicit logical source."""
import hashlib
from pathlib import Path
import struct

from .conformance import Violation, require, spec_identity


def event(fn, seq, kind=1):
    return struct.pack('<BHHB', 3, fn, seq, kind) + (b'x' if kind == 1 else b'unknown shape')


def data(fn, seq):
    return struct.pack('<BHHQH', 4, fn, seq, 0x100000002, 3) + b'abc'


def reply(corr):
    return struct.pack('<BHBB', 2, corr, 1, 0) + b'reply'


class HostNotificationChecks:
    """adapter.run(receive, clock, *, deadline, corr, expected) -> observations.

    corr None adopts a receive pump. Clock/receive use logical milliseconds;
    expected is the initial next notification seq per explicitly adopted fn.
    No transport or private equipment is discovered.
    """
    def __init__(self, adapter, *, spec):
        self.adapter = adapter
        _, self.identity = spec_identity(spec)
        self.observations = []

    def drive(self, frames, *, corr=7, expected=None, flood=False):
        now, reads = [0], []
        def receive(deadline):
            # This guard diagnoses nontermination, without claiming wall-clock timing.
            require(len(reads) < 20, 'reader exceeded bounded logical stimulus')
            require(now[0] <= deadline <= 20, 'reader extended receive deadline')
            now[0] = min(deadline, now[0] + 5)
            frame = frames(len(reads)) if flood else (frames[len(reads)] if len(reads) < len(frames) else None)
            reads.append({'at_ms': now[0], 'deadline_ms': deadline,
                          'frame_hex': frame.hex() if frame is not None else None})
            return frame
        record = {'corr': corr, 'initial_expected': expected or {7: 0}, 'deadline_ms': 20,
                  'receive': reads}
        self.observations.append(record)
        try:
            result = self.adapter.run(receive, lambda: now[0], deadline=20, corr=corr,
                                      expected=dict(record['initial_expected']))
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            record['ended_at_ms'] = now[0]
        require(isinstance(result, dict), 'adapter observation mapping required')
        record['observed'] = {k: (v.hex() if isinstance(v, bytes) else
                                 [x.hex() for x in v] if k == 'notifications' else v)
                              for k, v in result.items()}
        return result, now[0]

    def demux(self):
        unknown_roles = (0, *range(5, 256))
        # fn 0 never sends notifications (core §10.2): such frames are not adopted and leave fn seqs alone.
        for irrelevant in [struct.pack('<BHH', role, 7, 0) for role in unknown_roles] + [reply(8), struct.pack('<BHH', 1, 7, 0),
                                                                                         event(0, 0), data(0, 1)]:
            known, stream = event(7, 0), data(7, 1)
            result, _ = self.drive([irrelevant, known, stream, reply(7)])
            require(result['reply'] == reply(7) and result['notifications'] == [known, stream],
                    'notification fn treated as corr or stale/unknown frame accepted')
            require(result['expected'] == {7: 2} and result['gaps'] == {7: 0}, 'demux changed notification seq')

    def unknown_seq(self):
        for kind in (0, *range(2, 256)):
            known, unknown, stream, final = event(7, 65534), event(7, 65535, kind), data(7, 0), event(7, 1)
            result, _ = self.drive([known, unknown, stream, final], corr=None, expected={7: 65534})
            require(result['reply'] is None and result['notifications'] == [known, stream, final], 'unknown kind delivered or known notification lost')
            require(result['expected'] == {7: 2} and result['gaps'] == {7: 0}, 'unknown kind not counted, or event/data seq separated')

    def loss(self):
        known, unknown, stream, other = event(7, 0), event(7, 2, 127), data(7, 3), event(8, 0)
        result, _ = self.drive([known, unknown, stream, other], corr=None, expected={7: 0, 8: 0})
        require(result['notifications'] == [known, stream, other] and result['expected'] == {7: 4, 8: 1}, 'fn seq conflated')
        require(result['gaps'] == {7: 1, 8: 0}, 'unknown event created a false gap or concealed a real gap')

    def deadline(self, corr):
        for unknown in (False, True):
            def flood(index):
                return event(7, index, 127 if unknown else 1) if index % 2 == 0 else data(7, index)
            result, ended = self.drive(flood, corr=corr, flood=True)
            require(ended <= 20 and result['reply'] is None, 'notifications extended response/pump deadline')
            require(result['expected'][7] > 0 and result['gaps'][7] == 0, 'flood was not processed')

    def ignored_deadline(self):
        for corr in (7, None):
            for ignored in (struct.pack('<BHH', 0, 7, 0), struct.pack('<BHH', 250, 7, 0),
                            struct.pack('<BHH', 1, 7, 0), reply(8)):
                result, ended = self.drive(lambda _: ignored, corr=corr, flood=True)
                require(ended <= 20 and result['reply'] is None and result['notifications'] == [],
                        'ignored traffic extended deadline or became a response/notification')
                require(result['expected'] == {7: 0} and result['gaps'] == {7: 0},
                        'ignored traffic changed notification seq')

    def unusable(self, frame):
        try:
            result, _ = self.drive([frame])
        except Violation:
            raise  # A stimulus/checker failure is not a reader corruption verdict.
        except ValueError:
            return
        require(result.get('broken_frames', 0) >= 1 and result['reply'] is None and
                result['notifications'] == [], 'broken frame used or not classified as broken')

    def short_frames(self):
        for full, minimum in ((reply(7), 5), (event(7, 0), 7), (data(7, 0), 15)):
            for size in range(1, minimum):
                self.unusable(full[:size])

    def broken_notifications(self):
        stream = data(7, 0)
        self.unusable(stream[:13] + b'\x04\x00' + b'abc')
        for base in (event(7, 0), stream):
            for tail in (b'\x7f', b'\x7f\x00', b'\x7f\x02\x00x',
                         b'\xff\x02\x00x', b'\x00\x00\x00', b'\x80\x00\x00'):
                self.unusable(base + tail)

    def variable_notifications(self):
        for size in (0, 1, 255, 256):
            stream = struct.pack('<BHHQH', 4, 7, 0, 0xffffffffffffffff, size) + b'x' * size
            # Unknown TLVs, including high-bit aliases, are structurally valid.
            stream += b'\x7f\x00\x00\x81\x01\x00z'
            known = event(7, 1) + b'\x7f\x00\x00\x81\x01\x00z'
            unknown = event(7, 2, 127) + b'\x7f\xff\xff'
            result, _ = self.drive([stream, known, unknown, reply(7)])
            require(result['reply'] == reply(7) and result['notifications'] == [stream, known],
                    'valid variable data/event or unknown event shape rejected')
            require(result['expected'] == {7: 3} and result['gaps'] == {7: 0},
                    'variable data/event changed sequence')

    def run(self):
        rows = []
        for name, call in [('DEMUX', self.demux), ('UNKNOWN-SEQ', self.unknown_seq), ('LOSS', self.loss),
                           ('WAIT-DEADLINE', lambda: self.deadline(7)), ('PUMP-DEADLINE', lambda: self.deadline(None)), ('IGNORED-DEADLINE', self.ignored_deadline),
                           ('SHORT-FRAMES', self.short_frames), ('BROKEN-NOTIFICATIONS', self.broken_notifications),
                           ('VARIABLE-NOTIFICATIONS', self.variable_notifications)]:
            start = len(self.observations)
            row = {'id': 'HOST-NOTIFY-' + name, 'clause': 'core §2.4/10.1/10.2', 'status': 'passed'}
            try: call()
            except Exception as exc: row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            row['observations'] = self.observations[start:]
            rows.append(row)
        return {'status': 'passed' if all(r['status'] == 'passed' for r in rows) else 'failed',
                'spec': self.identity, 'level': 'core', 'full_conformance': False,
                'subject': 'Host notification reader, explicit logical schedule', 'checks': rows,
                'adapter': {'module': type(self.adapter).__module__, 'name': type(self.adapter).__qualname__},
                'adoption': {'known_kind': 1, 'known_fixed': 'one byte then TLVs', 'fns': [7, 8],
                             'clock': 'logical ms, up to 5 per receive, clipped to requested deadline', 'deadline_ms': 20},
                'sources_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                   for name in ('conformance_host_notifications.py', 'conformance_host_notifications_sample.py')},
                'unchecked': ['production Host integration', 'physical timing/OS scheduling',
                              'transport recovery for malformed frames and other event payload shapes', 'recovery/retry',
                              'external adapter source provenance', 'Probe notification emission']}
