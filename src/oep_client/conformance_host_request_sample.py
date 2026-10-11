"""Explicit Host TLV-tail encoder example; callers supply the fixed payload."""
import struct


def encode(interface, operation, fixed, fields):
    if interface.startswith('oep.wire.') and operation in ('scan', 'attach'):
        critical = {1, 2, 3, 4, 5, 6}
    elif interface == 'oep.fixture.logic' and operation in ('configure', 'query'):
        critical = {96}
    elif interface == 'oep.probe.plan' and operation == 'plan_apply':
        critical = {16}
    elif (interface, operation) in (('oep.fixture.gpio', 'set'), ('oep.fixture.uart', 'configure')):
        critical = {1}
    elif interface == 'oep.probe.config' and operation == 'set':
        critical = {1, 2, 3, 4, 6, 7, 8}
    elif interface in ('core', 'oep.fixture.analog', 'oep.fixture.capture-group'):
        critical = set()
    else:
        raise ValueError('unadopted request context')
    tail = bytearray()
    for tag, value in fields:
        if not 1 <= tag <= 127 or len(value) > 65535:
            raise ValueError('TLV ID/length')
        tail += struct.pack('<BH', tag | (128 if tag in critical else 0), len(value))
        tail += value
    return fixed + tail
