"""Core TLV request rules against an explicitly adopted four-context fixture."""
from .conformance import require, tlvs
from .conformance_registered_ops import fixed_then_tlvs


def field(tag, value):
    return bytes((tag,)) + len(value).to_bytes(2, 'little') + value


class TlvRequestChecks:
    contexts = ((1, 16, b'\x07'), (1, 17, b'\x07\x12'),
                (2, 16, b'\x02ab'), (2, 17, b'\x01'))

    def __init__(self, checks, state):
        self.c, self.state = checks, state
        self.observations = []

    def request(self, sid, context, raw):
        fn, op, _ = context
        return self.c.request(op, raw, session=sid, fn=fn)

    def accepted(self, sid, context, raw, value, repeats=()):
        before = self.state()
        request = self.request(sid, context, raw)
        payload = self.c.success(request)
        expected = len(value).to_bytes(2, 'little') + value + bytes((len(repeats),)) + bytes(repeats)
        require(fixed_then_tlvs(payload, expected), 'context-specific TLV value/order differs')   # a TLV tail may follow (core §2.3)
        after = self.state()
        self.observations.append({'action': 'accepted', 'request_hex': request.hex(),
                                  'before': before, 'after': after})
        entries = {(fn, op): (value, repeat) for fn, op, value, repeat in before['values']}
        entries[context[:2]] = (value.hex(), list(repeats))
        actual = {(fn, op): (item, repeat) for fn, op, item, repeat in after['values']}
        require(after['calls'] == before['calls'] + 1 and actual == entries,
                'accepted request changed a different fn/op context or execution count')

    def refused(self, sid, context, raw, reason, tag=None):
        before = self.state()
        request = self.request(sid, context, raw)
        payload = self.c.rejected(request, reason)
        after = self.state()
        self.observations.append({'action': 'refused', 'request_hex': request.hex(),
                                  'before': before, 'after': after})
        require(after == before, 'TLV rejection changed execution state')
        if tag is not None:
            require(payload and payload[0] == tag, 'unsupported tag not echoed as received')
            tlvs(payload[1:])

    def context(self):
        self.c.confirm();self.c.identity()
        entries = self.c.list_snapshot()
        require({row['fn'] for row in entries} == {1, 2} and
                all(row['name'] == 'io.github.open-embedded-probe.tlv-request' for row in entries),
                'explicit TLV request fixture declaration required')
        require(callable(self.state), 'explicit execution-state observation required')

    def isolation(self):
        with self.c.holding(60000) as sid:
            for _ in range(2):
                for context in self.contexts:
                    self.accepted(sid, context, field(1, context[2]), context[2])
            for context in self.contexts:
                fn, op, value = context
                # Same tag has a different shape in another op of fn1.
                if fn == 1:
                    wrong = b'x' if op == 17 else b'xy'
                    self.refused(sid, context, field(1, wrong), 'malformed')

    def critical(self):
        with self.c.holding(60000) as sid:
            for context in self.contexts:
                for tag in (1, 129):
                    self.accepted(sid, context, field(tag, context[2]), context[2])
                self.accepted(sid, context, field(127, b'\x07ignored') + field(1, context[2]), context[2])
                self.refused(sid, context, field(255, b'\x07ignored'), 'unsupported', 255)

    def malformed(self):
        with self.c.holding(60000) as sid:
            for context in self.contexts:
                for tag in (1, 129):
                    for value in (b'', context[2] + b'x'):
                        self.refused(sid, context, field(tag, value), 'malformed')
                for raw in (b'\x7f', b'\x7f\x00', b'\x7f\x02\x00x',
                            b'\xff\x02\x00x', field(0, b''), field(128, b'')):
                    self.refused(sid, context, raw, 'malformed')
            counted = self.contexts[2]
            for value in (b'\x00x', b'\x02x'):
                self.refused(sid, counted, field(1, value), 'malformed')

    def enums(self):
        with self.c.holding(60000) as sid:
            context = self.contexts[3]
            for tag in (1, 129):
                self.refused(sid, context, field(tag, b'\x00'), 'malformed')
                for value in (2, 3, 255):
                    self.refused(sid, context, field(tag, bytes((value,))), 'unsupported', tag)
                self.accepted(sid, context, field(tag, b'\x01'), b'\x01')

    def duplicates(self):
        with self.c.holding(60000) as sid:
            for context in self.contexts[:3]:
                _, _, value = context
                other = bytes((2,)) + b'xy' if context[:2] == (2, 16) else b'z' * len(value)
                for first, second in ((1, 129), (129, 1)):
                    self.accepted(sid, context, field(first, value) + field(second, other), value)
            for context in self.contexts:
                self.accepted(sid, context, field(3, b'\x09') + field(1, context[2]) + field(131, b'\x02'),
                              context[2], (9, 2))
            # core §2.3: a later copy of a non-repeating tag is checked too - a wrong length after a good first copy
            # is malformed, an unhandled value unsupported with that copy's tag as received
            for context in self.contexts:
                for first, second in ((1, 129), (129, 1)):
                    self.refused(sid, context, field(first, context[2]) + field(second, b''), 'malformed')
            for first, second in ((1, 129), (129, 1)):
                self.refused(sid, self.contexts[3], field(first, b'\x01') + field(second, b'\x02'), 'unsupported', second)

    def run(self):
        start = len(self.c.results)
        for name in ('context', 'isolation', 'critical', 'malformed', 'enums', 'duplicates'):
            self.c.check('CORE-TLV-REQUEST-' + name.upper(), 'core §2.2/2.3', getattr(self, name))
            if name == 'context' and self.c.results[-1]['status'] != 'passed': self.c.abort = True
        checks = self.c.results[start:]
        return {'status': 'passed' if all(r['status'] == 'passed' for r in checks) else 'failed',
                'full_conformance': False, 'subject': 'explicit four fn/op TLV contexts',
                'checks': checks, 'state_observations': self.observations, 'unchecked': ['other adopted request schemas', 'concurrent requests',
                                               'all core/common contracts', 'physical implementation'],
                'adoption': 'docs/tlv-request-conformance.ja.md'}
