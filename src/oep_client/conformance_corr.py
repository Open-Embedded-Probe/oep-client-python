"""Cyclic corr16 boundaries (core §4.1, §5.1) on explicitly instrumented retention sample equipment."""
import struct
from .conformance import require, tlvs
from .conformance_retention import RetentionChecks


class CorrChecks(RetentionChecks):
    def ascending(self):
        with self.c.holding(60000) as sid:
            ids = []
            for corr in (32767, 32768, 32769, 65534):
                self.c.corr = corr - 1
                request = self.create(sid)
                saved, rid = self.created_one(request)
                ids.append(rid)
                require(self.state()['high'] == corr, 'ordinary unsigned ascending corr refused')
                self.replay_unchanged(request, saved)
            require(len(set(ids)) == 4, 'new corr failed to execute exactly once')

    def half_gaps(self):
        """A corr 1 to 32767 ahead of the last is new; 32768 and more is result_lost, never executed (core §5.1)."""
        for target, new in ((32768, True), (32769, False), (32770, False)):
            with self.c.holding(60000) as sid:
                require(self.state()['high'] == 1, 'fresh session high')
                if new:
                    self.c.corr = target - 1
                request = self.create(sid, corr=None if new else target)
                if new:
                    saved, _ = self.created_one(request)
                    require(self.state()['high'] == target, 'forward gap below half the space refused')
                    self.replay_unchanged(request, saved)
                else:
                    self.replay_unchanged(request, reason='result_lost')
                    require(self.state()['high'] == 1 and not self.state()['resources'],
                            'a corr half the space or more ahead was executed')

    def wrap(self):
        """65535 is followed by 1 (0 is never used) and the session keeps its resources; a corr half the space or
        more behind the last is result_lost (core §4.1, §5.1)."""
        with self.c.holding(60000) as sid:
            self.walk(sid, 65534)
            request = self.create(sid)                                # 65535
            saved, rid = self.created_one(request)
            wrapped = self.create(sid)                                # wraps to 1
            require(struct.unpack_from('<H', wrapped, 1)[0] == 1, 'the corr after 65535 is not 1')
            _, rid2 = self.created_one(wrapped)
            require(self.state()['high'] == 1 and set(self.state()['resources']) == {rid, rid2},
                    'wrap lost resources or history')
            self.replay_unchanged(request, saved)
            for corr in (32769, 40000):                               # half the space or more behind 1
                self.replay_unchanged(self.create(sid, corr=corr), reason='result_lost')
            require(set(self.state()['resources']) == {rid, rid2}, 'old corr executed')

    def zero_precedence(self):
        with self.c.holding(60000) as sid:
            self.walk(sid, 65533)
            self.created_one(self.create(sid))
            for fn, session in ((self.a.functions[0], sid), (65535, sid), (0, 0), (0, sid ^ 1 or 2)):
                request = self.c.request(self.c.core['clock'], session=session, fn=fn, corr=0)
                self.replay_unchanged(request, reason='malformed')

    def free_corr(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid); saved, _ = self.created_one(request)
            # SID 0 has no replay history: its numbers, even half the space away, leave S's alone.
            before = self.state()
            for corr in (65535, 1, 32768, 65535):
                self.a.wait_ms(5)
                payload = self.c.success(self.c.request(self.c.core['clock'], corr=corr))
                require(len(payload) >= 12 and struct.unpack_from('<I', payload)[0] == self.c.boot,
                        'free-session clock payload')
                tlvs(payload[12:])
                require(self.state() == before, 'SID 0 corr changed session history/lease')
            self.replay_unchanged(request, saved)

    def wrap_end(self):
        """An end after the wrap releases everything and is replayed from the record; the ended id is no_session; only
        a new id's open starts a new replay state (core §5.1, §6.2)."""
        with self.c.holding(60000) as sid:
            self.walk(sid, 65535)
            request = self.create(sid); saved, _ = self.created_one(request)     # 1, after the wrap
            end = self.c.request(self.c.core['end'], session=sid)
            ended = self.c.exchange(end); self.result(ended, end); tlvs(ended[5:]); self.c.session = None
            require(self.state()['high'] == 2 and self.state()['holder'] is None and
                    not self.state()['resources'], 'end after the wrap failed')
            self.replay_unchanged(request, saved)
            self.replay_unchanged(end, ended)
            replacement = sid ^ 0xffffffff or 1
            self.c.corr = 0
            opened = self.c.success(self.c.request(self.c.core['open'], struct.pack('<IB', 60000, 0), session=replacement))
            self.c.session = replacement
            require(struct.unpack_from('<II', opened) == (60000, self.c.boot), 'new session open payload')
            tlvs(opened[8:])
            require(self.state()['last'] == replacement and self.state()['high'] == 1 and
                    set(dict(self.state()['cache'])) == {1}, 'different session did not reset history')
            self.created_one(self.create(replacement))

    def run(self):
        report = super().run()
        if report['status'] != 'passed': self.c.abort = True
        for name, method in [('ASCENDING', self.ascending), ('HALF-GAPS', self.half_gaps), ('WRAP', self.wrap),
                             ('ZERO', self.zero_precedence), ('FREE', self.free_corr), ('WRAP-END', self.wrap_end)]:
            first = len(self.a.trace)
            self.c.check('CORE-CORR-' + name, 'core §4.1/5.2/6.1', method)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        report.update(status='passed' if all(x['status'] == 'passed' for x in self.c.results) else 'failed',
                      checks=self.c.results, adapter_trace=self.a.trace, peer_exchanges=self.peer.trace,
                      scope='bounded history and cyclic u16 corr boundaries')
        report['unchecked'] = ['physical transport', 'concurrent scheduling', 
                               'host result_lost recovery policy', 'other retention limits']
        return report
