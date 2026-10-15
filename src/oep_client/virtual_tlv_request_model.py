"""Explicit TLV request adoption on the current virtual Core; no physical pins."""
import copy
import struct
from .virtual_core import Core, Reject
from .virtual_resource_model import ResourceModel, Resources


class TlvRequests(Resources):
    fixed = {16: 0, 17: 0}
    free = set()
    name = b'io.github.open-embedded-probe.tlv-request'

    def __init__(self):
        self.values, self.calls = {}, 0

    def release(self):
        self.values.clear()

    def describe(self, fn):
        from .virtual_core import tlv
        return (tlv(7, b'\x10\x03'),)

    def request_tags(self, fn, op, raw):
        first, repeated = {}, []
        while raw:
            if len(raw) < 3: raise Reject(3)
            tag, size = struct.unpack_from('<BH', raw)
            if not tag & 127 or len(raw) < size + 3: raise Reject(3)
            value, raw = raw[3:3 + size], raw[3 + size:]
            number = tag & 127
            if number not in (1, 3):
                if tag & 128: raise Reject(11, bytes((tag,)))
                continue
            if number == 3:
                if len(value) != 1: raise Reject(3)
                if len(repeated) == 255: raise Reject(11, bytes((tag,)))
                repeated.append(value[0]);continue
            width = 2 if (fn, op) == (1, 17) else 1
            if (fn, op) == (2, 16):
                if not value or len(value) != value[0] + 1: raise Reject(3)
                if value[0] > 48: raise Reject(11, bytes((tag,)))
            elif len(value) != width:
                raise Reject(3)
            if (fn, op) == (2, 17):
                if value == b'\x00': raise Reject(3)
                if value != b'\x01': raise Reject(11, bytes((tag,)))
            first.setdefault(number, value)              # the first is used; a later copy is checked too (core §2.3)
        first[3] = repeated
        return first

    def dispatch_request(self, fn, op, fixed, tags):
        default = b'\x00\x00' if (fn, op) == (1, 17) else b'\x01' if (fn, op) == (2, 17) else b'\x00'
        selected = tags.get(1, default)
        repeated = tags[3]
        self.values[fn, op] = (selected, list(repeated))
        self.calls += 1
        return struct.pack('<H', len(selected)) + selected + bytes((len(repeated),)) + bytes(repeated)


class TlvRequestModel(ResourceModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms, boot_id)
        self.extension = TlvRequests()
        self.ep.current_core = Core(self.ep, self.extension)

    def reboot(self, boot_id):
        self.ep.reboot(boot_id)
        self.extension = TlvRequests()
        self.ep.current_core = Core(self.ep, self.extension)

    def request_state(self):
        return {'calls': self.extension.calls,
                'values': [[fn, op, value.hex(), copy.deepcopy(repeated)]
                           for (fn, op), (value, repeated) in sorted(self.extension.values.items())]}
