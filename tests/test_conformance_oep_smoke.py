"""Small independent OEP example fixture; no production endpoint or devices."""
import copy
import struct

import pytest

from oep_client.conformance_core_smoke import CoreSmokeChecks
from oep_client.conformance_oep_smoke import OepExampleSmokeChecks, REVIEWED_INTERFACES
from test_conformance_core_smoke import SmallCore, REG, SPEC, tag

NAMES = {3: 'oep.probe.plan', 4: 'oep.fixture.gpio', 5: 'oep.fixture.uart', 8: 'oep.wire.swd', 9: 'oep.target.arm-adi'}
OPERATIONS = {3: {'plan_apply': 16, 'plan_release': 17}, 4: {'set': 16, 'read': 17},
    5: {'configure': 16, 'read': 17, 'marks': 18, 'clear': 19, 'mark': 20, 'write': 21, 'status': 22},
    8: {'scan': 16, 'attach': 17, 'detach': 18, 'connections': 19},
    9: {'transfer': 16, 'read_block': 17, 'write_block': 18}}
REGISTRY = copy.deepcopy(REG)
REGISTRY['interface'] = [{'name': name, 'op': [{'name': op, 'code': code} for op, code in OPERATIONS[fn].items()]} for fn, name in NAMES.items()]
METADATA = dict(SPEC, interface_sha256=REVIEWED_INTERFACES)
PINS = {'gpio': (0, 1), 'uart_rx': (2,), 'swd': (3, 4)}


class ExampleCore(SmallCore):
    def __init__(self, fault=None, input_only=False):
        super().__init__(0)
        self.oep_fault, self.input_only = fault, input_only
        self.plans, self.connections = {}, {}
        self.modes = {n: 0 for n in range(5)}
        self.buffer, self.position = b'', 0
        self.gpio_applies = self.target_calls = 0
        self.uart_baud = 0
        self.next_resource = 0x10001  # u32 numbers (core §9); earlier allocations/gaps put this past u16

    def resources(self):
        # Exclude valid core order/lease updates and allowed allocation-ID gaps.
        return {'plans': copy.deepcopy(self.plans), 'connections': copy.deepcopy(self.connections),
                'gpio_modes': dict(self.modes), 'uart_buffer': self.buffer.hex(),
                'uart_position': self.position, 'uart_baud': self.uart_baud, 'gpio_applies': self.gpio_applies,
                'target_calls': self.target_calls}

    def released(self):
        return (not self.plans and not self.connections and not self.buffer and not any(self.modes.values())
                and not self.uart_baud)

    def feed(self, data):
        assert self.plans.get(5) == [(1, 2)] and self.uart_baud  # receiving starts at an explicit configure
        self.buffer += data; self.position += len(data)

    def send(self, request):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', request); body = request[10:]
        def success(data=b''):
            return struct.pack('<BHBB', 2, corr, 1, 0) + data
        def reject(code, tail=b''):
            return struct.pack('<BHBB', 2, corr, 0, code) + tail
        if fn == 0 and op == 2:
            first, = struct.unpack('<H', body); entries = []
            for number in sorted(NAMES)[first:]:
                name = NAMES[number].encode()
                entry = struct.pack('<HHBB', number, 0, 1, len(name)) + name
                if 8 + sum(map(len, entries)) + len(entry) > 64:
                    break
                entries.append(entry)
            return success(struct.pack('<HB', len(NAMES), len(entries)) + b''.join(entries))
        if fn == 0 and op == 3:
            target, first = struct.unpack('<HH', body)
            if target:
                assert first == 0
                bitmap = {3: 3, 4: 3, 5: 0x43, 8: 14, 9: 1}[target]
                data = tag(7, bytes([16, bitmap]))
                if target == 4:
                    data += tag(1, b'\x01\0\0\x03') + tag(0x40, struct.pack('<I', 1 if self.input_only else 0x19))
                if target == 5:
                    data += tag(1, b'\x01\0\0\x04') + tag(0x40, b'\x01\0')
                if target == 8:
                    data += tag(1, b'\x01\0\0\x08') + tag(1, b'\x02\0\0\x10')
                return success(b'\0' + data)
        if fn == 0:
            result = super().send(request)
            if op == 3:
                result += tag(67, b'\5\0')
            if op == 17 and result[3:5] == b'\1\0' and self.oep_fault != 'end_leak':
                self.plans.clear(); self.connections.clear(); self.buffer = b''; self.uart_baud = 0
                self.modes = {n: 0 for n in range(5)}
            return result
        offered = {3: {16, 17}, 4: {16, 17}, 5: {16, 17, 22}, 8: {17, 18, 19}, 9: {16}}
        if op not in offered.get(fn, set()):
            return reject(2)
        if sid and sid != self.holder:
            return reject(7 if self.holder is None else 8)
        if fn == 3:
            assert op == 16
            assignments = {}; at = 0
            while at < len(body):
                assert body[at:at+3] == b'\x10\5\0'
                owner, role, channel = struct.unpack_from('<HBH', body, at + 3)
                assignments.setdefault(owner, []).append((role, channel)); at += 8
            self.plans.update(assignments)
            return success()
        if fn == 4:
            count = body[0]
            if op == 16:
                items = [struct.unpack_from('<HB', body, 1 + 3*i) for i in range(count)]
                if any(mode not in ({0} if self.input_only else {0, 3, 4}) for _, mode in items):
                    if self.oep_fault == 'gpio_partial':
                        ch, mode = items[0]; self.modes[ch] = mode; self.gpio_applies += 1
                    return reject(11, b'\0')
                for channel, mode in items:
                    self.modes[channel] = mode; self.gpio_applies += 1
                return success()
            channels = [struct.unpack_from('<H', body, 1 + 2*i)[0] for i in range(count)]
            result = success(bytes([count]) + bytes([int(self.modes[ch] == 4) for ch in channels]))
            if self.oep_fault == 'gpio_read_changes':
                self.gpio_applies += 1
            return result
        if fn == 5:
            if op == 16:
                self.uart_baud = 115200
                return success(struct.pack('<I', 115200))
            if op == 22:
                if self.oep_fault == 'uart_status_default':  # the removed implicit 115200 8N1 before configure
                    return success(struct.pack('<IB', self.uart_baud or 115200, 0))
                return success(struct.pack('<IB', self.uart_baud, 0))
            source, arg, maximum = struct.unpack('<BQH', body)
            if source == 3:
                return reject(11, b'\0')
            data = self.buffer[:maximum]
            result = success(struct.pack('<QBH', self.position - len(self.buffer), 0, len(data)) + data)
            if self.oep_fault == 'uart_consumes':
                self.buffer = self.buffer[len(data):]
            return result
        if fn == 8:
            assert op == 17
            if self.oep_fault == 'attach_tlv_only':  # an old probe wanting max_speed/pins as TLVs
                return reject(3)
            method, swdio, swclk, max_speed = struct.unpack_from('<BHHI', body)
            assert len(body) == 9 and (method, swdio, swclk) == (0, 3, 4) and max_speed == 1000000
            number = self.next_resource; self.next_resource += 1
            self.connections[number] = {'wire_fn': 8, 'pins': [3, 4], 'sid': sid}
            if self.oep_fault == 'connection_u16':
                return success(struct.pack('<HIBI', number & 0xFFFF, 0x1BA01477, 0, 1000000))
            return success(struct.pack('<IIBI', number, 0x1BA01477, 0, 1000000))
        assert fn == 9
        connection, count = struct.unpack_from('<IH', body)
        if connection not in self.connections:
            return reject(10)
        if self.oep_fault == 'cross_fn':
            return reject(4)
        if 11 + 4 * count > 64:
            if self.oep_fault == 'overflow_io':
                self.target_calls += 1
            return reject(11, b'\0')
        assert body[6:] == bytes([2]) * count
        self.target_calls += count
        return success(struct.pack('<HBBH', count, 0, 1, count) + struct.pack('<I', 0x1BA01477) * count)


