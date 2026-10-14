"""Independent ARM-ADI transfer checks and basic logical bus-block checks (debug §6)."""
import struct
from .conformance import require, ops, tlvs
from .conformance_attach import AttachChecks


class ArmAdiChecks(AttachChecks):
    @staticmethod
    def read(req): return bytes((req | 2,))
    @staticmethod
    def write(req, value): return bytes((req & ~2,)) + struct.pack('<I', value)
    def transfer_request(self, sid, parent, steps, extra=b''):
        return self.request(5, 'transfer', sid, struct.pack('<IH', parent, len(steps)) + b''.join(steps) + extra)
    def transfer(self, sid, parent, steps, done, status, ack, values):
        request = self.transfer_request(sid, parent, steps)
        frame = self.c.exchange(request)
        require(len(frame) >= 11 and frame[3:5] == bytes((1, 0 if status == 0 else (2 if done else 1))), 'ARM completed outcome')
        require(struct.unpack_from('<HBBH', frame, 5) == (done, status, ack, len(values)), 'ARM done/status/raw ACK/nvals')
        end = 11 + 4 * len(values)
        require(len(frame) >= end, 'ARM values truncated')
        require([struct.unpack_from('<I', frame, 11 + 4 * i)[0] for i in range(len(values))] == values, 'ARM read values/order/posted read')
        tlvs(frame[end:])
        return request, frame
    def parent(self, sid): return self.attach(sid, fn=3)[0]
    def simple(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent = self.parent(sid)
            if kind == 'EMPTY':
                before = self.a.state(); self.transfer(sid, parent, [], 0, 0, 0, [])
                require(self.a.state() == before, 'empty transfer touched target')
            elif kind == 'MIXED':
                self.a.seed(parent, {4:0x20000000}, {}, 0)
                steps = [self.write(1, 0x12345678), self.read(4), self.write(8, 0xabcdef01), self.read(4)]
                self.transfer(sid, parent, steps, 4, 0, 1, [0x20000000, 0x20000000])
                require(self.a.state()['targets'][parent]['dp'][8] == 0xabcdef01, 'raw host DP write was restored')
            elif kind == 'POSTED':
                self.a.seed(parent, {4: 0x55}, {0: 0x11223344, 4: 0xaabbccdd}, 0xfeedface)
                self.transfer(sid, parent, [self.read(1), self.read(4), self.read(5), self.read(12)], 4, 0, 1, [0xfeedface, 0x55, 0x11223344, 0xaabbccdd])
            elif kind == 'WAIT-RECOVER':
                self.a.acks([2, 2, 1]); before = self.a.state()
                self.transfer(sid, parent, [self.write(8, 0x1234)], 1, 0, 1, [])
                after = self.a.state()
                require(len(after['packets']) - len(before['packets']) == 3 and len(after['executed']) - len(before['executed']) == 1, 'WAIT not retried or write executed repeatedly')
            elif kind == 'REPLAY':
                req, frame = self.transfer(sid, parent, [self.write(8, 0x1234), self.read(4)], 2, 0, 1, [0])
                self.a.acks([4]); before = self.a.state()
                require(self.c.exchange(req) == frame and self.a.state() == before, 'ARM replay reexecuted or recomputed target')
            elif kind == 'FRAME':
                count = (self.c.max_frame - 11) // 4
                self.transfer(sid, parent, [self.read(0)] * count, count, 0, 1, [0] * count)
                self.refused(self.transfer_request(sid, parent, [self.read(0)] * (count + 1)), 'unsupported', b'\0')
    def failure(self, ack, index):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent = self.parent(sid)
            self.a.seed(parent, {4:0x20000000}, {}, 0)
            steps = [self.write(1, 0x55), self.read(4), self.write(8, 0xaa), self.read(4)]
            self.a.acks([1] * index + ([2] * 3 if ack == 2 else [ack]))
            before = self.a.state()
            self.transfer(sid, parent, steps, index, {0:2, 2:1, 4:3}[ack], ack, [0x20000000] if index >= 2 else [])
            after = self.a.state()
            require(len(after['executed']) - len(before['executed']) == index, 'ARM executed failing transfer or suffix')
            require(len(after['packets']) - len(before['packets']) == index + (3 if ack == 2 else 1), 'ARM retried non-WAIT or skipped WAIT retries')
    def rejection(self, kind):
        self.fresh()
        if kind == 'SESSION': self.refused(self.transfer_request(0, 0, [b'\xff']), 'session_required'); return
        with self.c.holding(3000) as sid:
            parent = self.parent(sid)
            if kind == 'RESERVED':
                for bit in (16, 32, 64, 128): self.refused(self.transfer_request(sid, parent, [self.write(8, 99), bytes((bit | 2,))]), 'unsupported', b'\0')
            elif kind == 'TRUNCATED': self.refused(self.transfer_request(sid, parent, [self.write(8, 99), b'\x04x']), 'malformed')
            elif kind == 'COUNT': self.refused(self.request(5, 'transfer', sid, struct.pack('<IH', parent, 2) + b'\x02'), 'malformed')
            elif kind == 'CRITICAL': self.refused(self.transfer_request(sid, parent, [self.write(8, 99)], self.tlv(254, b'x')), 'unsupported', b'\xfe')
            elif kind == 'TLV': self.refused(self.transfer_request(sid, parent, [self.write(8, 99)], b'\x7f\x05\0x'), 'malformed')
            elif kind == 'FOREIGN': self.refused(self.transfer_request(sid % 0xffffffff + 1, parent, [b'\xff']), 'locked')
            elif kind == 'RESOURCE':
                self.refused(self.transfer_request(sid, 65535, []), 'no_resource')
                wrong, _ = self.attach(sid)
                self.refused(self.transfer_request(sid, wrong, []), 'unavailable', cause=6)
                stream, _ = self.console(sid, wrong)
                self.refused(self.transfer_request(sid, stream, []), 'unavailable', cause=6)
    def blocks(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent = self.parent(sid); base = 0x2000
            def request(name, count, values=(), address=base):
                return self.request(5, name, sid, struct.pack('<IIH', parent, address, count) + b''.join(struct.pack('<I', v) for v in values))
            def read(values):
                body = self.c.success(request('read_block', len(values)))
                require(len(body) >= 3 + len(values) * 4 and struct.unpack_from('<HB', body) == (len(values), 0), 'ARM block done/status')
                require([struct.unpack_from('<I', body, 3 + 4*i)[0] for i in range(len(values))] == values, 'ARM block stale/not bus data')
                tlvs(body[3 + 4*len(values):])
            if kind == 'LIVE':
                self.a.seed(parent, {8:0x12000000}, {0:0x23000052}, 0)
                self.a.memory(parent, {base:1, base+4:2}); read([1, 2])
                self.a.memory(parent, {base:0xfeed1234}); read([0xfeed1234, 2])
                body = self.c.success(request('write_block', 2, [0x12345678, 0xabcdef01]))
                require(body[:3] == b'\x02\0\0', 'ARM block write result'); tlvs(body[3:]); read([0x12345678, 0xabcdef01])
                target = self.a.state()['targets'][parent]
                require(target['dp'][8] == 0x12000000 and target['ap'][0] == 0x23000052 and target['ap'][4] == base+8, 'ARM SELECT/CSW restored/changed or TAR not advanced')
            elif kind == 'EMPTY':
                before = self.a.state(); read([])
                require(self.c.success(request('write_block', 0))[:3] == b'\0\0\0' and self.a.state() == before, 'empty ARM block touched bus')
            elif kind == 'LIMIT':
                self.a.memory(parent, {base+4*i:i for i in range(4)}); read([0, 1, 2, 3])
                for name in ('read_block', 'write_block'): self.refused(request(name, 5, [0]*5 if name == 'write_block' else ()), 'unsupported', b'\0')
            elif kind == 'ALIGNMENT':
                for name in ('read_block', 'write_block'): self.refused(request(name, 1, [0] if name == 'write_block' else (), base+1), 'malformed')
    def run(self):
        start = len(self.c.results)
        def context():
            require(self.a.reset_scope == 'logical-model' and self.a.reference and self.a.wait_attempts == 3, 'explicit ARM logical fixture required')
            self.specs = {fn: next(s for s in self.c.reg['interface'] if s['name'] == name and s['revision'] == 1) for fn, name in self.a.selection.items()}
            self.operations = {fn: {row['name']:row['code'] for row in spec['op']} for fn, spec in self.specs.items()}
            require(self.operations[5] == dict(transfer=16, read_block=17, write_block=18) and self.specs[5]['enum']['swd_ack'] == dict(ok=1, wait=2, fault=4), 'selected ARM grammar differs')
            require(self.c.reg['status'] == dict(ok=0, wait=1, line=2, fault=3, timeout=4, state=5), 'selected status grammar differs')
            self.c.confirm(); self.c.identity(); self.c.declarations()
            require(any(row['fn'] == 5 and row['name'] == 'oep.target.arm-adi' and row['revision'] == 1 for row in self.c.list_snapshot()), 'ARM fn/revision mismatch')
            rows = dict(reversed(self.c.describe(5)))
            require(ops(rows[7]) == {16,17,18} and 5 not in rows and rows[3] == struct.pack('<H', 16), 'ARM ops/features/max_length fixture declaration')
        self.c.check('OEP-ARM-CONTEXT', 'debug §6; core §7', context)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        cases = [(kind, lambda kind=kind:self.simple(kind)) for kind in ('EMPTY','MIXED','POSTED','WAIT-RECOVER','REPLAY','FRAME')]
        cases += [('ACK-'+str(ack)+'-'+str(index), lambda ack=ack,index=index:self.failure(ack,index)) for ack in (0,2,4) for index in range(4)]
        cases += [(kind, lambda kind=kind:self.rejection(kind)) for kind in ('RESERVED','TRUNCATED','COUNT','CRITICAL','TLV','SESSION','FOREIGN','RESOURCE')]
        cases += [('BLOCK-'+kind, lambda kind=kind:self.blocks(kind)) for kind in ('LIVE','EMPTY','LIMIT','ALIGNMENT')]
        for name, case in cases:
            first = len(self.a.trace); self.c.check('OEP-ARM-'+name, 'debug §2/6; core §2.3/4/5/9', case); self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
        rows = self.c.results[start:]
        return dict(status='passed' if all(row['status']=='passed' for row in rows) else 'failed', checks=rows, adapter_trace=self.a.trace, observed=self.c.observed,
                    full_conformance=False, scope='logical ARM transfer ACK/progress/posted reads and basic bus-block invariants',
                    unchecked=['SWD attach TARGETSEL/tid metadata, electrical packets, parity, idle and physical retry timing','ADIv6, AP bank selection and sticky ABORT behavior','MEM-AP posted block packet ordering, TAR wrap boundary and partial block failures','all routes and capacities, u32 address overflow policy','production Host integration and complete core/common normative audit'])
