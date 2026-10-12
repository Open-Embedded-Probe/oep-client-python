"""Raw-message logical fixture for selected timing contracts, not a full Probe."""
import struct

import pytest

from oep_client.conformance_core_smoke import CoreSmokeChecks
from oep_client.conformance_session_smoke import SessionSmokeChecks
from test_conformance_core_smoke import SmallCore, REG, SPEC


class TimedCore(SmallCore):
    def __init__(self, capacity, fault=None):
        super().__init__(capacity)
        self.timing_fault = fault
        self.now = 0
        self.deadline = None
        self.resources = {}
        self.next_resource = 1

    def effects(self):
        return dict(super().effects(), deadline_ms=self.deadline, resources=dict(self.resources))

    def advance(self, ms):
        self.now += ms
        if self.holder is not None and self.now > self.deadline:
            self.holder = self.deadline = None
            if self.timing_fault != 'expiry_leak':
                self.resources.clear()

    def send(self, request):
        role, corr, fn, op, sid = struct.unpack_from('<BHHBI', request)
        assert role == 1 and len(request) <= 64
        body = request[10:]
        def success(data=b''):
            return struct.pack('<BHBB', 2, corr, 1, 0) + data
        def reject(reason):
            data = struct.pack('<I', self.deadline - self.now) if reason == 8 else b''
            return struct.pack('<BHBB', 2, corr, 0, reason) + data
        # Discovery requests are delegated to the core-only fixture. The named
        # test function is declared here; its sole operation creates a resource.
        if not sid and fn == 0 and op in (1, 2, 3, 4):
            if op == 2:
                name = b'io.example.resource'
                return success(struct.pack('<HBHHBB', 1, 1, 7, 0, 1, len(name)) + name)
            if op == 3 and body[:2] == b'\x07\0':
                return success(b'\0\x07\x02\0\x10\x01')
            return super().send(request)
        if corr == 0:
            return reject(3)
        if (fn == 0 and op not in (16, 17, 18, 19)) or (fn == 7 and op != 16):
            return reject(2)
        if fn not in (0, 7):
            return reject(1)
        if not sid and (fn or op != 19):
            return reject(9)
        if sid and sid == self.last:
            if corr in self.cache:
                previous, answer = self.cache[corr]
                if self.timing_fault == 'replay_renews' and self.holder == sid:
                    self.deadline = self.now + 1000
                    self.renewals += 1
                return answer if previous == request else reject(3)
            if not 0 < (corr - self.high) % 65536 < 32768:
                return reject(12)
        if sid and sid != self.holder:
            can_open = fn == 0 and op == 16 and sid != self.last and len(body) == 5 and (self.holder is None or body[4] == 1)
            if not can_open:
                if self.timing_fault == 'foreign_renews' and self.holder:
                    self.deadline = self.now + 1000
                return reject(8 if self.holder else 7)
        if not sid:
            if self.timing_fault == 'sid0_renews' and self.holder:
                self.deadline = self.now + 1000
            return success(struct.pack('<BI', int(self.holder is not None), self.deadline - self.now if self.holder else 0))
        malformed = False
        if fn == 7:
            assert body == b''
            number = self.next_resource; self.next_resource += 1
            self.resources[number] = sid
            answer = success(struct.pack('<H', number))
        elif op == 16:
            assert body == struct.pack('<IB', 1000, body[4]) and body[4] in (0, 1)
            if sid != self.last:
                if self.timing_fault != 'force_leak':
                    self.resources.clear()
                self.cache.clear()
                self.holder = self.last = sid
            answer = success(struct.pack('<II', 1000, 17))
        elif op == 17:
            self.holder = self.deadline = None
            self.resources.clear()
            answer = success()
        elif op == 18:
            malformed = bool(body)
            answer = reject(3) if malformed else success()
        else:
            answer = success(struct.pack('<BI', 1, self.deadline - self.now))
        self.high = corr
        self.accepted += 1
        if self.holder == sid and not (malformed and self.timing_fault == 'rejected_no_renewal'):
            self.deadline = self.now + 1000
            self.renewals += 1
        self.cache[corr] = (request, answer)
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return answer


def run(capacity=1, fault=None, resource=True, observe=True, registry=REG, spec=SPEC):
    probe = TimedCore(capacity, fault)
    core = CoreSmokeChecks(probe.send, registry, 'minimal', spec=spec,
                           observe_effects=probe.effects if observe else None)
    def prepare(call):
        payload = core.success(call(16, fn=7))
        assert len(payload) == 2 and int.from_bytes(payload, 'little') != 0
    report = SessionSmokeChecks(core, wait_ms=probe.advance,
        prepare_resource=prepare if resource else None,
        resources_released=(lambda: not probe.resources) if resource else None).run()
    return report


@pytest.mark.parametrize('capacity,resource', [(0, False), (1, False), (0, True), (1, True)])
def test_selected_raw_lifetimes(capacity, resource):
    report = run(capacity, resource=resource)
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 4 and not report['full_conformance']
    assert report['unfinished_session'] is None
    assert ('resource release: no resource adapter' in report['unchecked']) == (not resource)


@pytest.mark.parametrize('fault,case', [('replay_renews', 'EXPIRY'), ('sid0_renews', 'EXPIRY'),
    ('foreign_renews', 'EXPIRY'), ('expiry_leak', 'EXPIRY'), ('rejected_no_renewal', 'REJECTED-RENEWAL'),
    ('force_leak', 'FORCE')])
def test_representative_lifetime_faults(fault, case):
    report = run(fault=fault)
    assert report['status'] == 'failed'
    assert any(row['id'] == 'SESSION-SMOKE-' + case and row['status'] == 'failed' for row in report['checks'])


def test_no_observer_preserves_unchecked_scope():
    report = run(observe=False)
    assert report['status'] == 'passed'
    assert 'replay/old-owner side effects not independently observed' in report['unchecked']
