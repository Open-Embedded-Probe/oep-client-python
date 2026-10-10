"""Data/event sequence and batching contracts, using an explicit stimulus adapter."""
import struct
import time

from .conformance import require, tlvs
from .conformance_subscriptions import SubscriptionChecks


DATA_CASES = (
    ('IF-DATA-IMMEDIATE', 'immediate'),
    ('IF-DATA-MINIMUM', 'minimum'),
    ('IF-DATA-DELAY', 'delay'),
    ('IF-DATA-EITHER', 'either'),
    ('IF-DATA-FIRST-BYTE', 'first_byte'),
    ('IF-DATA-EVENT-BYTES', 'event_bytes'),
    ('IF-DATA-SHARED-SEQ', 'shared'),
    ('IF-DATA-FN-SEQ', 'separate'),
    ('IF-DATA-SPLIT', 'split'),
    ('IF-DATA-NEXT-BATCH', 'next_batch'),
    ('IF-DATA-REPLAY', 'data_replay'),
    ('IF-DATA-OPEN', 'data_open'),
    ('IF-DATA-OPEN-REPLAY', 'data_open_replay'),
    ('IF-DATA-END', 'data_end'),
)


class DataChecks(SubscriptionChecks):
    def __init__(self, checks, adapter, *, wait=time.sleep, clock_ms=None):
        super().__init__(checks, adapter, wait=wait)
        self.clock_ms = clock_ms if clock_ms is not None else (lambda: time.monotonic_ns() // 1_000_000)

    def decode(self, frame):
        require(isinstance(frame, bytes) and 5 <= len(frame) <= self.c.max_frame, 'notification frame bounds')
        role, fn, seq = struct.unpack_from('<BHH', frame)
        require(fn != 0 and fn in self.a.functions, 'invalid notification fn')
        if role == 3:
            require(len(frame) >= 6, 'event header')
            return ('event', fn, seq, self.a.decode_event(frame))
        require(role == 4 and len(frame) >= 15, 'data role/fixed payload')
        position, size = struct.unpack_from('<QH', frame, 5)
        require(len(frame) >= 15 + size, 'data len exceeds available bytes')
        tlvs(frame[15 + size:])
        return ('data', fn, seq, position, frame[15:15 + size])

    def feed(self, fn, data):
        record = {'action': 'feed', 'fn': fn, 'data_hex': data.hex(),
                  'started_monotonic_ns': time.monotonic_ns()}
        self.notifications.append(record)
        try:
            position = self.a.feed(fn, data)
            require(type(position) is int and 0 <= position <= 0xffffffffffffffff, 'external stimulus position')
            record['position'] = position
            return position
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, (OSError, TimeoutError)):
                self.c.abort = True
            raise
        finally:
            record['elapsed_ns'] = time.monotonic_ns() - record['started_monotonic_ns']

    def receive(self):
        record = {'action': 'receive', 'observation_ms': self.a.observation_ms,
                  'started_monotonic_ns': time.monotonic_ns(), 'frames_hex': []}
        self.notifications.append(record)
        try:
            frames = self.a.receive(self.a.observation_ms)
            require(isinstance(frames, (list, tuple)) and all(isinstance(x, bytes) for x in frames),
                    'raw notification frame sequence required')
            record['frames_hex'] = [x.hex() for x in frames]
            return [self.decode(frame) for frame in frames]
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, (OSError, TimeoutError)):
                self.c.abort = True
            raise
        finally:
            record['elapsed_ns'] = time.monotonic_ns() - record['started_monotonic_ns']

    def no_data(self):
        require(self.receive() == [], 'data sent before enabled threshold, or unexpected notification')

    def expect_data(self, fn, position, data, seq):
        frames = self.receive()
        require(frames, 'missing data notification')
        combined = bytearray()
        for frame in frames:
            require(frame[:3] == ('data', fn, seq), 'data fn/shared seq/order mismatch')
            require(frame[3] == position + len(combined), 'stream position differs from external stimulus')
            combined.extend(frame[4])
            seq = (seq + 1) & 65535
        require(bytes(combined) == data, 'data bytes lost, duplicated or changed')
        return seq

    def expect_event(self, fn, seq):
        self.marker += 1
        record = {'action': 'event', 'fn': fn, 'marker': self.marker,
                  'started_monotonic_ns': time.monotonic_ns()}
        self.notifications.append(record)
        try:
            self.a.stimulate(fn, self.marker)
            require(self.receive() == [('event', fn, seq, self.marker)], 'event/shared seq/order mismatch')
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, (OSError, TimeoutError)):
                self.c.abort = True
            raise
        finally:
            record['elapsed_ns'] = time.monotonic_ns() - record['started_monotonic_ns']

    def immediate(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            position = self.feed(fn, b'ab')
            seq = self.expect_data(fn, position, b'ab', 0)
            position = self.feed(fn, b'cd')
            self.expect_data(fn, position, b'cd', seq)

    def minimum(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 6, 0)
            position = self.feed(fn, b'ab')
            self.no_data()
            self.wait(.45)  # delay=0 is disabled, not immediate
            self.no_data()
            self.feed(fn, b'cdef')
            self.expect_data(fn, position, b'abcdef', 0)

    def delay(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 0, 400)
            position = self.feed(fn, b'abc')
            self.no_data()  # min_bytes=0 is disabled, not immediate
            self.wait(.45)
            self.expect_data(fn, position, b'abc', 0)

    def either(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 6, 400)
            started = self.clock_ms()
            position = self.feed(fn, b'abcdef')
            self.expect_data(fn, position, b'abcdef', 0)
            require(self.clock_ms() - started < 400, 'minimum timing crossed delay; cannot distinguish OR/AND')
            self.subscribe(sid, fn, 6, 400)
            position = self.feed(fn, b'gh')
            self.no_data()
            self.wait(.45)
            self.expect_data(fn, position, b'gh', 0)

    def first_byte(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 0, 400)
            first = self.clock_ms()
            position = self.feed(fn, b'a')
            self.no_data()
            self.wait(.18)
            last = self.clock_ms()
            self.feed(fn, b'b')
            self.no_data()
            self.wait(.18)
            self.expect_data(fn, position, b'ab', 0)
            now = self.clock_ms()
            self.notifications.append({'action': 'first-byte-timing', 'first_age_ms': now - first,
                                       'last_age_ms': now - last, 'delay_ms': 400})
            require(now - first >= 400 and now - last < 400,
                    'observation timing cannot distinguish first/last byte deadline')

    def event_bytes(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 6, 0)
            position = self.feed(fn, b'ab')
            self.no_data()
            self.expect_event(fn, 0)  # event bytes do not reach min_bytes=6
            self.wait(.45)
            self.no_data()
            self.feed(fn, b'cdef')
            self.expect_data(fn, position, b'abcdef', 1)

    def shared(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            position = self.feed(fn, b'a')
            seq = self.expect_data(fn, position, b'a', 0)
            self.expect_event(fn, seq)
            position = self.feed(fn, b'b')
            self.expect_data(fn, position, b'b', (seq + 1) & 65535)

    def separate(self):
        with self.c.holding(60000) as sid:
            first, second = self.a.functions[:2]
            self.subscribe(sid, first)
            self.subscribe(sid, second)
            position = self.feed(first, b'a')
            seq = self.expect_data(first, position, b'a', 0)
            self.expect_event(second, 0)
            self.expect_event(first, seq)
            position = self.feed(second, b'b')
            self.expect_data(second, position, b'b', 1)

    def split(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            data = bytes(i % 256 for i in range(self.c.max_frame * 2 + 17))
            position = self.feed(fn, data)
            seq = self.expect_data(fn, position, data, 0)
            self.expect_event(fn, seq)

    def next_batch(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 0, 400)
            position = self.feed(fn, b'a')
            self.no_data()
            self.wait(.45)
            seq = self.expect_data(fn, position, b'a', 0)
            position = self.feed(fn, b'b')
            self.no_data()  # previous first-byte deadline must not leak to this batch
            self.wait(.45)
            self.expect_data(fn, position, b'b', seq)
            self.subscribe(sid, fn, 4, 0)
            position = self.feed(fn, b'ab')
            self.no_data()
            self.feed(fn, b'cd')
            seq = self.expect_data(fn, position, b'abcd', 0)
            position = self.feed(fn, b'e')
            self.no_data()
            self.wait(.45)
            self.no_data()
            self.feed(fn, b'fgh')
            self.expect_data(fn, position, b'efgh', seq)

    def open_buffer(self, replay):
        with self.c.holding(60000) as sid:
            opened = self.c.request(self.c.core['open'], struct.pack('<IB', 60000, 0), session=sid)
            original = self.c.success(opened)
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 6, 400)
            position = self.feed(fn, b'ab')
            self.no_data()
            self.expect_event(fn, 0)
            if not replay:
                opened = self.c.request(self.c.core['open'], struct.pack('<IB', 60000, 0), session=sid)
            require(self.c.success(opened) == original, 'same-session open response changed')
            self.wait(.45)
            seq = self.expect_data(fn, position, b'ab', 1)
            self.expect_event(fn, seq)

    def data_open(self):
        self.open_buffer(False)

    def data_open_replay(self):
        self.open_buffer(True)

    def data_replay(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            request = self.subscribe(sid, fn, 6, 400)
            position = self.feed(fn, b'ab')
            self.no_data()
            self.expect_event(fn, 0)
            tlvs(self.c.success(request))
            self.wait(.45)
            seq = self.expect_data(fn, position, b'ab', 1)
            self.expect_event(fn, seq)

    def data_end(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn, 6, 400)
            self.feed(fn, b'old')
            self.no_data()
            tlvs(self.c.success(self.c.request(self.c.core['end'], session=sid)))
            self.c.session = None
            self.wait(.45)
            self.no_data()
        with self.c.holding(60000) as sid:
            self.subscribe(sid, fn)
            position = self.feed(fn, b'new')
            self.expect_data(fn, position, b'new', 0)

    def run(self):
        start = len(self.c.results)
        def adapter():
            require(callable(self.clock_ms), 'explicit clock must be callable')
            require(callable(getattr(self.a, 'feed', None)), 'explicit external data stimulus required')
            require(getattr(self.a, 'loss_free', None) is True,
                    'adapter must explicitly declare this stimulus/observation setup loss-free')
            require(type(self.a.observation_ms) is int and 1 <= self.a.observation_ms <= 50,
                    'data suite observation window must be 1..50ms')
        self.c.check('IF-DATA-ADAPTER', 'core §11; adapter contract', adapter)
        if self.c.results[-1]['status'] != 'passed':
            self.c.abort = True
        super().run()
        for name, method in DATA_CASES:
            first = len(self.notifications)
            self.c.check(name, 'core §11.2/11.3', getattr(self, method))
            self.c.results[-1]['notification_observations'] = self.notifications[first:]
        rows = self.c.results[start:]
        return {'status': 'passed' if all(row['status'] == 'passed' for row in rows) else 'failed',
                'full_conformance': False, 'scope': 'subscription lifetime, data/event shared seq and batching',
                'levels': {'core': 'selected session/lifetime contracts',
                           'interface': 'selected subscription, data/event and batching contracts',
                           'oep-interface': 'not executed'},
                'observed': self.c.observed, 'checks': rows,
                'unchecked': ['seq wrap in recorded run', 'response priority', 'notification route/closure',
                              'transport queue bound', 'drop consumes seq', 'physical asynchronous behavior',
                              'unconfigured fn', 'reboot in recorded run']}
