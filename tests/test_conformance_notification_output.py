"""Selected raw subscription/output contracts on a bounded logical fixture."""
from collections import deque
import copy
import struct

import pytest

from oep_client.conformance_core_smoke import CoreSmokeChecks
from test_conformance_core_smoke import REG, SPEC
from test_conformance_session_smoke import TimedCore


class NotificationCore(TimedCore):
    def __init__(self, capacity, fault=None):
        super().__init__(capacity)
        self.output_fault = fault
        self.subscription = None

    def send(self, request):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', request)
        body = request[10:]
        def success(data=b''):
            return struct.pack('<BHBB', 2, corr, 1, 0) + data
        def reject(code, data=b''):
            return struct.pack('<BHBB', 2, corr, 0, code) + data
        if not sid and fn == 0 and op == 2:
            name = b'io.example.notice'
            return success(struct.pack('<HBHHBB', 1, 1, 7, 0, 1, len(name)) + name)
        if not sid and fn == 0 and op == 3 and body[:2] == b'\x07\0':
            return success(b'\0\x07\x02\0\0\x06')
        if fn != 7:
            answer = super().send(request)
            if self.holder is None:
                self.subscription = None
            return answer
        if not corr:
            return reject(3)
        if op not in (1, 2):
            return reject(2)
        if not sid:
            return reject(9)
        if sid == self.last:
            if corr in self.cache:
                previous, answer = self.cache[corr]
                return answer if request == previous else reject(3)
            if not 0 < (corr - self.high) % 65536 < 32768:
                return reject(12)
        if sid != self.holder:
            return reject(8, struct.pack('<I', self.deadline - self.now)) if self.holder else reject(7)
        self.high = corr
        if op == 1:
            minimum, delay = struct.unpack('<HI', body)
            if self.output_fault == 'rejected_seq_reset' and self.subscription:
                self.subscription['seq'] = 0
            if minimum > 48 and delay == 0:
                answer = reject(4, b'\x01\x01\0\x02')
            else:
                self.subscription = dict(min_bytes=minimum, max_delay_ms=delay, seq=0)
                answer = success()
        else:
            assert body == b''
            self.subscription = None
            answer = success()
        self.deadline = self.now + 1000
        self.accepted += 1; self.renewals += 1
        self.cache[corr] = (request, answer)
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return answer


class BoundedOutput:
    """One notification slot and one current response, pumped without blocking."""
    def __init__(self, probe):
        self.probe = probe
        self.active = self.notice = None
        self.responses = deque()
        self.peak_notification_bytes = 0
        self.wire_batches = []
        self.wire_writes = []
        self.raw_requests = []

    @staticmethod
    def packet(message):
        return struct.pack('<H', len(message)) + message

    def submit(self, request):
        assert not self.responses and not (self.active and self.active[0] == 'response')
        self.raw_requests.append(request.hex())
        previous_subscription = self.probe.subscription
        self.responses.append(self.packet(self.probe.send(request)))
        retain = self.probe.output_fault == 'retain_old_notice' or (
            self.probe.output_fault == 'retain_on_unsubscribe' and self.probe.subscription is None)
        if self.probe.subscription is not previous_subscription and not retain:
            self.notice = None

    def event(self, token):
        if self.probe.subscription is None:
            return None
        seq = self.probe.subscription['seq']
        self.probe.subscription['seq'] = (seq + 1) & 65535
        message = struct.pack('<BHHBI', 3, 7, seq, 1, token) + bytes(54)
        assert len(message) == 64
        if self.notice is not None or (self.active and self.active[0] == 'notice'):
            if self.probe.output_fault == 'reuse_dropped_seq':
                self.probe.subscription['seq'] = seq
            return None
        self.notice = self.packet(message)
        self.peak_notification_bytes = max(self.peak_notification_bytes, len(self.notice))
        return message

    def pump(self, credit=7):
        if self.active and self.active[0] == 'notice' and self.responses and self.probe.output_fault == 'interleave':
            # Deliberately insert the reply before the old frame's remainder.
            chunk = self.responses.popleft()
            self.wire_writes.append(chunk.hex())
            return chunk
        if self.active is None:
            if self.notice and self.probe.output_fault == 'notice_first':
                self.active, self.notice = ('notice', self.notice), None
            elif self.responses:
                self.active = ('response', self.responses.popleft())
            elif self.notice:
                self.active, self.notice = ('notice', self.notice), None
            else:
                return b''
        kind, remaining = self.active
        chunk, remaining = remaining[:credit], remaining[credit:]
        self.active = (kind, remaining) if remaining else None
        self.wire_writes.append(chunk.hex())
        return chunk

    def drain(self):
        stream = bytearray()
        while self.active or self.notice or self.responses:
            stream.extend(self.pump())
        self.wire_batches.append(bytes(stream).hex())
        return bytes(stream)

    def exchange(self, request):
        self.submit(request)
        messages = decode(self.drain())
        assert len(messages) == 1 and messages[0][0] == 2
        return messages[0]