def execute(fault=None, input_only=False, stimulus=True):
    probe = ExampleCore(fault, input_only)
    core = CoreSmokeChecks(probe.send, REGISTRY, 'minimal', spec=METADATA)
    runner = OepExampleSmokeChecks(core, pins=PINS, observe_resources=probe.resources,
                                  resources_released=probe.released, feed_uart=probe.feed if stimulus else None)
    return probe, runner.run()


@pytest.mark.parametrize('input_only', [False, True])
def test_oep_representative_scenarios_pass(input_only):
    probe, report = execute(input_only=input_only)
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 4 and not report['full_conformance']
    assert report['unfinished_session'] is None and probe.released()
    assert probe.target_calls == 1


@pytest.mark.parametrize('fault,case', [('gpio_partial', 'GPIO'), ('gpio_read_changes', 'GPIO'),
    ('uart_consumes', 'UART'), ('uart_status_default', 'UART'), ('cross_fn', 'ARM'), ('overflow_io', 'ARM'),
    ('attach_tlv_only', 'ARM'), ('connection_u16', 'ARM'), ('end_leak', 'GPIO')])
def test_oep_representative_faults_are_detected(fault, case):
    _, report = execute(fault)
    assert report['status'] == 'failed'
    assert any(row['id'] == 'OEP-SMOKE-' + case and row['status'] == 'failed' for row in report['checks'])


def test_missing_stimulus_is_not_a_pass_or_equipment_skip():
    _, report = execute(stimulus=False)
    assert report['status'] == 'incomplete'
    assert next(row for row in report['checks'] if row['id'] == 'OEP-SMOKE-UART')['status'] == 'not_run'
