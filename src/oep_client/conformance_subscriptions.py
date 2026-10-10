"""Common subscription lifetime verdicts; external event stimulus is explicit."""
import secrets
import struct
import time

from .conformance import ops, require, tlvs


SUBSCRIPTION_CASES = (
    ('IF-SUB-NONE', 'none'),
    ('IF-SUB-SESSION', 'session_required'),
    ('IF-SUB-OWNER', 'owner'),
    ('IF-SUB-FN', 'isolation'),
    ('IF-SUB-REPLACE', 'replace'),
    ('IF-SUB-REPLAY', 'replay'),
    ('IF-SUB-UNSUBSCRIBE', 'unsubscribe'),
    ('IF-SUB-UNSUBSCRIBE-REPLAY', 'unsubscribe_replay'),
    ('IF-SUB-OPEN', 'open_retain'),
    ('IF-SUB-OPEN-REPLAY', 'open_replay'),
    ('IF-SUB-END', 'end'),
    ('IF-SUB-EXPIRY', 'expiry'),
    ('IF-SUB-FORCE', 'force'),
    ('IF-SUB-INVALID', 'invalid'),
    ('IF-SUB-EVENT-THRESHOLD', 'threshold'),
)


class SubscriptionChecks:
    def __init__(self, checks, adapter, *, wait=time.sleep):
        self.c, self.a, self.wait = checks, adapter, wait
        self.notifications = []
        self.marker = 0

    def subscribe(self, sid, fn, minimum=0, delay=0):
        request = self.c.request(1, struct.pack('<HI', minimum, delay), session=sid, fn=fn)
        tlvs(self.c.success(request))
        return request

    def unsub(self, sid, fn):
        request = self.c.request(2, session=sid, fn=fn)
        tlvs(self.c.success(request))
        return request

    def event(self, fn, seq=None):
        self.marker += 1
        marker = self.marker
        record = {'stimulus_fn': fn, 'marker': marker, 'observation_ms': self.a.observation_ms,
                  'started_monotonic_ns': time.monotonic_ns(), 'frames_hex': []}
        self.notifications.append(record)
        try:
            self.a.stimulate(fn, marker)
            frames = self.a.receive(self.a.observation_ms)
            require(isinstance(frames, (list, tuple)), 'adapter must return raw frame sequence')
            # Keep every raw frame before validation, including malformed output.
            require(all(isinstance(frame, bytes) for frame in frames), 'adapter raw frames must be bytes')
            record['frames_hex'] = [frame.hex() for frame in frames]
            parsed = []
            for frame in frames:
                require(6 <= len(frame) <= self.c.max_frame, 'event length outside negotiated bounds')
                role, received_fn, received_seq = struct.unpack_from('<BHH', frame)
                require(role == 3, 'expected event role (this adapter does not test data)')
                require(received_fn != 0 and received_fn in self.a.functions, 'invalid notification fn')
                observed_marker = self.a.decode_event(frame)
                parsed.append((received_fn, received_seq, observed_marker))
            expected = [] if seq is None else [(fn, seq, marker)]
            require(parsed == expected, f'notification mismatch: expected {expected}, got {parsed}')
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, (OSError, TimeoutError)):
                self.c.abort = True
            raise
        finally:
            record['elapsed_ns'] = time.monotonic_ns() - record['started_monotonic_ns']

    def none(self):
        for fn in self.a.functions:
            self.event(fn)

    def session_required(self):
        for fn in self.a.functions:
            self.c.rejected(self.c.request(1, struct.pack('<HI', 0, 0), fn=fn), 'session_required')
            self.c.rejected(self.c.request(2, fn=fn), 'session_required')
            self.event(fn)

    def owner(self):
        with self.c.holding() as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            self.event(fn, 0)
            foreign = sid % 0xffffffff + 1
            self.c.rejected(self.c.request(1, struct.pack('<HI', 0, 0), session=foreign, fn=fn), 'locked')
            self.c.rejected(self.c.request(2, session=foreign, fn=fn), 'locked')
            self.event(fn, 1)

    def isolation(self):
        with self.c.holding() as sid:
            first, second = self.a.functions[:2]
            self.subscribe(sid, first)
            self.event(second)
            self.event(first, 0)
            self.subscribe(sid, second)
            self.event(second, 0)
            self.event(first, 1)
            self.unsub(sid, first)
            self.event(first)
            self.event(second, 1)

    def replace(self):
        with self.c.holding() as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            self.event(fn, 0)
            self.event(fn, 1)
            self.subscribe(sid, fn, 65535, 0xffffffff)
            self.event(fn, 0)
            self.event(fn, 1)  # exactly one subscription remains

    def replay(self):
        with self.c.holding() as sid:
            fn = self.a.functions[0]
            request = self.subscribe(sid, fn)
            self.event(fn, 0)
            tlvs(self.c.success(request))
            self.event(fn, 1)
            self.event(fn, 2)

    def unsubscribe(self):
        with self.c.holding() as sid:
            fn = self.a.functions[0]
            self.unsub(sid, fn)
            self.event(fn)
            self.subscribe(sid, fn)
            self.event(fn, 0)
            self.unsub(sid, fn)
            self.event(fn)
            self.unsub(sid, fn)
            self.event(fn)
            self.subscribe(sid, fn)
            self.event(fn, 0)

    def unsubscribe_replay(self):
        with self.c.holding() as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            request = self.unsub(sid, fn)
            self.event(fn)
            self.subscribe(sid, fn)
            self.event(fn, 0)
            tlvs(self.c.success(request))
            self.event(fn, 1)  # cached old unsubscribe must not remove new subscription

    def open_retain(self):
        with self.c.holding() as sid:
            for fn in self.a.functions:
                self.subscribe(sid, fn)
                self.event(fn, 0)
            value = self.c.success(self.c.request(self.c.core['open'], struct.pack('<IB', 3000, 0), session=sid))
            require(len(value) >= 8 and struct.unpack_from('<II', value) == (3000, self.c.boot), 'open payload')
            tlvs(value[8:])
            for fn in self.a.functions:
                self.event(fn, 1)

    def open_replay(self):
        with self.c.holding() as sid:
            request = self.c.request(self.c.core['open'], struct.pack('<IB', 3000, 0), session=sid)
            original = self.c.success(request)
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            self.event(fn, 0)
            require(self.c.success(request) == original, 'open replay changed')
            self.event(fn, 1)

    def ended(self, kind):
        with self.c.holding(1000 if kind == 'expiry' else 3000) as sid:
            for fn in self.a.functions:
                self.subscribe(sid, fn)
                self.event(fn, 0)
            if kind == 'end':
                tlvs(self.c.success(self.c.request(self.c.core['end'], session=sid)))
                self.c.session = None
            elif kind == 'expiry':
                self.wait(1.1)
                value = self.c.success(self.c.request(self.c.core['lock_state']))
                require(len(value) >= 5 and struct.unpack_from('<BI', value) == (0, 0), 'lease did not expire')
                tlvs(value[5:])
                self.c.session = None
            else:
                replacement = secrets.randbelow(0xffffffff) + 1
                while replacement == sid:
                    replacement = secrets.randbelow(0xffffffff) + 1
                self.c.corr = 0
                value = self.c.success(self.c.request(self.c.core['open'], struct.pack('<IB', 3000, 1),
                                                    session=replacement))
                self.c.session = replacement
                require(len(value) >= 8 and struct.unpack_from('<II', value) == (3000, self.c.boot), 'force open payload')
                tlvs(value[8:])
            for fn in self.a.functions:
                self.event(fn)
            if kind == 'force':
                fn = self.a.functions[0]
                self.subscribe(self.c.session, fn)
                self.event(fn, 0)

    def end(self):
        self.ended('end')

    def expiry(self):
        self.ended('expiry')

    def force(self):
        self.ended('force')

    def invalid(self):
        with self.c.holding() as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            self.event(fn, 0)
            seq = 1
            for size in range(6):
                self.c.rejected(self.c.request(1, b'\0' * size, session=sid, fn=fn), 'malformed')
                self.event(fn, seq)
                seq += 1
            for tail in (b'\0\0\0', b'\x7e', b'\x7e\x01\0'):
                self.c.rejected(self.c.request(1, struct.pack('<HI', 0, 0) + tail,
                                               session=sid, fn=fn), 'malformed')
                self.event(fn, seq)
                seq += 1
            request = self.c.request(1, struct.pack('<HI', 65535, 1) + b'\xfe\0\0', session=sid, fn=fn)
            payload = self.c.rejected(request, 'unsupported')
            require(payload and payload[0] == 254, 'unsupported must identify critical tag')
            tlvs(payload[1:])
            self.event(fn, seq)
            self.c.rejected(self.c.request(2, b'\0', session=sid, fn=fn), 'malformed')
            self.event(fn, seq + 1)

    def threshold(self):
        with self.c.holding() as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 65535, 0xffffffff)
            self.event(fn, 0)
            self.event(fn, 1)

    def run(self):
        start = len(self.c.results)
        def identify():
            self.c.confirm()
            self.c.identity()
            self.c.declarations()
            first = len(self.c.results)
            self.c.interfaces()
            require(all(x['status'] == 'passed' for x in self.c.results[first:]), 'interface declaration failure')
            require(2 <= len(self.a.functions) <= 8 and len(set(self.a.functions)) == len(self.a.functions),
                    'explicit distinct notification fn required')
            require(type(self.a.observation_ms) is int and 1 <= self.a.observation_ms <= 100,
                    'observation window must be 1..100ms for this bounded stimulus suite')
            declared = {x['fn']: ops(dict(reversed([(row['tag'], bytes.fromhex(row['value_hex']))
                        for row in x['describe']]))[self.c.reg['describe_common']['ops']])
                        for x in self.c.observed['interfaces']}
            require(all(type(fn) is int and fn in declared and {1, 2} <= declared[fn]
                        for fn in self.a.functions), 'configured fn must declare subscribe/unsubscribe')
            require(all(callable(getattr(self.a, name, None)) for name in
                        ('stimulate', 'receive', 'decode_event')), 'adapter event capabilities missing')
        self.c.check('IF-SUB-IDENTITY', 'core §7/11; adapter contract', identify)
        if self.c.results[-1]['status'] != 'passed':
            self.c.abort = True
        for name, method in SUBSCRIPTION_CASES:
            first = len(self.notifications)
            self.c.check(name, 'core §6/9/11.2/11.3', getattr(self, method))
            self.c.results[-1]['notification_observations'] = self.notifications[first:]
        rows = self.c.results[start:]
        return {'status': 'passed' if all(x['status'] == 'passed' for x in rows) else 'failed',
                'full_conformance': False, 'scope': 'subscription lifetime and externally stimulated events',
                'levels': {'core': 'lifetime checks only', 'interface': 'selected common subscription behavior',
                           'oep-interface': 'not executed'},
                'observed': self.c.observed, 'checks': rows,
                'unchecked': ['data/event shared seq', 'seq wrap', 'data thresholds/delay', 'response priority',
                              'notification route and closure', 'max_frame*2 queue bound', 'drop consumes seq',
                              'physical asynchronous behavior', 'reboot', 'unconfigured fn']}
