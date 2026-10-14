"""Common notification wrap, atomic resubscription and cached-route invariants."""
import struct
from .conformance import require, tlvs
from .conformance_routes import RouteChecks


class NotificationSequenceChecks(RouteChecks):
    def fresh(self):
        reset = self.a.restart_fixture()
        require(reset['scope'] == 'logical-model', 'logical reset required')
        for c in (self.c, self.peer):
            c.confirm(); c.identity(); c.declarations()
            require(c.boot == reset['boot_id'], 'reset boot differs')

    def on(self, route, request, reason=None):
        # The probe contract is checked with ordered raw requests. Cross-route
        # session stimuli are deliberately not a conforming Host usage example.
        sender = self.c.send
        self.c.send = lambda req: self.a.exchange(route, req)
        try:
            if reason is None:
                frame = self.c.exchange(request)
                require(frame[3:5] == b'\x01\0', 'subscription completed success')
                tlvs(frame[5:]); return frame
            frame = self.c.exchange(request)
            require(frame[3:5] == bytes((0, self.c.reasons[reason])), 'subscription refusal differs')
            return frame
        finally:
            self.c.send = sender

    def event_on(self, route, fn, seq, marker):
        self.a.stimulate(fn, marker); self.service()
        frames = self.a.receive(route)
        require(len(frames) == 1, 'one event on selected route required')
        self.event(frames[0], fn, seq, marker)
        other = self.a.peer if route == self.a.primary else self.a.primary
        require(self.a.receive(other) == [], 'notification sent on stale/both routes')

    def wrap(self):
        self.fresh()
        with self.c.holding(60000) as sid:
            fn, second = self.a.functions
            self.subscribe(sid, fn); self.subscribe(sid, second)
            self.a.seed_sequence(fn, 65534)
            for seq, kind in ((65534, 'data'), (65535, 'event'), (0, 'data'), (1, 'event')):
                if kind == 'event': self.event_on(self.a.primary, fn, seq, seq + 100)
                else:
                    data = bytes((seq & 255,)); position = self.a.feed(fn, data); self.service()
                    frames = self.a.receive(self.a.primary)
                    require(len(frames) == 1, 'one wrapped data frame required')
                    self.data(frames[0], fn, seq, position, data)
                    require(self.a.receive(self.a.peer) == [], 'wrapped data leaked')
            self.event_on(self.a.primary, second, 0, 200)

    def dropped(self):
        self.fresh()
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]; self.subscribe(sid, fn); self.a.seed_sequence(fn, 65534)
            expected = {}
            try:
                self.a.hold(self.a.primary, 0)
                for index in range(32):
                    seq = (65534 + index) & 65535
                    if index % 2:
                        self.a.stimulate(fn, index); expected[seq] = ('event', index)
                    else:
                        data = bytes((index,)) * (self.c.max_frame - 15)
                        position = self.a.feed(fn, data); expected[seq] = ('data', position, data)
                    self.bounded(self.a.primary)
                self.service(); require(self.a.receive(self.a.primary) == [], 'blocked writer emitted')
                frames = self.drain(); require(0 < len(frames) < 32, 'explicit pressure did not drop frames')
                previous = -1
                for frame in frames:
                    seq = int.from_bytes(frame[3:5], 'little'); index = (seq - 65534) & 65535
                    require(previous < index < 32, 'wrapped queue seq/order')
                    previous = index; value = expected[seq]
                    if value[0] == 'event': self.event(frame, fn, seq, value[1])
                    else: self.data(frame, fn, seq, value[1], value[2])
                self.event_on(self.a.primary, fn, 30, 201)
            finally: self.drain()

    def replace(self):
        self.fresh()
        with self.c.holding(60000) as sid:
            fn, other = self.a.functions
            self.subscribe(sid, fn); self.subscribe(sid, other)
            self.event_on(self.a.primary, fn, 0, 202); self.event_on(self.a.primary, other, 0, 203)
            request = self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=fn)
            self.on(self.a.peer, request)
            self.event_on(self.a.peer, fn, 0, 204)
            self.event_on(self.a.primary, other, 1, 205)

    def discard(self):
        # Replacing or ending a subscription drops its unsent notifications; a frame already
        # started on the route completes before the response on that route (core §10.3/10.4).
        self.fresh()
        with self.c.holding(60000) as sid:
            fn, other = self.a.functions
            self.subscribe(sid, fn); self.subscribe(sid, other)
            observations = []
            try:
                for step, (route, op) in enumerate(((self.a.primary, 1), (self.a.peer, 1), (self.a.primary, 2))):
                    base = 400 + 10 * step
                    self.a.hold(self.a.primary, 3)
                    for target, marker in ((fn, base), (fn, base + 1), (other, base + 2)): self.a.stimulate(target, marker)
                    self.service()
                    require(self.a.receive(self.a.primary) == [], 'held writer completed a frame')
                    queued = self.bounded(self.a.primary)
                    require(len(queued) == 3, 'started frame and two unsent frames required')
                    request = self.c.request(op, struct.pack('<HI', 0, 0) if op == 1 else b'', session=sid, fn=fn)
                    self.a.hold(self.a.primary, None); self.a.submit([(route, request)]); self.service()
                    frames = self.a.receive(self.a.primary)
                    observations.append({'route': route, 'op': op, 'frames_hex': [f.hex() for f in frames]})
                    require(len(frames) == (3 if route == self.a.primary else 2), 'old unsent notification survived or started frame lost')
                    self.event(frames[0], fn, (0, 1, 0)[step], base)
                    if route == self.a.primary:
                        require(frames[1][:5] == struct.pack('<BHBB', 2, struct.unpack_from('<H', request, 1)[0], 1, 0),
                                'response did not follow the started frame'); tlvs(frames[1][5:])
                    else:
                        reply = self.a.receive(self.a.peer)
                        require(len(reply) == 1 and reply[0][:5] == struct.pack('<BHBB', 2, struct.unpack_from('<H', request, 1)[0], 1, 0),
                                'other-route response missing'); tlvs(reply[0][5:])
                    self.event(frames[-1], other, step, base + 2)
                    if op == 1:
                        self.event_on(route, fn, 0, base + 3)
                        if route != self.a.primary:  # move the subscription back for the next step
                            self.on(self.a.primary, self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=fn))
                    else:
                        self.a.stimulate(fn, base + 3); self.service()
                        require(self.a.receive(self.a.primary) == [] and self.a.receive(self.a.peer) == [],
                                'notification after unsubscribe')
            finally: self.drain()
            self.c.observed['discard'] = observations

    def unsubscribe(self):
        self.fresh()
        with self.c.holding(60000) as sid:
            fn, other = self.a.functions
            owned = self.c.success(self.c.request(16, b'\x01', session=sid, fn=fn))
            require(len(owned) == 2 and int.from_bytes(owned, 'little') != 0, 'unsubscribe sample resource')
            self.on(self.a.peer, self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=other))
            self.event_on(self.a.peer, other, 0, 301)
            observations = []
            for phase in ('absent', 'active', 'again'):
                if phase == 'active':
                    self.subscribe(sid, fn); self.event_on(self.a.primary, fn, 0, 302)
                before = self.a.sequence_state()
                request = self.c.request(2, session=sid, fn=fn)
                self.on(self.a.peer, request)
                after = self.a.sequence_state()
                for field in ('subscriptions', 'routes', 'conditions'):
                    require(fn not in after[field], 'unsubscribe did not remove selected ' + field)
                    require({k:v for k,v in after[field].items() if k != fn} ==
                            {k:v for k,v in before[field].items() if k != fn},
                            'unsubscribe changed other ' + field)
                require(after['resources'] == before['resources'], 'unsubscribe changed owned resources')
                self.a.stimulate(fn, 303); self.service()
                require(self.a.receive(self.a.primary) == [] and self.a.receive(self.a.peer) == [],
                        'new notification emitted without subscription')
                observations.append({'phase': phase, 'request_hex': request.hex(), 'other_sequence': after['subscriptions'][other],
                                     'other_route': after['routes'][other], 'owned_resource_hex': owned.hex()})
            self.event_on(self.a.peer, other, 1, 304)
            self.subscribe(sid, fn); self.event_on(self.a.primary, fn, 0, 305)
            self.c.observed['unsubscribe_isolation'] = observations

    def replay(self, refused=False):
        self.fresh()
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]
            first = self.c.request(1, b'\0' if refused else struct.pack('<HI', 0, 0), session=sid, fn=fn)
            original = self.on(self.a.primary, first, 'malformed' if refused else None)
            replacement = self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=fn)
            self.on(self.a.peer, replacement); self.event_on(self.a.peer, fn, 0, 206)
            before = self.a.sequence_state()
            require(self.on(self.a.primary, first, 'malformed' if refused else None) == original,
                    'cached subscription response bytes changed')
            require(self.a.sequence_state() == before, 'cached subscription changed route/seq/conditions/history')
            self.event_on(self.a.peer, fn, 1, 207)

    def rejected(self):
        self.fresh()
        with self.c.holding(60000) as sid:
            fn = self.a.functions[0]; self.subscribe(sid, fn); self.a.seed_sequence(fn, 7)
            for payload, reason in ((b'\0', 'malformed'), (struct.pack('<HI', 6, 400)+b'\xfe\x01\0x', 'unsupported')):
                before = self.a.sequence_state()
                self.on(self.a.peer, self.c.request(1, payload, session=sid, fn=fn), reason)
                after = self.a.sequence_state()
                require(all(after[k] == before[k] for k in ('subscriptions', 'routes', 'conditions', 'resources')),
                        'rejected resubscription changed selected state')
            self.event_on(self.a.primary, fn, 7, 208)

    def run(self):
        start = len(self.c.results)
        def context():
            require(self.a.reset_scope == 'logical-model' and self.a.reference, 'explicit logical sequence fixture required')
            require(tuple(self.a.functions) == (1, 2) and self.a.primary != self.a.peer, 'explicit two-fn/two-route fixture required')
            require(all(callable(getattr(self.a, k, None)) for k in ('restart_fixture', 'seed_sequence', 'sequence_state')),
                    'explicit sequence stimulus/observations required')
            self.fresh()
            require(self.c.max_frame == self.peer.max_frame == 64, 'selected pressure fixture geometry differs')
            self.c.interfaces()
        self.c.check('IF-NOTIFY-SEQ-CONTEXT', 'core §10.2/10.3; explicit fixture', context)
        if any(x['status'] != 'passed' for x in self.c.results[start:]): self.c.abort = True
        for name, method in [('WRAP', self.wrap), ('DROP-WRAP', self.dropped), ('REPLACE', self.replace), ('DISCARD', self.discard), ('UNSUBSCRIBE', self.unsubscribe),
                             ('REPLAY', self.replay), ('REPLAY-REFUSED', lambda: self.replay(True)), ('REJECTED', self.rejected)]:
            first = len(self.a.trace)
            self.c.check('IF-NOTIFY-SEQ-'+name, 'core §5.2/10.2/10.3/10.4; transports §3', method)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        rows = self.c.results[start:]
        return dict(status='passed' if all(x['status'] == 'passed' for x in rows) else 'failed', full_conformance=False,
                    checks=rows, adapter_trace=self.a.trace, observed=self.c.observed,
                    scope='logical probe notification wrap, pressure and ordered raw resubscription/replay',
                    unchecked=['physical framing/OS buffers and concurrent producers', 'Host single-route usage validation',
                               'all OEP-specific notification payloads', 'complete core/common normative audit'])
