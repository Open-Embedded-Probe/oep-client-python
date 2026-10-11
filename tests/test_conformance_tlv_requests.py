import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_tlv_requests import TlvRequestChecks
from oep_client.virtual_tlv_request_model import TlvRequestModel
from oep_client.virtual_core import Reject
from test_conformance import REG


def build():
    model = TlvRequestModel(lambda: 0, boot_id=17)
    c = Checks(model.handle, copy.deepcopy(REG), 'virtual-resource-1')
    return model, c, TlvRequestChecks(c, model.request_state)


def test_tlv_request_contracts_pass():
    _, _, checker = build()
    report = checker.run()
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 6 and not report['full_conformance']


@pytest.mark.parametrize('fault,case', [
    ('shared_width', 'ISOLATION'), ('shared_state', 'ISOLATION'), ('critical_unknown', 'CRITICAL'),
    ('optional_rejected', 'CRITICAL'), ('strip_echo', 'CRITICAL'),
    ('repair_width', 'MALFORMED'), ('rejection_mutates', 'MALFORMED'),
    ('unsupported_as_malformed', 'ENUMS'), ('excluded_as_unsupported', 'ENUMS'),
    ('last_duplicate', 'DUPLICATES'), ('sort_repeated', 'DUPLICATES'),
])
def test_injected_request_faults(fault, case):
    model, _, checker = build()
    extension = model.extension
    parse, dispatch = extension.request_tags, extension.dispatch_request
    def altered(fn, op, raw):
        if fault == 'shared_width': fn, op = 1, 16
        if fault == 'critical_unknown' and raw.startswith(b'\x81'):
            raise Reject(11, b'\x81')
        if fault == 'optional_rejected' and raw.startswith(b'\x7f'):
            raise Reject(11, b'\x7f')
        if fault == 'last_duplicate':
            rows = []
            while raw:
                size = int.from_bytes(raw[1:3], 'little')
                rows.append(raw[:3 + size]);raw = raw[3 + size:]
            raw = b''.join(reversed(rows))
        try:
            tags = parse(fn, op, raw)
        except Reject as exc:
            if fault == 'rejection_mutates': extension.calls += 1
            if fault == 'repair_width' and exc.reason == 3:
                return {1: b'x', 3: []}
            if fault == 'strip_echo' and exc.reason == 11: raise Reject(11, bytes((exc.payload[0] & 127,)))
            if fault == 'unsupported_as_malformed' and exc.reason == 11: raise Reject(3)
            if fault == 'excluded_as_unsupported' and exc.reason == 3: raise Reject(11, b'\x01')
            raise
        if fault == 'sort_repeated': tags[3].sort()
        return tags
    if fault == 'shared_state':
        def shared(fn, op, fixed, tags):
            result = dispatch(fn, op, fixed, tags)
            extension.values = {(1, 16): extension.values[fn, op]}
            return result
        extension.dispatch_request = shared
    extension.request_tags = altered
    report = checker.run()
    row = next(r for r in report['checks'] if r['id'] == 'CORE-TLV-REQUEST-' + case)
    assert row['status'] == 'failed' and row['error'].startswith('Violation:'), row


def test_counted_value_capacity_rejects_without_execution():
    from oep_client.conformance_tlv_requests import field
    _, c, checker = build()
    checker.context()
    with c.holding(60000) as sid:
        value = bytes((48,)) + b'x' * 48
        checker.accepted(sid, checker.contexts[2], field(1, value), value)
        for tag in (1, 129):
            checker.refused(sid, checker.contexts[2], field(tag, bytes((49,)) + b'x' * 49),
                            'unsupported', tag)


@pytest.mark.parametrize('invalid', ['name', 'functions'])
def test_unselected_fixture_blocks_before_extension_requests(invalid):
    model, c, checker = build()
    if invalid == 'name': model.extension.name = b'io.github.example.other'
    else: model.extension.functions = (1, 3)
    report = checker.run()
    assert report['checks'][0]['status'] == 'failed'
    assert all(row['status'] == 'blocked' for row in report['checks'][1:])
    assert all(bytes.fromhex(row['request_hex'])[3:5] == b'\x00\x00' for row in c.trace)
