"""Independent bounded replay history checks on explicit sample equipment."""
import struct
from .conformance import require, tlvs, ops
from .conformance_replay_pressure import ReplayPressureChecks


class RetentionChecks(ReplayPressureChecks):
    def create(self, sid, kind=1, reply_size=7, size=12, corr=None):
        require(size == 12 or size >= 15, 'sample create request size')
        tail = b'' if size == 12 else struct.pack('<BH', 0x40, size - 15) + bytes(size - 15)
        return self.c.request(16, bytes((kind, reply_size)) + tail, session=sid,
                              fn=self.a.functions[0], corr=corr)

    def state(self):
        value = super().state()
        require(type(value.get('next_resource_id')) is int, 'resource allocator instrumentation required')
        return value

    def created_one(self, request, size=7):
        frame = self.c.exchange(request)
        self.result(frame, request)
        require(len(frame) == size, 'sample response size')
        rid = struct.unpack_from('<H', frame, 5)[0]
        require(rid != 0, 'zero resource ID'); tlvs(frame[7:])
        return frame, rid

    def record(self, request, wanted_request, wanted_response):
        corr = struct.unpack_from('<H', request, 1)[0]
        history = dict(self.state()['cache'])
        require(history.get(corr) == (wanted_request, wanted_response), 'retention marker/full bytes mismatch')
        require(self.state()['high'] >= corr, 'lost high-water mark')

    def replay_unchanged(self, request, saved=None, reason=None):
        before = self.state(); self.a.wait_ms(5)
        if reason is None: require(self.c.exchange(request) == saved, 'retained response changed')
        else: self.c.rejected(request, reason)
        require(self.state() == before, 'replay/lost result changed deadline/history/resource allocator')

    def request_boundary(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid, size=16)
            saved, rid = self.created_one(request)
            self.record(request, request, saved)
            self.replay_unchanged(request, saved)
            changed = request[:10] + b'\x02' + request[11:]
            self.replay_unchanged(changed, reason='malformed')
            require(set(self.state()['resources']) == {rid}, 'changed retained request executed')

    def request_lost(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid, size=17)
            saved, rid = self.created_one(request)
            self.record(request, None, saved)
            self.replay_unchanged(request, reason='result_lost')
            changed = request[:10] + b'\x02' + request[11:]
            self.replay_unchanged(changed, reason='result_lost')
            require(set(self.state()['resources']) == {rid}, 'unretained request re-executed')

    def response_boundary(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid, reply_size=16)
            saved, rid = self.created_one(request, 16)
            self.record(request, request, saved)
            self.replay_unchanged(request, saved)
            require(set(self.state()['resources']) == {rid}, 'boundary response replay executed')

    def response_lost(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid, reply_size=17)
            _, rid = self.created_one(request, 17)
            self.record(request, request, None)
            self.replay_unchanged(request, reason='result_lost')
            changed = request[:10] + b'\x02' + request[11:]
            self.replay_unchanged(changed, reason='malformed')
            require(set(self.state()['resources']) == {rid}, 'unretained response re-executed')

    def eviction(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid); _, rid = self.created_one(request)
            self.c.corr += 1
            unseen_corr = self.c.corr
            requests, frames = [], []
            for _ in range(12):
                req = self.c.request(self.c.core['keepalive'], session=sid)
                frames.append(self.c.exchange(req)); requests.append(req)
            state = self.state()
            require(len(state['cache']) == 8 and struct.unpack_from('<H', request, 1)[0] not in dict(state['cache']),
                    'sample did not exercise bounded history eviction')
            for req, frame in zip(requests[-3:], frames[-3:]):
                self.record(req, req, frame); self.replay_unchanged(req, frame)
            self.replay_unchanged(request, reason='result_lost')
            unseen = self.create(sid, corr=unseen_corr)
            self.replay_unchanged(unseen, reason='result_lost')
            require(set(self.state()['resources']) == {rid}, 'evicted/unseen old request executed')
            _, fresh = self.created_one(self.create(sid))
            require(fresh == state['next_resource_id'] and len(self.state()['resources']) == 2,
                    'new corr did not execute exactly once after lost result')

    def rejected_lost(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid, kind=9, size=17)
            payload = self.c.rejected(request, 'malformed')
            saved = b'\x02' + request[1:3] + bytes((0, self.c.reasons['malformed'])) + payload
            self.record(request, None, saved)
            before = self.state()
            self.replay_unchanged(request, reason='result_lost')
            require(not before['resources'], 'rejected create returned live resource')
            _, rid = self.created_one(self.create(sid))
            require(rid == before['next_resource_id'], 'failed/replayed request consumed allocator ID')

    def end_lost(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid, size=17); self.created_one(request)
            tlvs(self.c.success(self.c.request(self.c.core['end'], session=sid)))
            self.c.session = None
            require(self.state()['holder'] is None and not self.state()['resources'], 'end retained state')
            self.replay_unchanged(request, reason='result_lost')

    def same_open(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid, reply_size=17); _, rid = self.created_one(request, 17)
            opened = self.c.success(self.c.request(self.c.core['open'], struct.pack('<IB', 60000, 0), session=sid))
            require(len(opened) >= 8 and struct.unpack_from('<II', opened) == (60000, self.c.boot), 'same-S open payload')
            tlvs(opened[8:]); self.record(request, request, None)
            self.replay_unchanged(request, reason='result_lost')
            require(set(self.state()['resources']) == {rid}, 'same-S open freed resources')

    def run(self):
        def identify():
            require(self.a.primary != self.a.peer and callable(getattr(self.a, 'retention_state', None)) and
                    callable(getattr(self.a, 'wait_ms', None)), 'explicit retention/clock instrumentation required')
            self.c.confirm(); self.c.identity(); self.c.declarations(); self.c.interfaces()
            require(all(x['status'] == 'passed' for x in self.c.results), 'declaration failed')
            self.peer.confirm(); self.peer.identity()
            require(self.peer.boot == self.c.boot, 'peer boot differs')
            geometry = self.a.retention_state()
            require(geometry == {'max_bytes': 16, 'capacity': 8}, 'explicit bounded sample geometry required')
            require(geometry['capacity'] >= max(self.c.observed['confirm']['max_inflight'],
                                               self.peer.observed['confirm']['max_inflight']), 'insufficient recent records')
            self.state()
            declared = {entry['fn']: ops(dict((row['tag'], bytes.fromhex(row['value_hex']))
                        for row in entry['describe'])[7]) for entry in self.c.observed['interfaces']}
            require(self.a.functions and type(self.a.functions[0]) is int and self.a.functions[0] in declared and
                    {16, 17, 18, 19} <= declared[self.a.functions[0]], 'sample resource operations missing')
        self.c.check('CORE-RETENTION-IDENTITY', 'core §5.2/7.1; explicit bounded sample', identify)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        for name, case in [('REQUEST-BOUNDARY', self.request_boundary), ('REQUEST-LOST', self.request_lost),
                           ('RESPONSE-BOUNDARY', self.response_boundary), ('RESPONSE-LOST', self.response_lost),
                           ('EVICTION', self.eviction), ('REJECTED', self.rejected_lost), ('END', self.end_lost),
                           ('SAME-OPEN', self.same_open)]:
            first = len(self.a.trace)
            self.c.check('CORE-RETENTION-' + name, 'core §5.2/6.1/9; bounded fixture', case)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        return {'status': 'passed' if all(x['status'] == 'passed' for x in self.c.results) else 'failed',
                'full_conformance': False, 'scope': 'explicit bounded request/result history',
                'levels': {'core': 'selected replay retention contracts', 'interface': 'sample declaration/resource observation',
                           'oep-interface': 'not executed'}, 'checks': self.c.results, 'observed': self.c.observed,
                'adapter_trace': self.a.trace, 'peer_exchanges': self.peer.trace,
                'unchecked': ['physical transport', 'concurrency', 'reboot', 'host result_lost recovery policy',
                              'retention loss at expiry/force in recorded run', 'other retention limits']}
