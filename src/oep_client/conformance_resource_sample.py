"""Checker-side encoding of the documented sample extension, not probe code."""
import struct

from .conformance import require, tlvs


class SampleResourceAdapter:
    def __init__(self, checks, *, functions, kinds, capacity):
        self.c = checks
        self.slots = tuple((fn, kind) for fn in functions for kind in kinds)
        self.capacity = capacity

    def request(self, action, sid, slot, rid=0):
        fn, kind = slot
        op = {'create': 16, 'close': 17, 'use': 18}[action]
        payload = bytes((kind,)) + (struct.pack('<H', rid) if action != 'create' else b'')
        return self.c.request(op, payload, session=sid, fn=fn)

    def decode_created(self, payload):
        require(len(payload) >= 2, 'sample create payload'); tlvs(payload[2:])  # fixed part, then a TLV tail (core §2.3)
        return struct.unpack_from('<H', payload)[0]

    def decode_empty(self, payload):
        tlvs(payload)                                     # no fixed part, a TLV tail may follow (core §2.3, §4.3)

    def snapshot(self):
        result = {slot: set() for slot in self.slots}
        for fn in sorted({fn for fn, _ in self.slots}):
            payload = self.c.success(self.c.request(19, fn=fn))
            require(payload and len(payload) >= 1 + 3 * payload[0], 'sample snapshot payload'); tlvs(payload[1 + 3 * payload[0]:])  # fixed part, then a TLV tail (core §2.3)
            for at in range(1, 1 + 3 * payload[0], 3):
                rid, kind = struct.unpack_from('<HB', payload, at)
                require((fn, kind) in result, 'sample unexpected resource kind')
                require(rid not in result[fn, kind], 'sample duplicate resource')
                result[fn, kind].add(rid)
        return result
