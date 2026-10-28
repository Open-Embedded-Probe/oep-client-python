"""Host critical-bit policy with independently reviewed OEP request contexts.

This checks serialization of caller-selected fields, not selection, execution,
transport or a production Host. Other extensions supply their own policy review.
"""
import hashlib
from pathlib import Path

from .conformance import require, spec_identity


REVIEWED = {
    'interfaces/oep-if-plan.ja.md': 'da8bd60af53499573c07bc33dbd8c4df93eb61a93b91dc57907f26620ae36bbf',
    'interfaces/oep-if-fixture.ja.md': '780beb7a3f9adb51fccf3441930e4c04d2bb64ef738e302195cf5a4e7c2a60a6',
    'docs/oep-core.ja.md': 'd63f99f1809146ad41f1643526c81417b366d7a9d85af3845daee6654a229a14',
    'interfaces/oep-if-debug.ja.md': '6743420c31bcd354dd5b4b5352f4d8bcfa695f787dbc28644dba9e9533357338',
    'interfaces/oep-if-capture.ja.md': 'ffd89a6a1abf006aea788a6b47b270e6ac0e6d8190a716f4123c4f499bd3a61e',
}
REVIEWED_REGISTRY_SHA256='21057dbfc117365e0b9f9d96f377ed99dbc28568efc70829bf8d53384077f44e'


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
    for name,op,fixed,fields in policy():
        entry=registry['core'] if name=='core' else interfaces.get(name,{})
        require(name=='core' or entry.get('revision')==1,'request interface revision changed')
        expected_op=16 if op in ('scan','configure','open','plan_apply') else 17 if op=='attach' else 24
        require(next((r['code'] for r in entry['op'] if r['name']==op),None)==expected_op,
                'request operation number changed')
        # every request TLV of the context has a reviewed policy: a new one needs review, a renumbered one does not
        tags=entry['tlv']['configure' if op=='query' else op]
        require(set(tags)=={t for t,v,c in fields},'request tag space changed')
    return dict(identity, reviewed_request_policy_sha256=REVIEWED.copy(), registry=registry)


def _tlv(registry, interface, op):
    """The context's request TLV numbers by name: from the selected SPEC's registry, else the client's generated one."""
    ctx = 'configure' if op == 'query' else op
    if registry is None:
        from . import registry as generated
        return (generated.CORE if interface == 'core' else generated.INTERFACES[interface]).tlv[ctx]
    if interface == 'core':
        return registry['core']['tlv'][ctx]
    return next(row for row in registry['interface'] if row['name'] == interface)['tlv'][ctx]


def contexts(registry=None):
    """The reviewed policy with the TLVs numbered from a registry (names never hard-coded as numbers)."""
    return [(name, op, fixed, [(_tlv(registry, name, op)[tlv], value, critical) for tlv, value, critical in fields])
            for name, op, fixed, fields in policy()]


