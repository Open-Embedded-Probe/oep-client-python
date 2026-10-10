"""Session replay under pressure on explicitly instrumented sample equipment."""
import secrets
import struct
from .conformance import require, tlvs, ops
from .conformance_pipeline import PipelineChecks


class ReplayPressureChecks(PipelineChecks):
    def result(self, frame, request, reason=None):
        require(5 <= len(frame) <= self.c.max_frame and
                frame[:3] == struct.pack('<BH', 2, struct.unpack_from('<H', request, 1)[0]), 'result shape/corr')
        require(frame[3:5] == (b'\x01\0' if reason is None else bytes((0, self.c.reasons[reason]))),
                'unexpected outcome/rejection precedence')
        if reason is not None:
            if reason == 'locked':
                require(len(frame) >= 9 and struct.unpack_from('<I', frame, 5)[0] > 0, 'locked lease')
                tlvs(frame[9:])
            else: tlvs(frame[5:])

    def frames(self, route, requests, reasons=None):
        raw = self.a.receive(route)
        require(len(raw) == len(requests), 'missing/extra replay pressure output')
        for frame, request, reason in zip(raw, requests, reasons or [None] * len(requests)):
            self.result(frame, request, reason)
        return raw

    def send_batch(self, requests):
        self.a.submit([(self.a.primary, req) for req in requests]); self.service()

    def create(self, sid, kind=1):
        return self.c.request(16, bytes((kind,)), session=sid, fn=self.a.functions[0])

    def created(self, frames):
        ids = []
        for frame in frames:
            require(len(frame) == 7, 'sample create response size')
            rid = struct.unpack_from('<H', frame, 5)[0]
            require(rid != 0 and rid not in ids, 'invalid/duplicate resource IDs')
            ids.append(rid)
        return ids

    def state(self):
        value = self.a.state()
        require(all(key in value for key in ('holder', 'cache', 'resources', 'deadline', 'high', 'last')),
                'explicit replay instrumentation missing')
        return value

    def inflight_replay(self):
        with self.c.holding(60000) as sid:
            requests = [self.create(sid) for _ in range(3)]
            try:
                self.a.hold(self.a.primary, 0)
                self.send_batch(requests + [requests[0]])
                self.pending(self.a.primary, requests)
                require(self.a.receive(self.a.primary) == [], 'paused writer completed output')
            finally:
                self.a.hold(self.a.primary, None); self.service()
            frames = self.frames(self.a.primary, requests + [requests[0]])
            ids = self.created(frames[:3])
            require(frames[3] == frames[0], 'replay of in-flight request was rejected or re-executed')
            require(set(self.state()['resources']) == set(ids), 'replay created extra resource')
            before = self.state()
            require(self.c.exchange(requests[0]) == frames[0], 'post-drain replay differs')
            require(self.state() == before, 'replay changed lease/cache/resource state')

    def rejected_cache(self):
        with self.c.holding(60000) as sid:
            fill = [self.c.request(self.c.core['clock'], session=sid) for _ in range(3)]
            extra = self.create(sid)
            try:
                self.a.hold(self.a.primary, 0); self.send_batch(fill + [extra])
                self.pending(self.a.primary, fill)
            finally:
                self.a.hold(self.a.primary, None); self.service()
            frames = self.frames(self.a.primary, fill + [extra], [None] * 3 + ['window_exceeded'])
            before = self.state()
            require(not before['resources'], 'rejected request executed')
            require(self.c.exchange(extra) == frames[-1], 'overflow refusal not replayed after capacity recovered')
            require(self.state() == before, 'rejected replay renewed lease or created resource')
            changed = extra[:-1] + b'\x02'
            self.c.rejected(changed, 'malformed')
            require(self.state() == before, 'changed same-corr request changed state')
            self.created([self.c.exchange(self.create(sid))])
            require(len(self.state()['resources']) == 1, 'new corr did not execute once after recovery')

    def precedence(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid)
            saved = self.c.exchange(request)
            self.created([saved])
            fill = [self.c.request(self.c.core['clock'], session=sid) for _ in range(3)]
            try:
                self.a.hold(self.a.primary, 0); self.send_batch(fill)
                before = self.state()
                changed = request[:-1] + b'\x02'
                unknown_fn = request[:3] + b'\xff\xff' + request[5:]
                unknown_op = request[:5] + b'\xfa' + request[6:]
                zero_corr = request[:1] + b'\0\0' + request[3:]
                other = secrets.randbelow(0xffffffff) + 1
                while other == sid: other = secrets.randbelow(0xffffffff) + 1
                locked = self.c.request(self.c.core['clock'], session=other)
                no_session = self.c.request(16, b'\x01', fn=self.a.functions[0])
                requests = [request, changed, unknown_fn, unknown_op, zero_corr, locked, no_session]
                self.send_batch(requests)
                self.pending(self.a.primary, fill)
                require(self.state() == before, 'pre-admission replay/header/session refusal changed state')
            finally:
                self.a.hold(self.a.primary, None); self.service()
            reasons = [None, 'malformed', 'unknown_function', 'unknown_operation', 'malformed', 'locked', 'session_required']
            frames = self.frames(self.a.primary, fill + requests, [None] * 3 + reasons)
            require(frames[3] == saved, 'cached success lost to pressure')
            self.pending(self.a.primary, [])

    def peer_cache(self):
        with self.c.holding(60000) as sid:
            requests = [self.create(sid) for _ in range(3)]
            frames = [self.c.exchange(req) for req in requests]
            self.created(frames)
            before = self.state()
            # Matching corr on another connection, but session 0: never move S.
            for index in range(12):
                corr = struct.unpack_from('<H', requests[index % 3], 1)[0]
                self.peer.success(self.peer.request(self.peer.core['clock'], corr=corr))
            require(self.state() == before, 'peer session-0 traffic changed common replay history')
            for req, frame in zip(requests, frames):
                require(self.c.exchange(req) == frame, 'shared cache lost retained request')
            require(self.state() == before, 'replay after peer traffic mutated state')

    def takeover(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid); self.created([self.c.exchange(request)])
            before = self.state()
            self.peer.confirm()
            require(self.peer.boot == self.c.boot and self.state()['holder'] == sid, 'boot/holder changed before force')
            replacement = secrets.randbelow(0xffffffff) + 1
            while replacement == sid: replacement = secrets.randbelow(0xffffffff) + 1
            self.peer.corr = 0
            opened = self.peer.request(self.peer.core['open'], struct.pack('<IB', 60000, 1), session=replacement)
            try:
                value = self.peer.success(opened)
                self.peer.session = replacement
                self.c.session = None  # S ended by verified takeover; end only T.
                require(len(value) >= 8 and struct.unpack_from('<II', value) == (60000, self.c.boot), 'takeover payload')
                tlvs(value[8:])
                after = self.state()
                require(after['last'] == replacement and after['holder'] == replacement and not after['resources'],
                        'takeover retained old session/resources')
                corr = struct.unpack_from('<H', opened, 1)[0]
                require(after['cache'] == [(corr, (opened, b'\x02' + struct.pack('<H', corr) + b'\x01\0' + value))],
                        'global cache did not replace old history with new open')
                require(before['cache'] != after['cache'], 'session history unchanged')
                self.c.rejected(request, 'locked')  # original primary; S never sent on peer
            finally:
                if self.peer.session is not None:
                    tlvs(self.peer.success(self.peer.request(self.peer.core['end'], session=replacement)))
                    self.peer.session = None

    def run(self):
        report = super().run()
        if report['status'] != 'passed': self.c.abort = True
        def instrumentation():
            require(callable(getattr(self.a, 'state', None)), 'state instrumentation required')
            value = self.state()
            require(type(value['deadline']) is int and type(value['high']) is int and
                    isinstance(value['cache'], list) and isinstance(value['resources'], dict),
                    'invalid replay instrumentation fields')
            require(self.a.functions and type(self.a.functions[0]) is int, 'explicit resource function required')
            declared = {entry['fn']: ops(dict((row['tag'], bytes.fromhex(row['value_hex']))
                        for row in entry['describe'])[7]) for entry in self.c.observed['interfaces']}
            require(self.a.functions[0] in declared and {16, 17, 18, 19} <= declared[self.a.functions[0]],
                    'configured resource function missing own operations')
        self.c.check('CORE-PRESSURE-INSTRUMENTATION', 'explicit sample resource/cache observation', instrumentation)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        for name, case in [('INFLIGHT', self.inflight_replay), ('REJECTED', self.rejected_cache),
                           ('PRECEDENCE', self.precedence), ('PEER-CACHE', self.peer_cache), ('TAKEOVER', self.takeover)]:
            first = len(self.a.trace)
            self.c.check('CORE-PRESSURE-' + name, 'core §4.3/5.2/6.1; explicit rejection fixture', case)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        report.update(status='passed' if all(x['status'] == 'passed' for x in self.c.results) else 'failed',
                      checks=self.c.results, adapter_trace=self.a.trace, peer_exchanges=self.peer.trace,
                      scope='explicit admission and session replay pressure fixture')
        report['unchecked'] = ['physical transport', 'silent overflow loss', 'concurrency', 'reboot',
                               'host scheduler', 'lease timing from physical write completion', 'large request/result retention bounds']
        return report
