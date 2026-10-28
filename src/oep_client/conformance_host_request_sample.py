"""Explicit Host TLV-tail encoder example; callers supply the fixed payload."""
import struct

from . import registry

# The request TLVs this example sends critical, by name (core §2.3: ones that must not be ignored; capture §3.3 / §3.8)
_CAPTURE_CRITICAL = {'format', 'format_name', 'data_path', 'carriage', 'trigger_opts', 'threshold', 'clock',
                     'glitch_filter', 'coupling', 'offset', 'bandwidth', 'resolution', 'oversample'}
_POLICY = {
    'scan': {'max_speed', 'idle_clock', 'targetsel'},
    'attach': {'reset', 'idle_clock', 'targetsel'},
}


def _critical(interface, operation):
    """The critical tag numbers of the context, from the generated registry."""
    if interface.startswith('oep.wire.') and operation in _POLICY:
        names, tlv = _POLICY[operation], registry.INTERFACES[interface].tlv[operation]
    elif (interface, operation) == ('oep.probe.plan', 'plan_apply'):
        names, tlv = {'role_assignment'}, registry.PROBE_PLAN.tlv['plan_apply']
    elif (interface, operation) == ('oep.fixture.uart', 'configure'):
        names, tlv = {'format'}, registry.FIXTURE_UART.tlv['configure']
    elif (interface, operation) == ('core', 'open'):
        names, tlv = set(), registry.CORE.tlv['open']
    elif interface in ('oep.fixture.logic', 'oep.fixture.analog') and operation in ('configure', 'query'):
        names, tlv = _CAPTURE_CRITICAL, registry.INTERFACES[interface].tlv['configure']
    else:
        raise ValueError('unadopted request context')
    return {number for name, number in tlv.items() if name in names}


def encode(interface, operation, fixed, fields):
    critical = _critical(interface, operation)
    tail = bytearray()
    for tag, value in fields:
        if not 1 <= tag <= 127 or len(value) > 65535:
            raise ValueError('TLV ID/length')
        tail += struct.pack('<BH', tag | (registry.TAG_CRITICAL if tag in critical else 0), len(value))
        tail += value
    return fixed + tail
