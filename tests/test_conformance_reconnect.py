"""Retention checks challenged with independent, literal protocol responses."""
import copy
import struct

import pytest

from oep_client.conformance import Checks
from test_conformance import REG


class Probe:
    def __init__(self, monkeypatch, fault=None):
        self.now, self.boot, self.clock_value = 0, 17, 100
        self.sid, self.deadline = None, 0
        self.retired, self.cache = set(), {}
        self.fault, self.closed, self.after_reopen = fault, 0, []
        monkeypatch.setattr('oep_client.conformance.time.sleep', self.sleep)

    def sleep(self, seconds):
        self.now += seconds * 1000

    def reopen(self):
        self.closed += 1
        if self.fault == 'release':
            self.sid = None
        elif self.fault == 'cache':
            self.cache.clear()
        elif self.fault == 'ended':
            self.retired.clear()
        elif self.fault == 'lease':
            self.deadline = self.now + 1000
        elif self.fault == 'boot':
            self.boot += 1
        self.sleep(0.05)

    def send(self, req):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', req)
        if self.closed:
            self.after_reopen.append(op)
        if self.sid is not None and self.now >= self.deadline:
            self.retired.add(self.sid)
            self.sid = None
        key = (sid, corr)
        if sid and key in self.cache:
            return self.cache[key]
        status, detail, payload = 1, 0, b''
        if op == 1:
            if self.fault == 'confirm' and self.closed:
                return struct.pack('<BHBB', 2, corr, 1, 0) + b'bad confirm'
            payload = b'OEP!' + struct.pack('<BBHIBI', 1, 0, 64, 64, 1, self.boot) + b'\x01\x01\0\0'
        elif op == 3:
            first = struct.unpack_from('<H', req, 12)[0]
            unit = b'wrong' if self.fault == 'unit' and self.closed else b'unit'
            payload = b'\0' + (b'\x42' + struct.pack('<H', len(unit)) + unit if first == 0 else b'')
        elif op == 16:
            if sid in self.retired:
                status, detail = 0, 7
            else:
                self.sid = sid
                lease = struct.unpack_from('<I', req, 10)[0]
                self.deadline = self.now + lease
                payload = struct.pack('<II', lease, self.boot)
        elif op == 17:
            self.retired.add(sid)
            self.sid = None
        elif op == 18:
            if self.sid != sid:
                status, detail = 0, 7
        elif op == 19:
            payload = struct.pack('<BI', int(self.sid is not None),
                                  int(self.deadline - self.now) if self.sid else 0)
        elif op == 4:
            self.clock_value += 1
            payload = struct.pack('<IQ', self.boot, self.clock_value)
        else:
            raise AssertionError(op)
        reply = struct.pack('<BHBB', 2, corr, status, detail) + payload
        if sid:
            self.cache[key] = reply
        return reply


@pytest.mark.parametrize('kind', ['session', 'replay', 'ended', 'lease'])
def test_reconnect_retention_accepts_compliant_responses(monkeypatch, kind):
    probe = Probe(monkeypatch)
    check = Checks(probe.send, copy.deepcopy(REG), 'unit')
    check.reopen = probe.reopen
    check.confirm()
    check.check('reconnect', 'transports §3', lambda: check.reconnect(kind))
    assert check.results[-1]['status'] == 'passed', check.results[-1]
    assert probe.closed == 1 and probe.sid is None
    assert any('reconnect' in x for x in check.trace)


@pytest.mark.parametrize('kind,fault,error', [
    ('session', 'release', 'lock'), ('replay', 'cache', 'history'),
    ('ended', 'ended', 'ended session'), ('lease', 'lease', 'lease'),
    ('session', 'boot', 'restarted'), ('session', 'unit', 'unit_id'),
    ('session', 'confirm', 'confirm'),
])
def test_reconnect_retention_detects_mutants(monkeypatch, kind, fault, error):
    probe = Probe(monkeypatch, fault)
    check = Checks(probe.send, copy.deepcopy(REG), 'unit')
    check.reopen = probe.reopen
    check.confirm()
    check.check('reconnect', 'transports §3', lambda: check.reconnect(kind))
    assert check.results[-1]['status'] == 'failed'
    assert error in check.results[-1]['error']
    if fault in ('boot', 'unit', 'confirm'):
        assert check.abort and check.session is None
        assert all(op in (1, 3) for op in probe.after_reopen), 'mutated a restarted/replaced endpoint'


def test_identity_selection_is_case_insensitive(monkeypatch):
    probe = Probe(monkeypatch)
    check = Checks(probe.send, copy.deepcopy(REG), 'UNIT')
    check.confirm()
    check.identity()
    assert check.observed['unit_id'] == 'unit'


@pytest.mark.parametrize('kind,interface,passed', [(1, 255, True), (6, 255, True),
                                                 (1, 0, False), (6, 0, False),
                                                 (2, 0, True), (7, 0, False)])
def test_transport_declaration_independent_of_ops_tag(kind, interface, passed):
    check = Checks(lambda _: None, copy.deepcopy(REG), 'unit')
    check.confirm_transport = 0
    check.observed['core_describe'] = [
        {'tag': 73, 'value_hex': bytes((0, kind, interface)).hex()},
        {'tag': 77, 'value_hex': '01000000'},
    ]
    check.check('transport', 'core §7.5', check.transport_declarations)
    assert check.results[-1]['status'] == ('passed' if passed else 'failed')


@pytest.mark.parametrize('length,passed', [(55, True), (56, False)])
def test_describe_value_bound_includes_result_header_and_more(length, passed):
    check = Checks(lambda _: None, copy.deepcopy(REG), 'unit')
    check.observed['core_describe'] = [{'tag': 127, 'value_hex': (b'x' * length).hex()}]
    check.check('size', 'core §7.3', check.describe_size)
    assert check.results[-1]['status'] == ('passed' if passed else 'failed')


@pytest.mark.parametrize('clear_history', [False, True])
def test_repeat_confirm_preserves_replay_history(monkeypatch, clear_history):
    probe = Probe(monkeypatch)

    def send(req):
        if req[5] == 1 and probe.sid is not None and clear_history:
            probe.cache.clear()
        return probe.send(req)

    check = Checks(send, copy.deepcopy(REG), 'unit')
    check.confirm()
    check.check('confirm-history', 'core §5.2', check.confirm_history)
    assert check.results[-1]['status'] == ('failed' if clear_history else 'passed')
    assert probe.sid is None


@pytest.mark.parametrize('release_lock', [False, True])
def test_discovery_does_not_end_owned_session(monkeypatch, release_lock):
    probe = Probe(monkeypatch)

    def send(req):
        if req[5] == 1 and probe.sid is not None and release_lock:
            probe.sid = None
        if req[5] == 2:
            return b'\x02' + req[1:3] + b'\x01\0\0\0\0'
        return probe.send(req)

    check = Checks(send, copy.deepcopy(REG), 'unit')
    check.confirm()
    check.observed['interfaces'] = []
    check.check('discovery', 'core §6.3', check.discovery_locked)
    assert check.results[-1]['status'] == ('failed' if release_lock else 'passed')
    assert probe.sid is None
