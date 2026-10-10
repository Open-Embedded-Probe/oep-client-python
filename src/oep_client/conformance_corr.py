"""Unsigned corr boundaries on explicitly instrumented retention sample equipment."""
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
        for target in (32768, 32769, 32770):
            with self.c.holding(60000) as sid:
                require(self.state()['high'] == 1, 'fresh session high')
                self.c.corr = target - 1
                request = self.create(sid)
                saved, _ = self.created_one(request)
                require(self.state()['high'] == target, 'half-range forward gap refused')
                self.replay_unchanged(request, saved)
                self.replay_unchanged(self.create(sid, corr=2), reason='result_lost')

    def large_gap(self):
        with self.c.holding(60000) as sid:
            self.c.corr = 65533
            request = self.create(sid)
            saved, rid = self.created_one(request)
            self.replay_unchanged(request, saved)
            for corr in (2, 32767, 32768, 65533):
                self.replay_unchanged(self.create(sid, corr=corr), reason='result_lost')
            require(set(self.state()['resources']) == {rid}, 'old unsigned corr executed')

    def zero_precedence(self):
        with self.c.holding(60000) as sid:
            self.c.corr = 65533
            self.created_one(self.create(sid))
            for fn, session in ((self.a.functions[0], sid), (65535, sid), (0, 0), (0, sid ^ 1 or 2)):
                request = self.c.request(self.c.core['clock'], session=session, fn=fn, corr=0)
                self.replay_unchanged(request, reason='malformed')

    def free_corr(self):
        with self.c.holding(60000) as sid:
            request = self.create(sid); saved, _ = self.created_one(request)
            # SID 0 has no replay history. Numeric overlap with S is sequential, never unresolved.
            before = self.state()
            for corr in (65535, 1, 32768, 65535):
                self.a.wait_ms(5)
                payload = self.c.success(self.c.request(self.c.core['clock'], corr=corr))
                require(len(payload) >= 12 and struct.unpack_from('<I', payload)[0] == self.c.boot,
                        'free-session clock payload')
                tlvs(payload[12:])
                require(self.state() == before, 'SID 0 corr changed session history/lease')
            self.replay_unchanged(request, saved)

    def reserved_end(self):
        with self.c.holding(60000) as sid:
            self.c.corr = 65533
            request = self.create(sid); saved, _ = self.created_one(request)
            end = self.c.request(self.c.core['end'], session=sid)
            require(struct.unpack_from('<H', end, 1)[0] == 65535, 'end did not use reserved last number')
            ended = self.c.exchange(end); self.result(ended, end); tlvs(ended[5:]); self.c.session = None
            require(self.state()['high'] == 65535 and self.state()['holder'] is None and
                    not self.state()['resources'], 'upper-bound end failed')
            self.replay_unchanged(request, saved)
            self.replay_unchanged(end, ended)
            self.replay_unchanged(self.create(sid, corr=1), reason='malformed')  # retained old open has different bytes
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
        for name, method in [('ASCENDING', self.ascending), ('HALF-GAPS', self.half_gaps), ('LARGE-GAP', self.large_gap),
                             ('ZERO', self.zero_precedence), ('FREE', self.free_corr), ('RESERVED-END', self.reserved_end)]:
            first = len(self.a.trace)
            self.c.check('CORE-CORR-' + name, 'core §4.1/5.2/6.1', method)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        report.update(status='passed' if all(x['status'] == 'passed' for x in self.c.results) else 'failed',
                      checks=self.c.results, adapter_trace=self.a.trace, peer_exchanges=self.peer.trace,
                      scope='bounded history and unsigned u16 corr boundaries')
        report['unchecked'] = ['physical transport', 'concurrent scheduling', 'production host rollover policy',
                               'host result_lost recovery policy', 'other retention limits']
        return report
