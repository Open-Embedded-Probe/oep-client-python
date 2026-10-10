"""Independent recovery scenarios using sample wire state and explicit reply loss."""
import struct
from .conformance import require
from .conformance_retention import RetentionChecks
from .conformance_recovery_sample import SampleRecoveryHost
from .conformance_route_sample import encoded


class RecoveryChecks(RetentionChecks):
    def __init__(self, checks, adapter):
        super().__init__(checks, adapter)
        self.host = SampleRecoveryHost(checks, adapter.functions)
        self.decisions = []

    def lose(self, request):
        require(self.a.discard_reply(request) is None, 'lost reply delivered to recovery host')
        # Sample ops have no operation wait; logical transfer time is zero.
        before = self.a.clock_ms()
        self.a.wait_ms(1050)
        require(self.a.clock_ms() - before >= 1050, 'reply-loss retry preceded host wait lower bound')

    def recover(self, sid, boot, before, status, *, action='create', slot=None, rid=None, single_writer=True):
        state = self.state()
        first = len(self.c.trace)
        decision = self.host.recover(sid, boot, before, action=action,
                                     slot=slot or (self.a.functions[0], 1), rid=rid, single_writer=single_writer)
        self.decisions.append(encoded(decision))
        require(decision['status'] == status and decision['original_outcome'] == 'unknown',
                'recovery inferred unsupported outcome or state')
        require(self.state()['resources'] == state['resources'] and
                self.state()['next_resource_id'] == state['next_resource_id'], 'recovery changed resources/allocator')
        for row in self.c.trace[first:]:
            data = bytes.fromhex(row['request_hex'])
            _, _, fn, op, session = struct.unpack_from('<BHHBI', data)
            require((fn == 0 and op in (1, 3, 4)) or
                    (fn in self.a.functions and op == 19 and session == 0),
                    'recovery resent mutation or used forbidden operation')
        if status in ('boot-changed', 'session-unavailable'):
            require(decision['inventory'] is None and 'candidate_resource_id' not in decision,
                    'stale boot/session returned resource bindings')
        else:
            require(decision['inventory'] == state['resources'], 'wire inventory differs from independent oracle')
        return decision

    def create_lost(self, mode):
        with self.c.holding(60000) as sid:
            before, boot = self.host.inventory(), self.c.boot
            request = self.create(sid, size=17 if mode == 'request' else 12,
                                  reply_size=17 if mode == 'response' else 7)
            self.lose(request)
            if mode == 'evicted':
                for _ in range(12): self.c.success(self.c.request(self.c.core['keepalive'], session=sid))
            self.replay_unchanged(request, reason='result_lost')
            decision = self.recover(sid, boot, before, 'observed-added')
            require(decision['candidate_resource_id'] in self.state()['resources'], 'missing observed resource')

    def rejected(self):
        with self.c.holding(60000) as sid:
            before, boot = self.host.inventory(), self.c.boot
            request = self.create(sid, kind=9, size=17)
            self.lose(request); self.replay_unchanged(request, reason='result_lost')
            self.recover(sid, boot, before, 'unchanged-state')

    def unseen(self):
        with self.c.holding(60000) as sid:
            before, boot = self.host.inventory(), self.c.boot
            request = self.create(sid)  # intentionally never submitted
            self.c.success(self.c.request(self.c.core['keepalive'], session=sid))
            self.replay_unchanged(request, reason='result_lost')
            self.recover(sid, boot, before, 'unchanged-state')

    def close_lost(self, rejected=False):
        with self.c.holding(60000) as sid:
            _, rid = self.created_one(self.create(sid))
            before, boot = self.host.inventory(), self.c.boot
            request = self.c.request(17, bytes((2 if rejected else 1,)) + struct.pack('<H', rid) +
                                     b'\x40\x01\0\0', session=sid, fn=self.a.functions[0])
            require(len(request) == 17, 'close request must exceed retention limit')
            self.lose(request); self.replay_unchanged(request, reason='result_lost')
            self.recover(sid, boot, before, 'still-present' if rejected else 'observed-absent', action='close', rid=rid)

    def ambiguous(self):
        with self.c.holding(60000) as sid:
            before, boot = self.host.inventory(), self.c.boot
            request = self.create(sid, size=17)
            self.lose(request)
            self.created_one(self.create(sid))  # another outstanding logical operation changes the same slot
            self.replay_unchanged(request, reason='result_lost')
            self.recover(sid, boot, before, 'ambiguous-state')

    def expiry(self):
        with self.c.holding(1000) as sid:
            before, boot = self.host.inventory(), self.c.boot
            request = self.create(sid, size=17)
            self.lose(request); self.service()
            self.c.session = None
            self.replay_unchanged(request, reason='result_lost')
            self.recover(sid, boot, before, 'session-unavailable')

    def reboot(self):
        with self.c.holding(60000) as sid:
            before, boot = self.host.inventory(), self.c.boot
            request = self.create(sid, size=17)
            self.lose(request)
            self.a.restart(boot ^ 1); self.c.session = None
            self.recover(sid, boot, before, 'boot-changed')

    def run(self):
        def controls():
            require(getattr(self.a, 'reset_scope', None) == 'logical-model' and all(callable(getattr(self.a, method, None))
                    for method in ('discard_reply', 'restart', 'clock_ms')), 'explicit logical reply-loss/reset controls required')
        self.c.check('CORE-RECOVERY-CONTROL', 'explicit sample host policy, logical loss only', controls)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        report = super().run()
        if report['status'] != 'passed': self.c.abort = True
        cases = [(mode.upper(), lambda mode=mode: self.create_lost(mode)) for mode in ('request', 'response', 'evicted')]
        cases += [('REJECTED', self.rejected), ('UNSEEN', self.unseen), ('CLOSE', self.close_lost),
                  ('CLOSE-REJECTED', lambda: self.close_lost(True)), ('AMBIGUOUS', self.ambiguous),
                  ('EXPIRY', self.expiry), ('BOOT', self.reboot)]
        for name, method in cases:
            first = len(self.a.trace)
            self.c.check('CORE-RECOVERY-' + name, 'core §5.2/6; sample state readback policy', method)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        report.update(status='passed' if all(x['status'] == 'passed' for x in self.c.results) else 'failed',
                      checks=self.c.results, adapter_trace=self.a.trace, peer_exchanges=self.peer.trace,
                      recovery_decisions=self.decisions, scope='sample host readback after result_lost')
        report['levels']['core'] = 'selected replay retention and host recovery contracts'
        report['unchecked'] = ['physical reply loss/timeout/reconnect', 'production Host recovery integration',
                               'concurrent writers and atomic snapshots', 'force during readback',
                               'arbitrary extension recovery semantics', 'other retention sizes']
        return report
