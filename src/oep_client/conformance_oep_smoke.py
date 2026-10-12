"""Representative OEP scenarios; adapters supply explicit pins and observations."""
import copy
import hashlib
from pathlib import Path
import struct

from .conformance import ops, require, tlvs

REVIEWED_INTERFACES = {
    'oep-if-fixture.ja.md': '102ab57140fd7f4fd00f0e731b1a61e78c1ffd2db484c32c6bc5b2afa73d4921',
    'oep-if-plan.ja.md': '1b785a2c41934d6f1e29e663ca61a1a62049da5c4b094eb345f9a9d981ccbe1a',
    'oep-if-debug.ja.md': 'd32c71b5a4d8a3d8f52f6b09d54530876116c79f026d9756c1cfe122b6b312f7',
    'oep-if-common.ja.md': 'b6b6c065f6c30a0b4fbc94229e0f12746eb060ca440dfbde21920b4e0f5af0ac',
}


def tag(number, data):
    return bytes([number]) + struct.pack('<H', len(data)) + data


class OepExampleSmokeChecks:
    def __init__(self, core, *, pins, observe_resources, resources_released, feed_uart=None, functions=None):
        require(all(core.spec.get('interface_sha256', {}).get(name) == digest for name, digest in REVIEWED_INTERFACES.items()),
                'OEP smoke interface documents need review')
        require(callable(observe_resources) and callable(resources_released), 'explicit resource observations required')
        self.c, self.pins = core, dict(pins)
        self.selected = dict(functions or {})
        self.observe, self.released, self.feed = observe_resources, resources_released, feed_uart
        self.operations = {row['name']: {op['name']: op['code'] for op in row['op']} for row in core.reg['interface']}
        self.results, self.observations = [], []

    def prepare(self):
        self.c.confirm(); self.c.discovery()
        self.functions, self.declared, self.descriptions = {}, {}, {}
        required = {'oep.probe.plan': ('plan_apply', 'plan_release'), 'oep.fixture.gpio': ('set', 'read'),
                    'oep.fixture.uart': ('configure', 'read', 'status'), 'oep.wire.swd': ('attach', 'detach', 'connections'),
                    'oep.target.arm-adi': ('transfer',)}
        for entry in self.c.observed['interfaces']:
            if entry['name'] not in required or (entry['name'] in self.selected and self.selected[entry['name']] != entry['fn']):
                continue
            require(entry['name'] not in self.functions, 'explicit example function selection needed for multiple instances')
            self.functions[entry['name']] = entry['fn']
            rows = self.c.describe(entry['fn']); self.descriptions[entry['name']] = dict(reversed(rows))
            self.declared[entry['name']] = ops(self.descriptions[entry['name']][7])
            require({self.operations[entry['name']][op] for op in required[entry['name']]} <= self.declared[entry['name']],
                    entry['name'] + ' mandatory operations absent')

    def start(self):
        self.c.open_session(); self.corr = 1

    def call(self, name, operation, body=b'', *, unlocked=False):
        code = self.operations[name][operation]
        require(code in self.declared[name], name + ' required selected operation absent')
        if unlocked:
            return self.c.call(code, body, fn=self.functions[name])
        self.corr += 1
        return self.c.call(code, body, fn=self.functions[name], sid=self.c.session, corr=self.corr)

    def apply(self, name, assignments):
        body = b''.join(tag(0x10, struct.pack('<HBH', self.functions[name], role, channel)) for role, channel in assignments)
        tlvs(self.c.success(self.call('oep.probe.plan', 'plan_apply', body)))

    def reject_unchanged(self, action, reason, label):
        before = copy.deepcopy(self.observe())
        reply = action()
        self.c.rejected(reply, reason)
        if reason == 'unsupported':
            require(reply[5] == 0, 'expected fixed-field unsupported marker')
        after = copy.deepcopy(self.observe())
        self.observations.append({'action': label, 'before': before, 'after': after})
        require(before == after, 'rejection changed observed resources/target')

    def finish(self):
        self.corr += 1
        tlvs(self.c.success(self.c.call('end', sid=self.c.session, corr=self.corr)))
        self.c.session = None
        snapshot = copy.deepcopy(self.observe())
        self.observations.append({'action': 'end', 'resources': snapshot})
        require(self.released(), 'session end leaked selected resources')

    def gpio(self):
        name = 'oep.fixture.gpio'; first, second = self.pins['gpio']
        self.start(); self.apply(name, [(1, first), (1, second)])
        modes = self.descriptions[name].get(0x40, b'')
        require(len(modes) == 4 and int.from_bytes(modes, 'little') & 1, 'GPIO input mode declaration')
        has_outputs = int.from_bytes(modes, 'little') & 0x18 == 0x18
        initial, changed = (3, 4) if has_outputs else (0, 0)
        tlvs(self.c.success(self.call(name, 'set', bytes([1]) + struct.pack('<HB', first, initial))))
        before = copy.deepcopy(self.observe())
        body = self.c.success(self.call(name, 'read', bytes([1]) + struct.pack('<H', first), unlocked=True))
        require(len(body) >= 2 and body[0] == 1 and body[1] in (0, 1), 'GPIO read shape')
        if has_outputs:
            require(body[1] == 0, 'GPIO low output/read mismatch')
        tlvs(body[2:]); after = copy.deepcopy(self.observe())
        self.observations.append({'action': 'GPIO read', 'before': before, 'after': after})
        require(after == before, 'GPIO read changed resources')
        bad = bytes([2]) + struct.pack('<HBHB', first, changed, second, 7)
        self.reject_unchanged(lambda: self.call(name, 'set', bad), 'unsupported', 'GPIO invalid batch before effects')
        self.finish()

    def uart(self):
        name = 'oep.fixture.uart'; rx, = self.pins['uart_rx']
        self.start(); self.apply(name, [(1, rx)])
        p = self.c.success(self.call(name, 'configure', struct.pack('<I', 115200)))
        require(len(p) >= 4 and abs(int.from_bytes(p[:4], 'little') - 115200) * 100 <= 115200 * 5, 'UART actual baud')
        tlvs(p[4:])
        self.feed(b'AB')
        before = copy.deepcopy(self.observe()); reads = []
        for _ in range(2):
            p = self.c.success(self.call(name, 'read', struct.pack('<BQH', 1, 0, 2), unlocked=True))
            require(len(p) >= 11, 'UART read prefix')
            position, flags, length = struct.unpack_from('<QBH', p)
            self.observations.append({'action': 'UART read', 'position': position, 'flags': flags, 'length': length,
                                      'data_hex': p[11:11+length].hex(), 'resources': copy.deepcopy(self.observe())})
            require(length == 2 and p[11:13] == b'AB', 'UART nonconsuming data')
            tlvs(p[13:]); reads.append((position, flags, length))
        require(reads[0] == reads[1] and self.observe() == before, 'UART read consumed/changed data')
        if self.operations[name]['marks'] not in self.declared[name]:
            self.reject_unchanged(lambda: self.call(name, 'read', struct.pack('<BQH', 3, 0, 2), unlocked=True),
                                  'unsupported', 'UART marks absent')
        if self.operations[name]['write'] not in self.declared[name]:
            self.reject_unchanged(lambda: self.c.call(self.operations[name]['write'], b'\1\0X', fn=self.functions[name]),
                                  'unknown_operation', 'UART write absent')
        self.finish()

    def arm(self):
        wire, target = 'oep.wire.swd', 'oep.target.arm-adi'
        self.start()
        payload = b'\0' + tag(0x81, struct.pack('<I', 1000000)) + tag(0x83, struct.pack('<HH', *self.pins['swd']))
        p = self.c.success(self.call(wire, 'attach', payload))
        require(len(p) >= 11, 'SWD attach prefix')
        connection, identity, flags, speed = struct.unpack_from('<HIBI', p)
        require(connection and 0 < speed <= 1000000, 'SWD connection/speed'); tlvs(p[11:])
        p = self.c.success(self.call(target, 'transfer', struct.pack('<HHB', connection, 1, 2)))
        require(len(p) >= 10 and struct.unpack_from('<HBBH', p) == (1, 0, 1, 1), 'raw ARM transfer progress/ACK/value count')
        require(struct.unpack_from('<I', p, 6)[0] == identity, 'raw DP identity differs from selected attach')
        tlvs(p[10:])
        count = (self.c.frame - 11) // 4 + 1
        bad = struct.pack('<HH', connection, count) + bytes([2]) * count
        self.reject_unchanged(lambda: self.call(target, 'transfer', bad), 'unsupported', 'ARM fixed reply overflow')
        self.finish()
        self.start()
        self.reject_unchanged(lambda: self.call(target, 'transfer', struct.pack('<HHB', connection, 1, 2)),
                              'no_resource', 'ARM old connection after session end')
        self.finish()

    def run(self):
        try:
            self.prepare()
        except Exception as exc:
            self.results.append({'id': 'OEP-SMOKE-CONTEXT', 'level': 'oep-interface', 'status': 'failed', 'error': str(exc)})
        else:
            self.results.append({'id': 'OEP-SMOKE-CONTEXT', 'level': 'oep-interface', 'status': 'passed'})
            for label, required, method in (
                ('GPIO', ('oep.probe.plan', 'oep.fixture.gpio'), self.gpio),
                ('UART', ('oep.probe.plan', 'oep.fixture.uart'), self.uart),
                ('ARM', ('oep.wire.swd', 'oep.target.arm-adi'), self.arm)):
                row = {'id': 'OEP-SMOKE-' + label, 'level': 'oep-interface'}
                if any(name not in self.functions for name in required):
                    row.update(status='not_applicable', reason='selected example interfaces absent')
                elif label == 'UART' and self.feed is None:
                    row.update(status='not_run', reason='explicit UART stimulus not supplied')
                else:
                    try:
                        method(); row['status'] = 'passed'
                    except Exception as exc:
                        row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
                self.results.append(row)
                if row['status'] == 'failed':
                    break
        statuses = {row['status'] for row in self.results}
        return {'status': 'failed' if 'failed' in statuses else 'incomplete' if 'not_run' in statuses else 'passed',
            'full_conformance': False, 'scope': 'selected GPIO/UART/raw ARM scenario samples with explicit pins/stimulus/observations',
            'spec': self.c.spec, 'checks': self.results, 'observations': self.observations, 'exchanges': self.c.trace,
            'selected_pins': self.pins, 'selected_functions': getattr(self, 'functions', {}), 'core_observed': self.c.observed,
            'unfinished_session': self.c.session,
            'checker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'core_checker_sha256': hashlib.sha256(Path(__file__).with_name('conformance_core_smoke.py').read_bytes()).hexdigest(),
            'helper_sha256': hashlib.sha256(Path(__file__).with_name('conformance.py').read_bytes()).hexdigest(),
            'unchecked': ['full standard operation sets and optional components', 'all core/resource dependencies and failures',
                          'electrical behavior and physical timing', 'complete interface conformance']}
