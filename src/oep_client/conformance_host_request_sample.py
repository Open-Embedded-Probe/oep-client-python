"""Explicit Host TLV-tail encoder example; callers supply the fixed payload."""
import struct


def encode(interface, operation, fixed, fields):
    if interface.startswith('oep.wire.') and operation == 'scan':
        critical = {1, 4, 6}
    elif interface.startswith('oep.wire.') and operation == 'attach':
        critical = {2, 4, 5}
    elif interface == 'oep.probe.plan' and operation == 'plan_apply':
        critical = {16}
    elif (interface, operation) == ('oep.fixture.uart', 'configure'):
        critical = {1}
    elif (interface, operation) == ('oep.probe.config', 'set'):
        critical = set()  # setting-data items: bit 7 is always 0 (config §1)
    elif interface == 'core' and operation == 'open' or interface in ('oep.fixture.logic', 'oep.fixture.analog') \
            and operation in ('configure', 'query'):
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