def decode(stream):
    messages = []
    while stream:
        assert len(stream) >= 2, 'short length prefix'
        length = int.from_bytes(stream[:2], 'little')
        assert 5 <= length <= 64 and len(stream) >= 2 + length, 'corrupt message length'
        messages.append(stream[2:2+length]); stream = stream[2+length:]
    return messages


def run(capacity=1, fault=None, registry=REG, spec=SPEC):
    probe = NotificationCore(capacity, fault)
    output = BoundedOutput(probe)
    core = CoreSmokeChecks(output.exchange, registry, 'minimal', spec=spec)
    report = dict(full_conformance=False, scope='selected raw subscription/event output with seven-byte logical writes',
                  observations=[], checks=[])
    case = 'CONTEXT'
    try:
        core.confirm(); core.discovery()
        core.success(core.call('open', struct.pack('<IB', 1000, 0), sid=1, corr=1))
        core.success(core.call(1, struct.pack('<HI', 16, 0), fn=7, sid=1, corr=2))
        report['checks'].append(dict(id=case, status='passed'))
        case = 'REJECTED-SUBSCRIBE'
        output.event(10); decode(output.drain())
        before = copy.deepcopy(probe.subscription)
        reply = core.call(1, struct.pack('<HI', 65535, 0), fn=7, sid=1, corr=3)
        core.rejected(reply, 'unavailable')
        assert reply[5:] == b'\x01\x01\0\x02'
        assert probe.subscription == before, 'rejected subscribe changed prior conditions/seq'
        report['observations'].append(dict(action=case, before=before, after=copy.deepcopy(probe.subscription)))
        report['checks'].append(dict(id=case, status='passed'))
        case = 'REPLACEMENT'
        output.event(11)  # Old subscription's notification has not started.
        core.success(core.call(1, struct.pack('<HI', 1, 0), fn=7, sid=1, corr=4))
        assert probe.subscription['seq'] == 0
        assert output.drain() == b'', 'old unsent notification survived subscription replacement'
        report['checks'].append(dict(id=case, status='passed'))
        case = 'PARTIAL-FRAME'
        first = output.event(20)
        prefix = output.pump(7)
        assert len(prefix) == 7
        assert output.event(21) is None
        # Explicit clock opcode, independent of registry list order.
        request = struct.pack('<BHHBI', 1, 100, 0, core.core['clock'], 0)
        output.submit(request)
        messages = decode(prefix + output.drain())
        assert len(messages) == 2 and messages[0] == first and messages[1][:5] == struct.pack('<BHBB', 2, 100, 1, 0), 'partial frame was interrupted'
        next_event = output.event(22)
        assert struct.unpack_from('<H', next_event, 3)[0] == 2, 'dropped event did not consume seq'
        assert decode(output.drain()) == [next_event]
        assert output.peak_notification_bytes <= 128
        report['observations'].append(dict(action=case, delivered_seq=[0, 2], peak_notification_bytes=output.peak_notification_bytes))
        report['checks'].append(dict(id=case, status='passed'))
        case = 'RESPONSE-PRIORITY'
        notice = output.event(30)
        output.submit(struct.pack('<BHHBI', 1, 101, 0, core.core['clock'], 0))
        messages = decode(output.drain())
        assert len(messages) == 2 and messages[0][:5] == struct.pack('<BHBB', 2, 101, 1, 0) and messages[1] == notice, 'new notice preceded waiting response'
        report['checks'].append(dict(id=case, status='passed'))
        case = 'UNSUBSCRIBE'
        assert output.event(39) is not None  # unsent when unsubscribe arrives
        output.submit(struct.pack('<BHHBI', 1, 5, 7, 2, 1))
        messages = decode(output.drain())
        assert len(messages) == 1 and messages[0][:5] == struct.pack('<BHBB', 2, 5, 1, 0), 'old unsent notification survived unsubscribe'
        assert output.event(40) is None and output.drain() == b''
        core.success(core.call('end', sid=1, corr=6))
        report['checks'].append(dict(id=case, status='passed'))
        report['status'] = 'passed'
    except Exception as exc:
        report['checks'].append(dict(id=case, status='failed', error=f'{type(exc).__name__}: {exc}'))
        report['status'] = 'failed'
    report.update(exchanges=core.trace, wire_batches=output.wire_batches,
                  wire_writes=output.wire_writes,
                  raw_requests=output.raw_requests,
                  unfinished_session=probe.holder, peak_notification_bytes=output.peak_notification_bytes)
    return report


@pytest.mark.parametrize('capacity', [0, 1])
def test_bounded_raw_notification_output(capacity):
    report = run(capacity)
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 6 and report['unfinished_session'] is None


@pytest.mark.parametrize('fault,case', [('rejected_seq_reset', 'REJECTED-SUBSCRIBE'),
    ('retain_old_notice', 'REPLACEMENT'), ('interleave', 'PARTIAL-FRAME'),
    ('reuse_dropped_seq', 'PARTIAL-FRAME'), ('notice_first', 'RESPONSE-PRIORITY'),
    ('retain_on_unsubscribe', 'UNSUBSCRIBE')])
def test_output_faults(fault, case):
    report = run(fault=fault)
    assert report['status'] == 'failed'
    assert report['checks'][-1]['id'] == case
