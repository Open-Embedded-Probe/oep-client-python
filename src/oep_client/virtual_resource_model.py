"""Explicit, in-process sample extension; neither an OEP interface nor hardware.

Its wire contract is documented in docs/resource-conformance.ja.md. The sample
uses the current core's session/replay machinery, not legacy target operations.
"""
import secrets
import struct
import time

from .endpoint import Endpoint
from .virtual_bench import core_v1, with_unit_id
from .virtual_core import Core, Reject, tlv


class Resources:
    functions = (1, 2)
    fixed = {16: 1, 17: 3, 18: 3, 19: 0}
    free = {19}
    name = b'io.github.open-embedded-probe.resource'

    def __init__(self):
        self.next_id = 1
        self.live = {}

    def release(self):
        self.live.clear()

    def list_page(self, first, limit):
        rows = []
        for index, fn in enumerate(self.functions):
            rows.append(struct.pack('<HHBBB', fn, index, 1, 0, len(self.name)) + self.name)
        page = bytearray(struct.pack('<HB', len(rows), 0))
        count = 0
        for row in rows[first:]:
            if len(page) + len(row) > limit:
                break
            page.extend(row)
            count += 1
        page[2] = count
        return bytes(page)

    def describe(self, fn):
        return (tlv(7, b'\x10\x0f'),)

    def dispatch(self, fn, op, payload):
        if op == 19:
            rows = [(rid, kind) for rid, (owner, kind) in self.live.items() if owner == fn]
            return bytes((len(rows),)) + b''.join(struct.pack('<HB', rid, kind) for rid, kind in rows)
        kind = payload[0]
        if kind not in (1, 2):
            raise Reject(3)
        if op == 16:
            if self.next_id > 65535 or len(self.live) >= 8:
                raise Reject(4, tlv(1, b'\x02'))
            rid = self.next_id
            self.next_id += 1
            self.live[rid] = (fn, kind)
            return struct.pack('<H', rid)
        rid = struct.unpack_from('<H', payload, 1)[0]
        if rid not in self.live:
            raise Reject(10)
        if self.live[rid] != (fn, kind):
            raise Reject(4, tlv(1, b'\x06'))
        if op == 17:
            del self.live[rid]
        return b''


class ResourceModel:
    def __init__(self, now_ms=None, boot_id=None):
        origin = time.monotonic_ns()
        self.ep = Endpoint(with_unit_id(core_v1(), 'virtual-resource-1'),
                           now_ms or (lambda: (time.monotonic_ns() - origin) // 1_000_000),
                           boot_id=secrets.randbits(32) if boot_id is None else boot_id)
        self.extension = Resources()
        self.ep.current_core = Core(self.ep, self.extension)

    def handle(self, request):
        return self.ep.handle(request)

    def reboot(self, boot_id):
        self.ep.reboot(boot_id)
        self.extension = Resources()
        self.ep.current_core = Core(self.ep, self.extension)
