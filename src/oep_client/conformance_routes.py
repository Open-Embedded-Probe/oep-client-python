"""Selected writer/route contracts on explicit instrumented equipment."""
import secrets
import struct
import time
from .conformance import Checks, ops, require, tlvs


ROUTE_CASES = (
    ('IF-ROUTE-RESULT', 'results'),
    ('IF-ROUTE-NOTIFY', 'notify'),
    ('IF-ROUTE-PRIORITY', 'priority'),
    ('IF-ROUTE-QUEUE-DROP', 'drop'),
    ('IF-ROUTE-PARTIAL', 'partial'),
    ('IF-ROUTE-CLOSE', 'close'),
)


class RouteChecks:
    def __init__(self, checks, adapter):
        self.c, self.a = checks, adapter
        self.peer = Checks(lambda req: adapter.exchange(adapter.peer, req), checks.reg, checks.unit)

    def subscribe(self, sid, fn):
        tlvs(self.c.success(self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=fn)))

    def service(self):
        started = time.monotonic_ns()
        self.a.service()
        require(time.monotonic_ns() - started <= self.a.service_budget_ms * 1_000_000,
                'stalled writer exceeded declared service observation budget')

    def event(self, frame, fn, seq, marker):
        require(10 <= len(frame) <= self.c.max_frame, 'event length')
        require(frame[:10] == struct.pack('<BHHBI', 3, fn, seq, 1, marker), 'event route/seq/content')
        tlvs(frame[10:])

    def data(self, frame, fn, seq, position, payload):
        require(len(frame) <= self.c.max_frame, 'data exceeds max_frame')
        require(frame[:15] == struct.pack('<BHHQH', 4, fn, seq, position, len(payload)), 'data header/seq/position')
        require(frame[15:15 + len(payload)] == payload, 'data content')
        tlvs(frame[15 + len(payload):])

    def result(self, frame, request):
        corr = struct.unpack_from('<H', request, 1)[0]
        require(5 <= len(frame) <= self.c.max_frame and frame[:5] == struct.pack('<BHBB', 2, corr, 1, 0),
                'result routing/corr/order/outcome')
        if request[5] == 4:
            require(len(frame) >= 17 and struct.unpack_from('<I', frame, 5)[0] == self.c.boot, 'clock boot/payload')
            tlvs(frame[17:])
        else:
            tlvs(frame[5:])

    def bounded(self, route):
        parts = self.a.queue(route)
        require(isinstance(parts, (list, tuple)) and all(isinstance(x, bytes) for x in parts),
                'queue instrumentation must expose raw unsent suffixes')
        size = sum(map(len, parts))
        require(size <= self.c.max_frame * 2, 'notification queue exceeds max_frame*2')
        return parts

    def drain(self):
        self.a.hold(self.a.primary, None)
        self.service()
        return self.a.receive(self.a.primary)

    def results(self):
        req = self.c.request(self.c.core['clock'], corr=1450)
        self.a.submit([(self.a.primary, req), (self.a.peer, req)])
        self.service()
        for route in (self.a.primary, self.a.peer):
            frames = self.a.receive(route)
            require(len(frames) == 1, 'response missing, duplicated or leaked across routes')
            self.result(frames[0], req)

    def notify(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            self.a.stimulate(fn, 101)
            self.service()
            frames = self.a.receive(self.a.primary)
            require(len(frames) == 1, 'expected one notification')
            self.event(frames[0], fn, 0, 101)
            require(self.a.receive(self.a.peer) == [], 'notification leaked to peer')

    def priority(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            try:
                self.a.hold(self.a.primary, 0)
                self.a.stimulate(fn, 102)
                one = self.c.request(self.c.core['clock'], session=sid)
                two = self.c.request(self.c.core['keepalive'], session=sid)
                peer = self.peer.request(self.peer.core['clock'])
                self.a.submit([(self.a.primary, one), (self.a.peer, peer), (self.a.primary, two)])
                self.service()
                require(self.a.receive(self.a.primary) == [], 'blocked writer emitted bytes')
                received = self.a.receive(self.a.peer)
                require(len(received) == 1, 'blocked primary stopped peer')
                self.result(received[0], peer)
                self.bounded(self.a.primary)
                frames = self.drain()
                require(len(frames) == 3, 'missing/extra output')
                self.result(frames[0], one)
                self.result(frames[1], two)
                self.event(frames[2], fn, 0, 102)
            finally:
                self.drain()  # unblock before holding() sends end

    def drop(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            expected = {}
            try:
                self.a.hold(self.a.primary, 0)
                for seq in range(32):
                    if seq % 2:
                        self.a.stimulate(fn, seq)
                        expected[seq] = ('event', seq)
                    else:
                        data = bytes((seq,)) * (self.c.max_frame - 15)
                        position = self.a.feed(fn, data)
                        expected[seq] = ('data', position, data)
                    self.bounded(self.a.primary)
                self.service()
                require(self.a.receive(self.a.primary) == [], 'stalled writer made progress')
                self.peer.clock()  # peer remains usable while primary is stalled
                frames = self.drain()
                require(len(frames) < 32, 'pressure fixture did not cause notification drop')
                previous = -1
                for frame in frames:
                    seq = struct.unpack_from('<H', frame, 3)[0]
                    require(previous < seq < 32, 'duplicate/out-of-order/undefined seq under pressure')
                    previous = seq
                    item = expected[seq]
                    if item[0] == 'event': self.event(frame, fn, seq, item[1])
                    else: self.data(frame, fn, seq, item[1], item[2])
                self.a.stimulate(fn, 103)
                self.service()
                frames = self.a.receive(self.a.primary)
                require(len(frames) == 1, 'post-drop notification missing')
                self.event(frames[0], fn, 32, 103)
                require(self.a.receive(self.a.peer) == [], 'push leaked to peer')
            finally:
                self.drain()

    def partial(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            self.subscribe(sid, fn)
            data = b'x' * (self.c.max_frame - 15)
            try:
                position = self.a.feed(fn, data)
                full = struct.pack('<BHHQH', 4, fn, 0, position, len(data)) + data
                self.a.hold(self.a.primary, 7)
                self.service()
                require(self.a.receive(self.a.primary) == [], 'incomplete frame treated as complete')
                require(self.bounded(self.a.primary) == [full[7:]], 'partial bytes not measured as unsent suffix')
                self.a.hold(self.a.primary, 0)
                second = self.a.feed(fn, data)
                self.a.stimulate(fn, 104)  # cannot fit after 57 + 64 bytes at max_frame=64
                self.bounded(self.a.primary)
                request = self.c.request(self.c.core['clock'], session=sid)
                self.a.submit([(self.a.primary, request)])
                frames = self.drain()
                require(len(frames) == 3, 'partial frame/result/queued data/drop mismatch')
                self.data(frames[0], fn, 0, position, data)  # finish an already started frame
                self.result(frames[1], request)  # before the next notification
                self.data(frames[2], fn, 1, second, data)
                self.a.stimulate(fn, 105)
                self.service()
                final = self.a.receive(self.a.primary)
                require(len(final) == 1, 'partial/drop continuation missing')
                self.event(final[0], fn, 3, 105)
            finally:
                self.drain()

    def close(self):
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            rid = self.a.create_resource(self.c, sid)
            self.subscribe(sid, fn)
            before = self.a.state()
            self.a.close(self.a.primary)
            self.c.session = None  # S is never sent on a different connection
            after = self.a.state()
            require(after == before, 'route close changed session/resources/subscription/replay history')
            self.peer.clock()
            locked = self.peer.success(self.peer.request(self.peer.core['lock_state']))
            require(len(locked) >= 5 and locked[0] == 1 and struct.unpack_from('<I', locked, 1)[0] > 0,
                    'route close released lock')
            tlvs(locked[5:])
            self.a.stimulate(fn, 106)
            self.service()
            require(self.a.receive(self.a.primary) == [] and self.a.receive(self.a.peer) == [],
                    'closed-route push sent or redirected')
            require(self.a.state()['subscriptions'][fn] == before['subscriptions'][fn] + 1,
                    'closed-route drop did not preserve subscription/consume seq')
            self.peer.confirm()
            require(self.peer.boot == self.c.boot and self.a.state()['holder'] == sid,
                    'boot or holder changed before takeover')
            replacement = secrets.randbelow(0xffffffff) + 1
            while replacement == sid: replacement = secrets.randbelow(0xffffffff) + 1
            self.peer.corr = 0
            try:
                value = self.peer.success(self.peer.request(self.peer.core['open'], struct.pack('<IB', 60000, 1),
                                                          session=replacement))
                self.peer.session = replacement
                require(len(value) >= 8 and struct.unpack_from('<II', value) == (60000, self.c.boot), 'takeover payload')
                tlvs(value[8:])
                state = self.a.state()
                require(not state['resources'] and not state['subscriptions'], 'takeover retained old resources')
                require(rid not in state['resources'], 'old resource still live')
            finally:
                if self.peer.session is not None:
                    tlvs(self.peer.success(self.peer.request(self.peer.core['end'], session=replacement)))
                    self.peer.session = None

    def run(self):
        start = len(self.c.results)
        def identify():
            require(self.a.primary != self.a.peer, 'two explicit routes required')
            require(type(self.a.service_budget_ms) is int and 1 <= self.a.service_budget_ms <= 1000,
                    'explicit bounded service observation budget required')
            require(all(callable(getattr(self.a, name, None)) for name in
                        ('exchange', 'hold', 'submit', 'service', 'receive', 'queue', 'stimulate', 'feed',
                         'close', 'state', 'create_resource')), 'instrumentation capabilities missing')
            for checks in (self.c, self.peer):
                checks.confirm(); checks.identity(); checks.declarations(); checks.interfaces()
                require(all(row['status'] == 'passed' for row in checks.results), 'declaration failure')
            require(self.c.boot == self.peer.boot and self.c.list_snapshot() == self.peer.list_snapshot(),
                    'peer identity/boot/interfaces differ')
            declared = {entry['fn']: ops(dict((row['tag'], bytes.fromhex(row['value_hex']))
                         for row in entry['describe'])[7]) for entry in self.c.observed['interfaces']}
            require(self.a.functions and all(type(fn) is int and fn in declared and {1, 2} <= declared[fn]
                                             for fn in self.a.functions), 'configured notification fn invalid')
        self.c.check('IF-ROUTE-IDENTITY', 'core §7; transports §3', identify)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        for name, method in ROUTE_CASES:
            first, peer_first = len(self.a.trace), len(self.peer.trace)
            def execute(method=method):
                try: getattr(self, method)()
                except (OSError, TimeoutError):
                    self.c.abort = True
                    raise
            self.c.check(name, 'core §11.4; transports §3', execute)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
            self.c.results[-1]['peer_exchanges'] = self.peer.trace[peer_first:]
        rows = self.c.results[start:]
        return {'status': 'passed' if all(x['status'] == 'passed' for x in rows) else 'failed',
                'full_conformance': False, 'scope': 'instrumented logical routes and message writer',
                'levels': {'core': 'selected route/session/writer contracts',
                           'interface': 'selected notification/resource behavior', 'oep-interface': 'not executed'},
                'checks': rows, 'observed': self.c.observed,
                'adapter_trace': self.a.trace, 'peer_exchanges': self.peer.trace,
                'unchecked': ['physical framing/OS buffers', 'per-route window/max_inflight',
                              'real TCP/USB disconnect', 'concurrent scheduling', 'unconfigured routes', 'reboot']}
