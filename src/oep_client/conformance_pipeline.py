"""Independent checks of explicitly instrumented route admission equipment."""
import struct
from .conformance import Checks, require, tlvs


class PipelineChecks:
    def __init__(self, checks, adapter):
        self.c, self.a = checks, adapter
        self.peer = Checks(lambda req: adapter.exchange(adapter.peer, req), checks.reg, checks.unit)

    def request(self, checks, size=10):
        require(size == 10 or size >= 13, 'fixture request size')
        tail = b'' if size == 10 else struct.pack('<BH', 0x40, size - 13) + bytes(size - 13)
        return checks.request(checks.core['clock'], tail)

    def pending(self, route, expected):
        raw = self.a.pending(route)
        require(raw == expected, 'unresolved request bytes/order differ')
        limits = self.limits[route]
        require(len(raw) <= limits['max_inflight'] and sum(map(len, raw)) <= limits['window'],
                'accepted requests exceed advertised route bounds')

    def responses(self, route, expected, rejected=()):
        frames = self.a.receive(route)
        require(len(frames) == len(expected), 'missing/extra pipeline response')
        for frame, request in zip(frames, expected):
            corr = struct.unpack_from('<H', request, 1)[0]
            require(frame[:3] == struct.pack('<BH', 2, corr) and len(frame) <= self.limits[route]['max_frame'],
                    'response route/order/corr/length')
            if corr in rejected:
                require(frame[3:5] == bytes((self.c.reg['resolutions']['rejected'], self.c.reasons['window_exceeded'])),
                        'explicit reject-overflow fixture policy not honored')
                tlvs(frame[5:])
            else:
                require(len(frame) >= 17 and frame[3:5] == b'\x01\0' and
                        struct.unpack_from('<I', frame, 5)[0] == self.c.boot, 'clock success/boot')
                tlvs(frame[17:])

    def service(self):
        import time
        start = time.monotonic_ns()
        self.a.service()
        require(time.monotonic_ns() - start <= self.a.service_budget_ms * 1_000_000, 'service fixture budget exceeded')

    def batch(self, route, requests):
        self.a.hold(route, 0)
        self.a.submit([(route, request) for request in requests])
        self.pending(route, requests)
        self.service()
        require(self.a.receive(route) == [], 'paused writer completed a result')
        self.pending(route, requests)
        self.a.hold(route, None)
        self.service()
        self.responses(route, requests)
        self.pending(route, [])

    def count(self):
        route = self.a.primary
        self.batch(route, [self.request(self.c) for _ in range(self.limits[route]['max_inflight'])])

    def window(self):
        route = self.a.primary
        # 64 + 16 reaches the sample's 80-byte window without hitting count=3.
        self.batch(route, [self.request(self.c, 64), self.request(self.c, 16)])

    def isolation(self):
        route, peer = self.a.primary, self.a.peer
        requests = [self.request(self.c) for _ in range(self.limits[route]['max_inflight'])]
        other = [self.request(self.peer) for _ in range(self.limits[peer]['max_inflight'])]
        try:
            self.a.hold(route, 0)
            self.a.submit([(route, x) for x in requests] + [(peer, x) for x in other])
            self.pending(route, requests); self.pending(peer, other)
            self.service()
            self.responses(peer, other); self.pending(peer, [])
            self.pending(route, requests)
            require(self.a.receive(route) == [], 'blocked primary made progress')
            fresh = self.request(self.peer)
            self.a.submit([(peer, fresh)]); self.service(); self.responses(peer, [fresh])
        finally:
            self.a.hold(route, None); self.service()
        self.responses(route, requests); self.pending(route, [])

    def overflow(self, by_bytes=False):
        route = self.a.primary
        requests = ([self.request(self.c, 64), self.request(self.c, 16)] if by_bytes else
                    [self.request(self.c) for _ in range(self.limits[route]['max_inflight'])])
        extra = self.request(self.c)
        try:
            self.a.hold(route, 0)
            self.a.submit([(route, x) for x in requests + [extra]])
            self.pending(route, requests)
            self.service(); self.pending(route, requests)
            require(self.a.receive(route) == [], 'overflow bypassed paused writer')
        finally:
            self.a.hold(route, None); self.service()
        self.responses(route, requests + [extra], (struct.unpack_from('<H', extra, 1)[0],))
        self.pending(route, [])
        self.batch(route, [self.request(self.c)])  # new corr, no assumed retry of overflow

    def partial(self):
        route = self.a.primary
        request = self.request(self.c)
        self.a.hold(route, 7)
        self.a.submit([(route, request)]); self.service()
        require(self.a.receive(route) == [], 'partial result completed')
        self.pending(route, [request])
        self.a.hold(route, None); self.service()
        self.responses(route, [request]); self.pending(route, [])

    def run(self):
        start = len(self.c.results)
        def identify():
            require(self.a.primary != self.a.peer and self.a.admission_policy == 'reject-overflow',
                    'explicit two-route reject-overflow fixture required')
            require(type(self.a.service_budget_ms) is int and 1 <= self.a.service_budget_ms <= 1000,
                    'bounded fixture budget required')
            require(all(callable(getattr(self.a, key, None)) for key in
                        ('pending', 'submit', 'service', 'hold', 'receive', 'exchange')), 'instrumentation missing')
            self.limits = {}
            for route, checks in ((self.a.primary, self.c), (self.a.peer, self.peer)):
                checks.confirm(); checks.identity(); checks.declarations(); checks.interfaces()
                require(all(row['status'] == 'passed' for row in checks.results), 'declaration failed')
                self.limits[route] = checks.observed['confirm'].copy()
            require(self.c.boot == self.peer.boot and self.c.list_snapshot() == self.peer.list_snapshot(),
                    'peer unit/boot/interfaces mismatch')
            p, q = self.limits[self.a.primary], self.limits[self.a.peer]
            require((p['max_frame'], p['window'], p['max_inflight']) == (64, 80, 3) and
                    (q['max_frame'], q['window'], q['max_inflight']) == (64, 96, 2),
                    'sample geometry required to isolate count/byte boundaries')
        self.c.check('CORE-PIPELINE-IDENTITY', 'core §4.4/7.1; explicit sample geometry', identify)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        cases = [('COUNT', self.count), ('WINDOW', self.window), ('ISOLATION', self.isolation),
                 ('COUNT-OVERFLOW', self.overflow), ('WINDOW-OVERFLOW', lambda: self.overflow(True)),
                 ('PARTIAL-RECOVERY', self.partial)]
        for name, case in cases:
            first = len(self.a.trace)
            self.c.check('CORE-PIPELINE-' + name, 'core §4.4; explicit admission policy', case)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        rows = self.c.results[start:]
        return {'status': 'passed' if all(x['status'] == 'passed' for x in rows) else 'failed',
                'full_conformance': False, 'scope': 'explicit instrumented admission fixture',
                'levels': {'core': 'selected pipeline contracts', 'interface': 'declarations only', 'oep-interface': 'not executed'},
                'checks': rows, 'observed': self.c.observed, 'route_limits': getattr(self, 'limits', {}),
                'adapter_trace': self.a.trace, 'peer_exchanges': self.peer.trace,
                'unchecked': ['physical transport', 'permitted silent overflow loss', 'session replay under pressure',
                              'concurrency', 'reboot', 'host pipeline scheduler']}
