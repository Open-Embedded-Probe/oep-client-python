"""Independent small core fixture; no production virtual endpoint or hardware."""
from collections import OrderedDict
import copy
import struct

import pytest

from oep_client.conformance_core_smoke import CoreSmokeChecks, REVIEWED_CORE_SHA256
from test_conformance import REG as LEGACY_REG

REG = copy.deepcopy(LEGACY_REG)
REG['limits'] = {'request_header_bytes': 10, 'result_header_bytes': 5, 'corr_half_range': 32768}
SPEC = {'commit': 'unit-fixture', 'core_sha256': REVIEWED_CORE_SHA256}


def tag(number, value):
    return bytes([number]) + struct.pack('<H', len(value)) + value


class SmallCore:
    """Test-only logical fixture, with explicit retained/zero-cache variants."""
    def __init__(self, capacity, fault=None, named=False):
        self.capacity, self.fault, self.named = capacity, fault, named
        self.last = self.holder = self.high = None
        self.cache = OrderedDict()
        self.accepted = self.renewals = self.clock = 0

    def effects(self):
        return {'accepted': self.accepted, 'renewals': self.renewals, 'holder': self.holder}

    def send(self, request):
        role, corr, fn, op, sid = struct.unpack_from('<BHHBI', request)
        assert role == 1 and fn == 0 and len(request) <= 64
        body = request[10:]
        def success(data=b''):
            return struct.pack('<BHBB', 2, corr, 1, 0) + data
        def reject(code):
            return struct.pack('<BHBB', 2, corr, 0, code)
        if corr == 0:
            return reject(3)
        if op in (17, 18) and sid == 0:
            return reject(9)
        if sid and sid == self.last and self.fault != 'repeat':
            if corr in self.cache:
                previous, answer = self.cache[corr]
                return answer if request == previous else reject(3)
            delta = (corr - self.high) % 65536
            if not 0 < delta < 32768 or (self.fault == 'no_wrap' and corr < self.high):
                return reject(12)
        if sid and sid != self.holder and not (op == 16 and self.holder is None and sid != self.last):
            answer = reject(7 if self.holder is None else 8)
        elif op == 1:
            assert body == b'OEP?\1\1'
            if self.fault == 'confirm_flags':  # the flags byte removed from confirm (oep-spec a09fa45)
                answer = success(b'OEP!' + struct.pack('<BBHIBI', 1, 0, 64, 64, 1, 17) + tag(1, b'\0'))
            else:
                answer = success(b'OEP!' + struct.pack('<BHIBI', 1, 64, 64, 1, 17) + tag(1, b'\0'))
        elif op == 2:
            assert body == b'\0\0'
            if self.named:
                name = b'io.example.echo'
                entry = struct.pack('<HHBB', 7, 0, 1, len(name)) + name
                if self.fault == 'old_flags':
                    entry = entry[:5] + b'\0' + entry[5:]
                answer = success(struct.pack('<HB', 1, 1) + entry)
            else:
                answer = success(b'\0\0\0')
        elif op == 3:
            assert body == b'\0' * 4
            answer = success(b'\0' + tag(7, b'\0\x1e\0\x0f') + tag(66, b'minimal') +
                             tag(73, b'\0\1\xff') + tag(77, struct.pack('<I', 1)))
        elif op == 4 and self.fault == 'policy_clock':
            answer = reject(4) + tag(1, b'\7')  # core §4.3: fn 0 lock-free queries are never refused by policy
        elif op == 4:
            self.clock += 1
            value = 100 - self.clock if self.fault == 'clock_reverse' else 0
            answer = success(struct.pack('<IQ', 17, value))
        elif op == 16:
            assert body == struct.pack('<IB', 60000, 0) + b'\x01\x0a\0core-smoke'
            self.holder = self.last = sid
            answer = success(struct.pack('<II', 60000, 17))
        elif op == 17:
            self.holder = None
            answer = success()
        elif op == 18:
            answer = success()
        elif op == 19:
            answer = success(struct.pack('<BI', int(self.holder is not None), 60000 if self.holder else 0))
        else:
            answer = reject(2)
        if sid and sid == self.last:
            self.accepted += 1
            self.renewals += int(self.holder == sid)
            self.high = corr
            self.cache[corr] = (request, answer)
            while len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
        return answer


@pytest.mark.parametrize('capacity,named', [(0, False), (1, False), (0, True), (1, True)])
def test_small_core_variants_pass_with_observed_replay_effects(capacity, named):
    probe = SmallCore(capacity, named=named)
    report = CoreSmokeChecks(probe.send, REG, 'minimal', spec=SPEC, observe_effects=probe.effects).run()
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 5 and not report['full_conformance']
    assert report['unfinished_session'] is None and probe.holder is None
    assert [r['result'] for r in report['observed']['replays']] == ['retained' if capacity else 'result_lost'] * 2
    assert all(r['effects_observed'] for r in report['observed']['replays'])
    assert len(report['observed']['interfaces']) == int(named)


@pytest.mark.parametrize('fault,case,named', [('old_flags', 'DISCOVERY', True),
    ('clock_reverse', 'CLOCK', False), ('no_wrap', 'CYCLE', False), ('repeat', 'CYCLE', False),
    ('confirm_flags', 'CONFIRM', False), ('policy_clock', 'CLOCK', False)])
def test_observable_faults_fail_the_appropriate_smoke_case(fault, case, named):
    probe = SmallCore(0, fault=fault, named=named)
    report = CoreSmokeChecks(probe.send, REG, 'minimal', spec=SPEC, observe_effects=probe.effects).run()
    assert report['status'] == 'failed'
    assert any(r['id'].endswith(case) and r['status'] == 'failed' for r in report['checks'])


def test_response_only_scope_does_not_claim_observed_side_effects():
    probe = SmallCore(0)
    report = CoreSmokeChecks(probe.send, REG, 'minimal', spec=SPEC).run()
    assert report['status'] == 'passed'
    assert 'replay side effects not independently observed' in report['unchecked']


def test_unsupported_preserves_a_known_noncritical_tlv_tag():
    probe = SmallCore(0)
    check = CoreSmokeChecks(probe.send, REG, 'minimal', spec=SPEC)
    check.rejected(struct.pack('<BHBB', 2, 1, 0, 11) + b'\x40', 'unsupported')
