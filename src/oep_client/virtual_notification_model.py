"""Explicit in-process event-only extension; no physical or wire queue model."""
import secrets
import struct
import time

from .endpoint import Endpoint
from .virtual_bench import core_v1, with_unit_id
from .virtual_core import Core
from .virtual_resource_model import Resources


class Notifications(Resources):
    fixed = {1: 6, 2: 0, **Resources.fixed}
    name = b'io.github.open-embedded-probe.notify'

    def __init__(self):
        super().__init__()
        self.subscriptions = {}
        self.conditions = {}
        self.pending = []

    def release(self):
        super().release()
        self.subscriptions.clear()
        self.conditions.clear()
        self.pending.clear()

    def describe(self, fn):
        from .virtual_core import tlv
        return (tlv(7, b'\x00\x06\x00\x0f'),)

    def dispatch(self, fn, op, payload):
        if op == 1:
            self.conditions[fn] = struct.unpack('<HI', payload)
            self.subscriptions[fn] = 0
            return b''
        if op == 2:
            self.subscriptions.pop(fn, None)
            self.conditions.pop(fn, None)
            return b''
        return super().dispatch(fn, op, payload)

    def emit(self, fn, marker):
        if fn not in self.subscriptions:
            return
        seq = self.subscriptions[fn]
        self.subscriptions[fn] = (seq + 1) & 65535
        self.pending.append(struct.pack('<BHHBI', 3, fn, seq, 1, marker))


class NotificationModel:
    def __init__(self, now_ms=None, boot_id=None):
        origin = time.monotonic_ns()
        self.ep = Endpoint(with_unit_id(core_v1(), 'virtual-notify-1'),
                           now_ms or (lambda: (time.monotonic_ns() - origin) // 1_000_000),
                           boot_id=secrets.randbits(32) if boot_id is None else boot_id)
        self.extension = Notifications()
        self.ep.current_core = Core(self.ep, self.extension)

    def handle(self, request):
        return self.ep.handle(request)

    def emit(self, fn, marker):
        self.ep.current_core.tick()
        self.extension.emit(fn, marker)

    def drain(self):
        self.ep.current_core.tick()
        frames, self.extension.pending = self.extension.pending, []
        return frames

    def reboot(self, boot_id):
        self.ep.reboot(boot_id)
        self.extension = Notifications()
        self.ep.current_core = Core(self.ep, self.extension)
