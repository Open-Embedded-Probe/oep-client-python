"""Independent bounded replay history checks on explicit sample equipment."""
import struct
from .conformance import require, tlvs, ops
from .conformance_replay_pressure import ReplayPressureChecks
from .conformance_routes import RouteChecks


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
        observations = []
        for lease in (1000, 60000):
            for force in (0, 1):
                with self.c.holding(3000) as sid:
                    fn = self.a.functions[0]
                    request = self.create(sid, reply_size=17)
                    _, rid = self.created_one(request, 17)
                    retained = self.create(sid)
                    saved, second = self.created_one(retained)
                    tlvs(self.c.success(self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=fn)))
                    self.a.stimulate(fn, 1); self.service()
                    frames = self.a.receive(self.a.primary)
                    require(len(frames) == 1, 'same-S initial notification missing')
                    RouteChecks.event(self, frames[0], fn, 0, 1)
                    require(self.a.receive(self.a.peer) == [], 'initial notification reached peer')
                    before = self.state()
                    self.a.wait_ms(7)
                    start = self.a.timing_state()['now_ms']
                    reopen = self.c.request(self.c.core['open'], struct.pack('<IB', lease, force), session=sid)
                    # A fresh open may arrive on another route, but does not subscribe there.
                    opened = self.peer.success(reopen)
                    finish = self.a.timing_state()['now_ms']
                    require(len(opened) >= 8 and struct.unpack_from('<II', opened) == (lease, self.c.boot),
                            'same-S open lease/boot payload')
                    tlvs(opened[8:])
                    after = self.state()
                    for field in ('holder', 'last', 'resources', 'routes', 'subscriptions', 'next_resource_id', 'boot_id'):
                        require(after[field] == before[field], 'same-S open changed ' + field)
                    require(set(after['resources']) == {rid, second}, 'same-S open freed resources')
                    history = dict(after['cache'])
                    require(all(history.get(corr) == value for corr, value in before['cache']),
                            'same-S open changed previous replay history')
                    corr = struct.unpack_from('<H', reopen, 1)[0]
                    require(after['high'] == corr and history.get(corr) ==
                            (reopen, b'\x02' + reopen[1:3] + b'\x01\0' + opened),
                            'same-S open was not recorded')
                    require(start + lease <= after['deadline'] <= finish + lease,
                            'same-S open did not update lease from response completion')
                    self.record(request, request, None)
                    self.replay_unchanged(request, reason='result_lost')
                    self.replay_unchanged(retained, saved)
                    self.replay_unchanged(reopen, b'\x02' + reopen[1:3] + b'\x01\0' + opened)
                    self.a.stimulate(fn, 2); self.service()
                    frames = self.a.receive(self.a.primary)
                    require(len(frames) == 1, 'same-S notification route changed or subscription lost')
                    RouteChecks.event(self, frames[0], fn, 1, 2)
                    require(self.a.receive(self.a.peer) == [], 'same-S open redirected notifications to peer')
                    observations.append({'lease_ms': lease, 'force': force, 'open_route': self.a.peer,
                                         'request_hex': reopen.hex(), 'reply_payload_hex': opened.hex(),
                                         'deadline': after['deadline'], 'start_ms': start, 'finish_ms': finish,
                                         'resource_ids': [rid, second], 'notification_sequence': [0, 1],
                                         'old_history_unchanged': True})
        self.c.observed['retention_same_open'] = observations

    def session_zero(self):
        with self.c.holding(60000) as sid:
            owned=self.create(sid);saved,rid=self.created_one(owned)
            before=self.state()
            request=self.peer.request(self.peer.core['clock'],corr=65535)
            first=self.peer.success(request)
            require(len(first)>=12 and struct.unpack_from('<I',first)[0]==self.c.boot,'session0 clock shape/boot')
            self.a.wait_ms(5)
            second=self.peer.success(request)
            require(len(second)>=12 and struct.unpack_from('<I',second)[0]==self.c.boot,'repeated session0 clock shape/boot')
            tlvs(first[12:]);tlvs(second[12:])
            require(struct.unpack_from('<Q',second,4)[0]>=struct.unpack_from('<Q',first,4)[0]+5000000,
                    'session0 duplicate was deduplicated instead of executed')
            require(self.state()==before and set(self.state()['resources'])=={rid},'session0 repeat changed owned lease/history/resources')
            self.replay_unchanged(owned,saved)
            self.c.observed['retention_session_zero']={'corr':65535,'first_clock_hex':first.hex(),'second_clock_hex':second.hex(),
                                                     'same_request_hex':request.hex(),'owned_replay_unchanged':True}

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
                           ('SAME-OPEN', self.same_open), ('SESSION-ZERO', self.session_zero)]:
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