def policy():
    # Each tuple: interface, op, fixed payload, (TLV name, value, critical) entries.
    # These expectations are independent of the sample encoder's policy table.
    rows = [('core', 'open', b'\xb8\x0b\x00\x00\x00', [('owner', b'test-owner', False)])]
    for wire in ('rvswd', 'swio', 'swd'):
        # debug §1/§3/§5: the pins and max_speed of attach are fixed fields; scan carries candidates. core §2.3 names scan's
        # max_speed and attach's reset as TLVs that must not be ignored; idle_clock and targetsel change what is tried.
        pins = b'\x00\x00\xff\xff' if wire == 'swio' else b'\x00\x00\x01\x00'
        scan = [('max_speed', b'\x40\x42\x0f\x00', True)]
        attach = [('reset', b'\x02\x00\x0a\x00', True)]
        if wire == 'rvswd':
            scan.append(('idle_clock', b'\x01', True)); attach.append(('idle_clock', b'\x01', True))
        if wire == 'swd':
            scan.append(('targetsel', b'\x34\x12\x00\x01', True))
            attach.append(('targetsel', b'\x34\x12\x00\x01', True))
        rows.extend([('oep.wire.' + wire, 'scan', b'\x01' + pins, scan),
                     ('oep.wire.' + wire, 'attach', b'\x00' + pins + b'\x40\x42\x0f\x00', attach)])
    for name in ('logic', 'analog'):
        # capture §3.3: the table's TLVs are implemented by every target probe, so they go without the critical bit -
        # but for those the table says to send critical (format, format_name, data_path, carriage, trigger_opts);
        # §3.8: every front-end TLV is optional and critical.
        tags = [('mode', b'\x02', False), ('rate', b'\xe8\x03\x00\x00', False),
                ('samples', b'\x10\x00\x00\x00', False), ('segments', b'\x02\x00\x00\x00', False),
                ('trigger', b'\x02\x00\x00\x00\x00\x00' if name == 'logic' else b'\x03\x01\xb8\x0b\x00\x00', False),
                ('pretrigger', b'\x02\x00\x00\x00', False),
                ('format', b'\x01', True), ('carriage', b'\x00\x01\x01\x00\x00\x00', True),
                ('format_name', b'io.example.form', True), ('data_path', b'\x01\x00\x00', True),
                ('trigger_opts', b'\x01\x01\x00\x00\x00\x00\x00', True),
                ('block_max', b'\x00\x01\x00\x00', False), ('overflow', b'\x01', False)]
        if name == 'logic':
            tags.extend([('threshold', b'\x00\xdc\x05\x00\x00\x00\x00\x00\x00', True),
                         ('clock', b'\x00\x00\xff\x00', True), ('glitch_filter', b'\x00\x64\x00\x00\x00', True)])
        else:
            tags.extend([('frontend', b'\x01\x00', False), ('frontend', b'\x03\x01', False),
                         ('coupling', b'\x01\x00', True), ('offset', b'\x01\x00\x00\x00\x00', True),
                         ('bandwidth', b'\x01\x40\x42\x0f\x00', True), ('resolution', b'\x0c', True),
                         ('oversample', b'\x04\x00', True)])
        for op in ('configure', 'query'):
            rows.append(('oep.fixture.' + name, op, b'', list(tags)))
    # Requested assignments cannot be silently ignored.
    rows.append(('oep.probe.plan','plan_apply',b'',[
        ('role_assignment',b'\x02\x00\x01\x01\x00',True),('role_assignment',b'\x02\x00\x01\x03\x00',True)]))
    rows.append(('oep.fixture.uart','configure',b'\x00\xc2\x01\x00',[('format',b'\x05',True)]))
    return rows


class HostRequestChecks:
    def __init__(self, encoder, *, spec):
        self.encoder, self.identity = encoder, dict(reviewed_identity(spec))
        self.registry = self.identity.pop('registry', None)   # the selected SPEC's numbers (a test's stub: the client's)
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
        for context in contexts(self.registry):
            if not any(c for t,v,c in context[3]): continue
            self.expect(context, context[3])
            # Remove each optional/required field only at the tail-component level.
            # This does not claim the resulting complete request is valid.
            for index in range(len(context[3])):
                self.expect(context, [row for i,row in enumerate(context[3]) if i != index])

    def noncritical(self):
        for context in contexts(self.registry):
            if any(not c for t,v,c in context[3]): self.expect(context, context[3])

    def order(self):
        for context in contexts(self.registry):
            self.expect(context, list(reversed(context[3])))
            self.expect(context, [])
        # Alternate same tag1 with opposite policies; alternate same tag number's widths.
        for _ in range(3):
            for context in contexts(self.registry): self.expect(context, context[3])

    def values(self):
        for context in contexts(self.registry):
            for tag,value,critical in context[3]:
                # Opaque value-copy stress, not interface semantic acceptance.
                for blob in (b'', bytes(range(256)), b'x' * 65535):
                    self.expect(context, [(tag, blob, critical)])

    def invalid(self):
        for context in contexts(self.registry):
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
            spec=self.identity,checks=checks,contexts=len(contexts(self.registry)),
            encoder=dict(module=self.encoder.__module__,name=self.encoder.__qualname__),
            sources_sha256={n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest()
                for n in ('conformance_host_requests.py','conformance_host_request_sample.py')},
            unchecked=['production Host encoders','field selection/defaults/mandatory arguments',
                'settings value semantics (core §13: values, not request TLVs)',
                'Probe execution and refusal nonmutation','all request schemas and other extensions',
                'fixed payload construction/transport bounds','external encoder provenance'],
            adoption='docs/host-request-conformance.ja.md')
