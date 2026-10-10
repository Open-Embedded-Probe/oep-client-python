"""Software USB transfer/report model for core-v1; not a USB device/gadget.

An independent probe-side parser exercises the public raw-transfer checker.
No host runtime encoder/decoder or conformance module is used here.
"""
from collections import deque
import time


class Usb:
    def __init__(self, endpoint, kind, *, input_size=9, output_size=11, report_id=6,
                 out_packet_size=8, clock=time.monotonic, sleep=time.sleep):
        if not endpoint.probe.core_contract or kind not in ('bulk', 'hid'):
            raise ValueError('USB model requires current core-v1 and bulk/hid')
        self.endpoint, self.kind = endpoint, kind
        self.input_size, self.output_size, self.report_id = input_size, output_size, report_id
        self.out_packet_size, self.clock, self.sleep = out_packet_size, clock, sleep
        self.transport = endpoint.add_transport(4 if kind == 'bulk' else 5, interface=1)
        self.pending, self.inbox = bytearray(), deque()
        self.discard, self.last = False, None
        self.closed = False

    def write(self, transfer, method='out'):
        if self.closed:
            raise OSError('USB model closed')
        if method not in ('out', 'set-report') or self.kind == 'bulk' and method != 'out':
            raise ValueError('unsupported raw USB write method')
        data = bytes(transfer)
        if self.kind == 'hid':
            if len(data) != self.output_size:
                raise ValueError('wrong model output report size')
            offset = int(bool(self.report_id))
            if offset and data[0] != self.report_id:
                return len(transfer)
            n = data[offset] | data[offset + 1] << 8
            if n > len(data) - offset - 2:
                self.pending.clear()
                self.discard, self.last = True, self.clock()
                return len(transfer)
            data = data[offset + 2:offset + 2 + n]  # receiver ignores padding, including nonzero
        self.receive(data)
        return len(transfer)

    def receive(self, data):
        if not data:
            return
        now = self.clock()
        if self.last is not None and now - self.last >= 0.2:
            self.pending.clear()
            self.discard = False
        self.last = now
        if self.discard:
            return
        self.pending.extend(data)
        while len(self.pending) >= 2:
            n = self.pending[0] | self.pending[1] << 8
            if n > self.endpoint.probe.max_frame:
                self.pending.clear()
                self.discard = True
                return
            if len(self.pending) < n + 2:
                return
            message = bytes(self.pending[2:n + 2])
            del self.pending[:n + 2]
            if n:
                response = self.endpoint.handle(message, transport=self.transport)
                if response is not None:
                    self.respond(response)

    def respond(self, message):
        data = bytes((len(message) & 255, len(message) >> 8)) + message
        if self.kind == 'bulk':
            # End each modeled IN transfer with a short final part or a ZLP.
            for at in range(0, len(data), self.out_packet_size):
                self.inbox.append(data[at:at + self.out_packet_size])
            if len(data) % self.out_packet_size == 0:
                self.inbox.append(b'')
        else:
            capacity = self.input_size - 2 - bool(self.report_id)
            for at in range(0, len(data), capacity):
                part = data[at:at + capacity]
                report = bytes((len(part) & 255, len(part) >> 8)) + part + bytes(capacity - len(part))
                self.inbox.append((bytes((self.report_id,)) if self.report_id else b'') + report)

    def read(self, timeout):
        if self.closed:
            raise OSError('USB model closed')
        if self.inbox:
            return self.inbox.popleft()
        self.sleep(timeout)
        return None

    def close(self):
        self.closed = True
