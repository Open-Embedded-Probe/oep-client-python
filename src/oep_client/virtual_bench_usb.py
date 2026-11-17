"""Software USB transfer/report model for core-v1; not a USB device/gadget.

An independent probe-side parser exercises the public raw-transfer checker.
No host runtime encoder/decoder or conformance module is used here.
"""
from collections import deque
import time

from . import registry as reg

HID_HEAD = reg.USB['hid_report_header_bytes']     # count(u16), first(u16) after the report ID (transports §1)
HID_NONE = reg.USB['hid_no_frame_start']


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
        self.hid_synced = False             # HID: just opened, read from the first report with a first (rule 6)
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
            first = data[offset + 2] | data[offset + 3] << 8
            if n > len(data) - offset - HID_HEAD or (first != HID_NONE and first >= n):
                self.pending.clear()                # rule 7: dropped, broken till the next first
                self.hid_synced = False
                return len(transfer)
            if not n:
                return len(transfer)
            data = data[offset + HID_HEAD:offset + HID_HEAD + n]  # receiver ignores padding, including nonzero
            if self.hid_synced and self._next_start(len(data), data) != first:
                self.pending.clear()                # rule 6: a report is missing before this one
                self.hid_synced = False
            if not self.hid_synced:
                if first == HID_NONE:
                    return len(transfer)
                self.pending.clear()
                self.hid_synced, data = True, data[first:]
        self.receive(data)
        return len(transfer)

    def _next_start(self, n, data):
        """Where the next frame starts in a report's n bytes `data`, by this reader's own pending frame."""
        if not self.pending:
            return 0
        if len(self.pending) == 1:
            end = 2 + (self.pending[0] | data[0] << 8) - 1
        else:
            end = 2 + (self.pending[0] | self.pending[1] << 8) - len(self.pending)
        return end if end < n else HID_NONE

    def receive(self, data):
        if not data:
            return
        now = self.clock()
        if self.kind == 'bulk' and self.last is not None and now - self.last >= 0.2:
            self.pending.clear()                    # the gap rule (transports §2): not on HID, which reads from first
            self.discard = False
        self.last = now
        if self.discard:
            return
        self.pending.extend(data)
        while len(self.pending) >= 2:
            n = self.pending[0] | self.pending[1] << 8
            if n > self.endpoint.probe.max_frame:
                self.pending.clear()
                if self.kind == 'hid':
                    self.hid_synced = False         # till the next report with a first
                else:
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
            capacity = self.input_size - HID_HEAD - bool(self.report_id)
            for at in range(0, len(data), capacity):
                part = data[at:at + capacity]
                first = 0 if at == 0 else HID_NONE   # one message per report run: it starts in the first
                report = (bytes((len(part) & 255, len(part) >> 8, first & 255, first >> 8)) + part
                          + bytes(capacity - len(part)))
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
