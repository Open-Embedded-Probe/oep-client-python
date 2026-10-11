"""Host critical-bit policy with independently reviewed OEP request contexts.

This checks serialization of caller-selected fields, not selection, execution,
transport or a production Host. Other extensions supply their own policy review.
"""
import hashlib
from pathlib import Path

from .conformance import require, spec_identity


REVIEWED = {
    'interfaces/oep-if-plan.ja.md': '1b785a2c41934d6f1e29e663ca61a1a62049da5c4b094eb345f9a9d981ccbe1a',
    'interfaces/oep-if-fixture.ja.md': 'f9bf2afb9ed278fb5d4ab8e63f61bc4bdf15f4db499c40caec4f9ff41eb09ee6',
    'interfaces/oep-if-probe-config.ja.md': '7ecb886254ad5451b9a5c7cad4bffd8327bb0c146436266d90aadb9cfafc61f8',
    'docs/oep-core.ja.md': 'cddf917d49f36bbb1a5b24fbda247675834e1c398beaa179728b8efa31831bf9',
    'interfaces/oep-if-debug.ja.md': '2540689121fa46cfcb68cad08bef0a71acd7e8c0a802330b05bd97057f11861e',
    'interfaces/oep-if-capture.ja.md': 'bc63cae4b7d717f2228c0412534b1c409eedb5a20547cefd02dd99f0893016ec',
}
REVIEWED_REGISTRY_SHA256='cbe25053be20cb5c36942d632fb2443c245b4c94aae526716dd46b2bb6c9c69e'


def reviewed_identity(spec):
    registry, identity = spec_identity(spec)
    require(identity['registry_sha256']==REVIEWED_REGISTRY_SHA256,
            'request registry changed: review before reusing cases')
    for name, expected in REVIEWED.items():
        require(hashlib.sha256((Path(spec) / name).read_bytes()).hexdigest() == expected,
                'request policy text changed: review before reusing cases')
    require(registry['protocol']['revision']==1 and registry['protocol']['byte_order']=='little',
            'request policy protocol changed')
    interfaces={row['name']:row for row in registry['interface']}
    for name,op,fixed,fields in contexts():
        entry=registry['core'] if name=='core' else interfaces.get(name,{})
        require(name=='core' or entry.get('revision')==1,'request interface revision changed')
        expected_op=17 if name=='oep.probe.config' and op=='set' else 16 if op in ('scan','configure','bind','open','plan_apply','set') else 17 if op=='attach' else 24
        require(next((r['code'] for r in entry['op'] if r['name']==op),None)==expected_op,
                'request operation number changed')
        tags=entry['tlv']['item' if name=='oep.probe.config' and op=='set' else 'configure' if op=='query' else op]
        require(set(tags.values())=={t for t,v,c in fields},'request tag space changed')
    return dict(identity, reviewed_request_policy_sha256=REVIEWED.copy())


def contexts():
    # Each tuple: interface, op, fixed payload, (tag, value, critical) entries.
    # These expectations are independent of the sample encoder's policy table.
    rows = [('core', 'open', b'\xb8\x0b\x00\x00\x00', [(1, b'test-owner', False)])]
    for wire in ('rvswd', 'swio', 'swd'):
        scan = [(1, b'\x40\x42\x0f\x00', True)]
        attach = [(1, b'\x40\x42\x0f\x00', True),
                  (3, b'\x00\x00\xff\xff' if wire == 'swio' else b'\x00\x00\x01\x00', True),
                  (5, b'\x02\x00\x0a\x00', True)]
        if wire == 'rvswd':
            scan.append((4, b'\x01', True)); attach.append((4, b'\x01', True))
        if wire == 'swd':
            scan.append((6, b'\x34\x12\x00\x01', True))
            attach.append((2, b'\x34\x12\x00\x01', True))
        rows.extend([('oep.wire.' + wire, 'scan', b'\x01\x00\x00\xff\xff' if wire=='swio' else b'\x01\x00\x00\x01\x00', scan),
                     ('oep.wire.' + wire, 'attach', b'\x00', attach)])
    for name in ('logic', 'analog'):
        tags = [(64, b'\x02', False), (66, b'\xe8\x03\x00\x00', False),
                (67, b'\x10\x00\x00\x00', False), (68, b'\x02\x00\x00\x00', False),
                (69, b'\x02\x00\x00\x00\x00\x00' if name == 'logic' else b'\x03\x01\xb8\x0b\x00\x00', False),
                (70, b'\x02\x00\x00\x00', False)]
        if name == 'analog':
            tags.extend([(71, b'\x01\x00', False), (71, b'\x03\x01', False)])
        else:
            tags.extend([(96, b'\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00', True),
                         (96, b'\x01\x00\x04\x00\x00\x00\x00\x00\x00\x00', True)])
        for op in ('configure', 'query'):
            rows.append(('oep.fixture.' + name, op, b'', list(tags)))
    rows.append(('oep.fixture.capture-group', 'bind', b'\x02\x02\x00\x03\x00', [(1, b'\x02\x00', False)]))
    # Requested assignments and output/format settings cannot be silently ignored.
    rows.append(('oep.probe.plan','plan_apply',b'',[
        (16,b'\x02\x00\x01\x01\x00',True),(16,b'\x02\x00\x01\x03\x00',True)]))
    rows.append(('oep.fixture.gpio','set',b'\x02\x01\x00\x04\x02\x00\x03',[
        (1,b'\x00\x00',True),(1,b'\x01\xff',True)]))
    rows.append(('oep.fixture.uart','configure',b'\x00\xc2\x01\x00',[(1,b'\x05',True)]))
    # All supplied set items are intentional setting changes, including presets
    # and line labels consumed by Host wiring discovery. Distinct repeat keys.
    rows.append(('oep.probe.config','set',b'',[
        (1,b'\x02\x00\x01\x01\x00',True),(1,b'\x02\x00\x01\x03\x00',True),
        (2,b'\x04\x00nrst',True),(3,b'\x01\x00\x04\x00',True),
        (4,b'\x00\x01\x00\x02\x00\x03\x00\x40\x42\x0f\x00\x00\xff\x08socket-a',True),
        (6,b'\x06\x00\x00\xc2\x01\x00\x05',True),(7,b'\x07\x00',True),
        (8,b'\x00\x03lab\x08testpass',True),(8,b'\x01\x04test\x00',True)]))
    return rows


