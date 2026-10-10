"""Bounded replay history across expiry, replacement and logical reboot."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_retention import RetentionChecks


class RetentionLifecycleChecks(RetentionChecks):
    def prepared(self, sid, mode):
        request = self.create(sid, size=17 if mode == 'request' else 12,
                              reply_size=17 if mode == 'response' else 7)
        _, rid = self.created_one(request, 17 if mode == 'response' else 7)
        tlvs(self.c.success(self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=self.a.functions[0])))
        if mode == 'evicted':
            for _ in range(12):
                tlvs(self.c.success(self.c.request(self.c.core['keepalive'], session=sid)))
            require(struct.unpack_from('<H', request, 1)[0] not in dict(self.state()['cache']), 'eviction not exercised')
        return request, rid

    def fresh_sid(self, old):
        sid = secrets.randbelow(0xffffffff) + 1
        while sid == old: sid = secrets.randbelow(0xffffffff) + 1
        return sid

    def opened(self, checks, sid, force=0):
        checks.corr = 0
        request = checks.request(checks.core['open'], struct.pack('<IB', 60000, force), session=sid)
        value = checks.success(request)
        checks.session = sid
        require(len(value) >= 8 and struct.unpack_from('<II', value) == (60000, self.c.boot), 'replacement open payload')
        tlvs(value[8:])
        frame = b'\x02' + request[1:3] + b'\x01\0' + value
        require(self.state()['cache'] == [(1, (request, frame))] and self.state()['high'] == 1,
                'new session did not replace old markers/high-water history')

    def expiry(self, mode):
        with self.c.holding(1000) as sid:
            request, rid = self.prepared(sid, mode)
            before = self.state()
            self.a.wait_ms(1050); self.service()
            self.c.session = None
            after = self.state()
            require(after['holder'] is None and not after['resources'] and not after['subscriptions'], 'expiry retained runtime state')
            require(all(after[key] == before[key] for key in ('cache', 'high', 'last', 'next_resource_id', 'boot_id')),
                    'expiry discarded history or reset boot allocator')
            self.replay_unchanged(request, reason='result_lost')
            if mode == 'response':
                self.replay_unchanged(request[:10] + b'\x02' + request[11:], reason='malformed')
            self.c.rejected(self.c.request(self.c.core['keepalive'], session=sid), 'no_session')
            replacement = self.fresh_sid(sid)
            self.opened(self.c, replacement)
            _, new_rid = self.created_one(self.create(replacement))
            require(new_rid == before['next_resource_id'] and new_rid != rid, 'same-boot allocator reused old resource')
            # holding() ends the actual current T on its original primary.

    def force(self, mode):
        with self.c.holding(60000) as sid:
            request, rid = self.prepared(sid, mode)
            before = self.state()
            self.peer.confirm(); self.peer.identity()
            require(self.peer.boot == self.c.boot and self.state()['holder'] == sid, 'boot/holder changed before force')
            replacement = self.fresh_sid(sid)
            try:
                self.opened(self.peer, replacement, 1)
                self.c.session = None
                after = self.state()
                require(after['last'] == replacement and after['holder'] == replacement and
                        not after['resources'] and not after['subscriptions'], 'force retained old runtime state')
                require(after['next_resource_id'] == before['next_resource_id'] and after['boot_id'] == before['boot_id'],
                        'force reset boot identity or allocator')
                self.replay_unchanged(request, reason='locked')  # S only on original primary
                req = self.peer.request(16, b'\x01\x07', session=replacement, fn=self.a.functions[0])
                value = self.peer.success(req)
                require(len(value) == 2 and struct.unpack('<H', value)[0] == before['next_resource_id'] and
                        struct.unpack('<H', value)[0] != rid, 'force reused old resource ID')
            finally:
                if self.peer.session is not None:
                    tlvs(self.peer.success(self.peer.request(self.peer.core['end'], session=replacement)))
                    self.peer.session = None

    def reboot(self):
        with self.c.holding(60000) as sid:
            missing, _ = self.prepared(sid, 'request')
            response = self.create(sid, reply_size=17); self.created_one(response, 17)
            retained = self.create(sid); self.created_one(retained)
            paused = self.c.request(self.c.core['clock'], session=sid)
            self.a.hold(self.a.primary, 0); self.send_batch([paused])
            queued = self.create(sid)
            self.a.submit([(self.a.primary, queued)])  # accepted but not executed
            old_boot = self.c.boot
            new_boot = old_boot ^ 1  # explicit logical fixture reset, not firmware control
            self.a.restart(new_boot)
            self.c.session = None
            after = self.state()
            require(after['boot_id'] == new_boot and after['holder'] is None and after['last'] is None and
                    after['high'] == 0 and not after['cache'] and not after['resources'] and
                    not after['subscriptions'] and after['next_resource_id'] == 1, 'reboot retained old boot state')
            require(self.a.pending(self.a.primary) == [] and self.a.pending(self.a.peer) == [], 'reboot retained pending request credit')
            require(self.a.receive(self.a.primary) == [] and self.a.receive(self.a.peer) == [], 'reboot leaked old buffered output')
            self.c.confirm(); self.c.identity(); self.c.declarations(); self.c.clock()
            self.peer.confirm(); self.peer.identity()
            require(self.c.boot == new_boot == self.peer.boot and self.c.boot != old_boot, 'boot change not confirmed')
            for request in (missing, response, retained, paused, queued):
                self.c.rejected(request, 'no_session')  # never resend old open
            require(not self.state()['cache'] and self.state()['holder'] is None, 'stale request recorded or executed after reboot')
            replacement = self.fresh_sid(sid)
            self.opened(self.c, replacement)
            _, rid = self.created_one(self.create(replacement))
            require(rid == 1, 'new boot did not restart resource numbering')

    def run(self):
        def controls():
            require(getattr(self.a, 'reset_scope', None) == 'logical-model' and all(callable(getattr(self.a, name, None)) for name in ('restart', 'pending', 'receive')),
                    'explicit logical-model restart capability required')
            state = self.state()
            require(type(state.get('boot_id')) is int and 0 <= state['boot_id'] <= 0xffffffff, 'boot instrumentation required')
        self.c.check('CORE-LIFECYCLE-CONTROL', 'explicit logical reset fixture only', controls)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        report = super().run()
        if report['status'] != 'passed': self.c.abort = True
        for group, method in [('EXPIRY', self.expiry), ('FORCE', self.force)]:
            for mode in ('request', 'response', 'evicted'):
                first = len(self.a.trace)
                self.c.check('CORE-LIFECYCLE-' + group + '-' + mode.upper(), 'core §5.2/6/9', lambda mode=mode, method=method: method(mode))
                self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        first = len(self.a.trace)
        self.c.check('CORE-LIFECYCLE-REBOOT', 'core §5.2/6.5/9; logical model reset', self.reboot)
        self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        report.update(status='passed' if all(x['status'] == 'passed' for x in self.c.results) else 'failed',
                      checks=self.c.results, adapter_trace=self.a.trace, peer_exchanges=self.peer.trace,
                      scope='bounded history across expiry, replacement and logical reboot')
        report['unchecked'] = ['physical restart/transport reconnect', 'concurrent scheduling',
                               'host result_lost recovery policy', 'other retention sizes', 'all boot-id entropy sources']
        return report
