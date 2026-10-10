"""Explicit data/event source model; transport buffering/priority is not modeled."""
import secrets
import struct
import time

from .endpoint import Endpoint
from .virtual_bench import core_v1, with_unit_id
from .virtual_core import Core
from .virtual_notification_model import Notifications


class Data(Notifications):
    name = b'io.github.open-embedded-probe.stream'

    def __init__(self, now, frame_size):
        super().__init__()
        self.now, self.frame_size = now, frame_size
        self.buffers = {}

    def release(self):
        super().release()
        self.buffers.clear()

    def dispatch(self, fn, op, payload):
        result = super().dispatch(fn, op, payload)
        if op in (1, 2):
            self.buffers.pop(fn, None)
        return result

    def feed(self, fn, position, data):
        if fn not in self.subscriptions or not data:
            return
        if fn in self.buffers:
            first_position, buffered, first_time = self.buffers[fn]
            if position != first_position + len(buffered):
                raise ValueError('sample source must be contiguous within a batch')
            self.buffers[fn] = (first_position, buffered + data, first_time)
        else:
            self.buffers[fn] = (position, bytes(data), self.now())
        self.pump()

    def pump(self):
        for fn in list(self.buffers):
            if fn not in self.subscriptions:
                continue
            position, data, first = self.buffers[fn]
            minimum, delay = self.conditions[fn]
            if ((minimum == 0 and delay == 0) or (minimum and len(data) >= minimum)
                    or (delay and self.now() - first >= delay)):
                self.flush(fn)

    def flush(self, fn):
        position, data, first = self.buffers.pop(fn)
        room = self.frame_size - 15
        while data:
            part, data = data[:room], data[room:]
            seq = self.subscriptions[fn]
            self.subscriptions[fn] = (seq + 1) & 65535
            self.pending.append(struct.pack('<BHHQH', 4, fn, seq, position, len(part)) + part)
            position += len(part)


class DataModel:
    def __init__(self, now_ms=None, boot_id=None):
        origin = time.monotonic_ns()
        self.ep = Endpoint(with_unit_id(core_v1(), 'virtual-stream-1'),
                           now_ms or (lambda: (time.monotonic_ns() - origin) // 1_000_000),
                           boot_id=secrets.randbits(32) if boot_id is None else boot_id)
        self.extension = Data(self.ep.now, self.ep.probe.max_frame)
        self.ep.current_core = Core(self.ep, self.extension)

    def handle(self, request):
        # Polling the source does not transmit ahead of a result. Wire order is
        # deliberately outside this model; callers receive events separately.
        response = self.ep.handle(request)
        self.extension.pump()
        return response

    def emit(self, fn, marker):
        self.ep.current_core.tick()
        self.extension.pump()
        self.extension.emit(fn, marker)

    def feed(self, fn, position, data):
        self.ep.current_core.tick()
        self.extension.feed(fn, position, data)

    def drain(self):
        self.ep.current_core.tick()
        self.extension.pump()
        frames, self.extension.pending = self.extension.pending, []
        return frames

    def reboot(self, boot_id):
        self.ep.reboot(boot_id)
        self.extension = Data(self.ep.now, self.ep.probe.max_frame)
        self.ep.current_core = Core(self.ep, self.extension)