class HostRequestChecks:
    def __init__(self, encoder, *, spec):
        self.encoder, self.identity = encoder, reviewed_identity(spec)
        self.observations = []

    def expect(self, context, entries):
        interface, op, fixed, _ = context
        fields = [(tag, value) for tag, value, critical in entries]
        # No decoder or sample-generated golden bytes participate in this oracle.
        expected = fixed + b''.join(bytes((tag | (128 if critical else 0),)) +
                    len(value).to_bytes(2, 'little') + value for tag, value, critical in entries)
        actual = self.encoder(interface, op, fixed, fields)
        self.observations.append(dict(interface=interface, operation=op,
            fixed_hex=fixed.hex(), fields=[dict(tag=t, value_hex=v.hex(), critical=c) for t,v,c in entries],
            expected_hex=expected.hex(), actual_hex=actual.hex() if isinstance(actual, bytes) else None))
        require(isinstance(actual, bytes) and actual == expected,
                'request critical policy, supplied fixed bytes or field order/value changed')

    def critical(self):
        for context in contexts():
            if not any(c for t,v,c in context[3]): continue
            self.expect(context, context[3])
            # Remove each optional/required field only at the tail-component level.
            # This does not claim the resulting complete request is valid.
            for index in range(len(context[3])):
                self.expect(context, [row for i,row in enumerate(context[3]) if i != index])

    def noncritical(self):
        for context in contexts():
            if any(not c for t,v,c in context[3]): self.expect(context, context[3])

    def order(self):
        for context in contexts():
            self.expect(context, list(reversed(context[3])))
            self.expect(context, [])
        # Alternate same tag1 with opposite policies; alternate same tag number's widths.
        for _ in range(3):
            for context in contexts(): self.expect(context, context[3])

    def values(self):
        for context in contexts():
            for tag,value,critical in context[3]:
                # Opaque value-copy stress, not interface semantic acceptance.
                for blob in (b'', bytes(range(256)), b'x' * 65535):
                    self.expect(context, [(tag, blob, critical)])

    def invalid(self):
        for context in contexts():
            interface, op, fixed, _ = context
            for tag,value in ((0,b''),(128,b''),(255,b''),(1,b'x'*65536)):
                record=dict(interface=interface,operation=op,tag=tag,length=len(value))
                self.observations.append(record)
                try: self.encoder(interface, op, fixed, [(tag,value)])
                except ValueError:
                    record['unusable']='ValueError';continue
                require(False, 'invalid lower-seven-bit ID or unrepresentable TLV length encoded')

    def run(self):
        checks = []
        for name in ('critical', 'noncritical', 'order', 'values', 'invalid'):
            start=len(self.observations)
            row=dict(id='HOST-REQUEST-'+name.upper(),clause='core §2.2/2.3; adopted interface request policies',status='passed')
            try: getattr(self,name)()
            except Exception as exc: row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
            row['observations']=self.observations[start:];checks.append(row)
        return dict(status='passed' if all(r['status']=='passed' for r in checks) else 'failed',
            full_conformance=False,level='core',subject='Host TLV-tail critical-policy component with OEP adoption',
            spec=self.identity,checks=checks,contexts=len(contexts()),
            encoder=dict(module=self.encoder.__module__,name=self.encoder.__qualname__),
            sources_sha256={n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest()
                for n in ('conformance_host_requests.py','conformance_host_request_sample.py')},
            unchecked=['production Host encoders','field selection/defaults/mandatory arguments',
                'Probe execution and refusal nonmutation','all request schemas and other extensions',
                'fixed payload construction/transport bounds','external encoder provenance'],
            adoption='docs/host-request-conformance.ja.md')
