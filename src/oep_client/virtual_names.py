"""Protocol numbers by name for the virtual models and checks (registry/oep-v1.toml through the generated registry):
reject reasons, unavailable causes and their payload, the common describe tags. A model or a check never writes an
OEP number as a literal - it names it here or in its interface's registry table (oep-spec core §2.5)."""
import struct

from . import registry as _reg

R = dict(_reg.REJECT_REASONS)                          # malformed, unsupported, unavailable, ...
CAUSE = dict(_reg.CORE.enum["unavailable_cause"])      # pin_in_use, limit, wrong_state, ...
DC = dict(_reg.DESCRIBE_COMMON)                         # ops, role_channels, features, ...
UNAVAILABLE_TLV = dict(_reg.CORE.tlv["unavailable_payload"])


def tlv(tag, value):
    return bytes((tag,)) + struct.pack('<H', len(value)) + value


def cause(name, channel=None):
    """The unavailable payload: TLV cause, and the channel when given (core §4.3)."""
    out = tlv(UNAVAILABLE_TLV["cause"], bytes((CAUSE[name],)))
    return out if channel is None else out + tlv(UNAVAILABLE_TLV["channel"], struct.pack('<H', channel))


NAMED_EXTENSION = _reg.TAG_NAMED_EXTENSION                # every context's named extension TLV (core §2.2)


def extension(name=b'io.example.note', data=b'x'):
    """A named extension TLV (core §2.2): name_len name data. A reader that does not know the name skips it."""
    return tlv(NAMED_EXTENSION, bytes((len(name),)) + name + data)
