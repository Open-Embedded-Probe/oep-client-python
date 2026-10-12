"""Exercise shared interface checks without standard-operation assumptions."""
import copy
import struct

import pytest

from oep_client.conformance_core_smoke import CoreSmokeChecks
from oep_client.conformance_interface_smoke import InterfaceSmokeChecks
from test_conformance_core_smoke import SmallCore, REG, SPEC, tag

REGISTRY = copy.deepcopy(REG)
REGISTRY['describe_common'].update(role_channels=1, max_clock_hz=2, max_length=3,
                                  min_clock_hz=4, features=5, channel_group=6)


class ExtensionCore(SmallCore):
    def __init__(self, profile='independent', fault=None):
        super().__init__(0)
        self.profile, self.extension_fault = profile, fault
        self.named_effects = self.description_calls = 0

    def effects(self):
        return dict(super().effects(), named_effects=self.named_effects)

    def send(self, request):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', request)
        body = request[10:]
        def success(data):
            return struct.pack('<BHBB', 2, corr, 1, 0) + data
        if fn:
            assert fn == 7
            if self.extension_fault == 'mutate_rejection':
                self.named_effects += 1
            return struct.pack('<BHBB', 2, corr, 0, 9 if self.extension_fault == 'session_first' else 2)
        if op == 2 and self.profile != 'none':
            name = b'oep.fixture.uart' if self.profile == 'uart' else b'io.example.echo'
            if self.extension_fault == 'bad_name':
                name = b'io..echo'
            return success(struct.pack('<HBHHBB', 1, 1, 7, 0, 1, len(name)) + name)
        if op == 3 and body[:2] == b'\x07\0':
            assert body == b'\x07\0\0\0'
            self.description_calls += 1
            offered = 0x43 if self.profile == 'uart' else 1
            if self.extension_fault == 'subscription_pair':
                content = tag(7, b'\0\x02')
            else:
                if self.extension_fault == 'unstable' and self.description_calls > 1:
                    offered |= 2
                content = tag(7, bytes([0x10, offered]))
            if self.profile == 'uart':
                content += tag(1, b'\x01\0\0\x01') + tag(0x40, b'\x01\0')
            return success(b'\0' + content)
        result = super().send(request)
        if op == 3:
            result += tag(67, b'\x01\0')
        return result


def run(profile='independent', fault=None, observe=True):
    probe = ExtensionCore(profile, fault)
    core = CoreSmokeChecks(probe.send, REGISTRY, 'minimal', spec=SPEC,
                           observe_effects=probe.effects if observe else None)
    return InterfaceSmokeChecks(core).run()


@pytest.mark.parametrize('profile', ['none', 'independent', 'uart'])
def test_shared_contract_accepts_small_profiles(profile):
    report = run(profile)
    assert report['status'] == 'passed', report['checks']
    assert not report['full_conformance'] and report['levels']['oep-interface'] == 'not executed'
    assert len(report['checks']) == (1 if profile == 'none' else 8)
    assert bool(report['not_applicable']) == (profile == 'none')


@pytest.mark.parametrize('fault,case', [('bad_name', 'NAME'), ('subscription_pair', 'OPS'),
    ('unstable', 'STABLE'), ('session_first', 'ABSENT'), ('mutate_rejection', 'ABSENT')])
def test_representative_shared_contract_faults_are_detected(fault, case):
    report = run(fault=fault)
    assert report['status'] == 'failed'
    assert any(row['id'].startswith('IF-SMOKE-' + case) and row['status'] == 'failed' for row in report['checks'])


def test_rejection_without_observer_leaves_effects_unchecked():
    report = run(observe=False)
    assert report['status'] == 'passed'
    assert 'rejection side effects not independently observed' in report['unchecked']
