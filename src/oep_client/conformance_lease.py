"""Independent lease timing checks for an explicit logical writer fixture."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_replay_pressure import ReplayPressureChecks


class LeaseChecks(ReplayPressureChecks):
    def timing(self):
        value = self.a.timing_state()
        require(type(value.get('now_ms')) is int, 'model clock instrumentation required')
        return value

    def setup(self, sid):
        req = self.create(sid)
        frame = self.c.exchange(req)
        self.created([frame])
        tlvs(self.c.success(self.c.request(1, struct.pack('<HI', 0, 0), session=sid, fn=self.a.functions[0])))
        return req, frame

    def unhold(self):
        self.a.hold(self.a.primary, None); self.service()

    def sent_deadline(self, before, after, lease=1000):
        require(before['now_ms'] + lease <= after['deadline'] <= after['now_ms'] + lease,
                'lease did not start at whole-response completion')

    def whole(self):
        with self.c.holding(1000) as sid:
            self.setup(sid)
            old = self.state(); self.a.wait_ms(100)
            request = self.c.request(self.c.core['clock'], session=sid)
            try:
                self.a.hold(self.a.primary, 0); self.send_batch([request])
                self.a.wait_ms(150); self.service()
                require(self.state()['deadline'] == old['deadline'], 'processing or waiting renewed lease')
                self.a.hold(self.a.primary, 7); self.service()
                require(self.a.receive(self.a.primary) == [], 'partial clock response completed')
                require(self.state()['deadline'] == old['deadline'], 'partial response renewed lease')
                self.a.wait_ms(150)
                before = self.timing(); self.unhold(); after = self.timing()
                self.frames(self.a.primary, [request]); self.sent_deadline(before, after)
                require(after['holder'] == sid and after['resources'] and after['subscriptions'], 'live state lost')
            finally: self.unhold()

    def rejected_sent(self):
        with self.c.holding(1000) as sid:
            self.setup(sid)
            request = self.c.request(self.c.core['keepalive'], b'\x40', session=sid)
            old = self.state(); self.a.wait_ms(100)
            try:
                self.a.hold(self.a.primary, 0); self.send_batch([request]); self.a.wait_ms(150)
                require(self.state()['deadline'] == old['deadline'], 'queued rejection renewed lease')
                before = self.timing(); self.unhold(); after = self.timing()
                saved = self.frames(self.a.primary, [request], ['malformed'])[0]
                self.sent_deadline(before, after)
                self.a.wait_ms(100)
                self.a.hold(self.a.primary, 0); self.send_batch([request]); self.a.wait_ms(100)
                self.unhold()
                require(self.frames(self.a.primary, [request], ['malformed'])[0] == saved, 'rejected replay changed')
                require(self.state()['deadline'] == after['deadline'], 'rejected replay renewed lease')
            finally: self.unhold()

    def no_renew(self):
        with self.c.holding(1000) as sid:
            request, saved = self.setup(sid)
            old = self.state(); self.a.wait_ms(150)
            require(self.c.exchange(request) == saved, 'success replay changed')
            self.peer.clock()
            invalid = self.c.request(self.c.core['clock'], session=sid, fn=65535)
            self.c.rejected(invalid, 'unknown_function')
            other = secrets.randbelow(0xffffffff) + 1
            while other == sid: other = secrets.randbelow(0xffffffff) + 1
            self.peer.rejected(self.peer.request(self.peer.core['clock'], session=other), 'locked')
            require(self.state()['deadline'] == old['deadline'], 'replay, free query or early refusal renewed lease')

    def expiry(self):
        with self.c.holding(1000) as sid:
            self.setup(sid)
            request = self.c.request(self.c.core['clock'], session=sid)
            try:
                self.a.hold(self.a.primary, 0); self.send_batch([request])
                self.a.wait_ms(1050); self.service()
                self.c.session = None
                expired = self.state()
                require(expired['holder'] is None and not expired['resources'] and not expired['subscriptions'],
                        'queued result prevented expiry or retained resources/subscriptions')
                lock = self.peer.success(self.peer.request(self.peer.core['lock_state']))
                require(lock[:5] == bytes(5), 'expired lock_state'); tlvs(lock[5:])
                self.unhold(); saved = self.frames(self.a.primary, [request])[0]
                require(self.state()['holder'] is None and self.state()['deadline'] == expired['deadline'],
                        'late response resurrected expired session')
                require(self.c.exchange(request) == saved, 'expiry lost recorded success')
                self.c.rejected(self.c.request(self.c.core['keepalive'], session=sid), 'no_session')
                require(self.state()['holder'] is None, 'post-expiry request reopened session')
            finally: self.unhold()

    def old_after_force(self):
        with self.c.holding(1000) as sid:
            self.setup(sid)
            request = self.c.request(self.c.core['keepalive'], session=sid)
            replacement = secrets.randbelow(0xffffffff) + 1
            while replacement == sid: replacement = secrets.randbelow(0xffffffff) + 1
            try:
                self.a.hold(self.a.primary, 0); self.send_batch([request])
                self.peer.confirm()
                require(self.peer.boot == self.c.boot and self.state()['holder'] == sid, 'unsafe takeover')
                self.peer.corr = 0
                opened = self.peer.success(self.peer.request(self.peer.core['open'], struct.pack('<IB', 1000, 1), session=replacement))
                self.peer.session = replacement; self.c.session = None
                require(len(opened) >= 8 and struct.unpack_from('<II', opened) == (1000, self.c.boot), 'force payload')
                tlvs(opened[8:]); old = self.state()
                self.a.wait_ms(150); self.unhold(); self.frames(self.a.primary, [request])
                current = self.state()
                require(current['holder'] == replacement and current['deadline'] == old['deadline'],
                        'old S completion renewed T or resurrected S')
                require(not current['resources'] and not current['subscriptions'], 'force retained S state')
            finally:
                self.unhold()
                if self.peer.session is not None:
                    tlvs(self.peer.success(self.peer.request(self.peer.core['end'], session=replacement)))
                    self.peer.session = None

    def ended(self):
        with self.c.holding(1000) as sid:
            self.setup(sid)
            first = self.c.request(self.c.core['clock'], session=sid)
            end = self.c.request(self.c.core['end'], session=sid)
            try:
                self.a.hold(self.a.primary, 0); self.send_batch([first, end])
                self.c.session = None
                before = self.state()
                require(before['holder'] is None and not before['resources'] and not before['subscriptions'],
                        'end waits for transport before releasing state')
                self.a.wait_ms(150); self.unhold(); frames = self.frames(self.a.primary, [first, end])
                require(self.state()['holder'] is None and self.state()['deadline'] == before['deadline'],
                        'old success/end completion revived session')
                require(self.c.exchange(end) == frames[1], 'end replay changed')
            finally: self.unhold()

    def run(self):
        report = super().run()
        if report['status'] != 'passed': self.c.abort = True
        def timing():
            require(callable(getattr(self.a, 'wait_ms', None)) and callable(getattr(self.a, 'clock_ms', None)) and
                    callable(getattr(self.a, 'timing_state', None)),
                    'explicit wait/clock capability required')
            self.timing()
        self.c.check('CORE-LEASE-INSTRUMENTATION', 'explicit logical clock/writer fixture', timing)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        for name, case in [('WHOLE', self.whole), ('REJECTED', self.rejected_sent), ('NO-RENEW', self.no_renew),
                           ('EXPIRE', self.expiry), ('FORCE', self.old_after_force), ('END', self.ended)]:
            first = len(self.a.trace)
            self.c.check('CORE-LEASE-' + name, 'core §5.2/6.1/6.4/9; logical writer', case)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        report.update(status='passed' if all(x['status'] == 'passed' for x in self.c.results) else 'failed',
                      checks=self.c.results, adapter_trace=self.a.trace, peer_exchanges=self.peer.trace,
                      scope='explicit logical writer lease timing')
        report['unchecked'] = ['physical transport completion', 'concurrent execution', 'reboot',
                               'host scheduler', 'silent loss recovery', 'long op in recorded real-clock run',
                               'large request/result retention bounds',
                               'route close and same-session open in recorded real-clock run']
        return report
