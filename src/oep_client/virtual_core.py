"""Virtual fn 0 implementation of the pre-freeze core (oep-spec 4681d22).

Independent of the conformance checker and the legacy interface registry.
The core-v1 profile has no resources, subscriptions, interfaces or targets.
Explicit sample models may supply an independently defined extension hook.
"""
from collections import OrderedDict
import struct

SPEC_COMMIT = '4681d2258c8a30cb03df3d93845b03bc6d8acd0a'
OPS = {1, 2, 3, 4, 16, 17, 18, 19}
FREE = {1, 2, 3, 4, 19}
FIXED = {1: 6, 2: 2, 3: 4, 4: 0, 16: 5, 17: 0, 18: 0, 19: 0}


def tlv(tag, value):
    return bytes((tag,)) + struct.pack('<H', len(value)) + value


class Reject(Exception):
    def __init__(self, reason, payload=b''):
        self.reason, self.payload = reason, payload


def tail(data, owner=False):
    first = {}
    while data:
        if len(data) < 3:
            raise Reject(3)
        tag, size = struct.unpack_from('<BH', data)
        if not tag & 127 or len(data) < 3 + size:
            raise Reject(3)
        value, data = data[3:3 + size], data[3 + size:]
        key = tag & 127
        if owner and key == 1:
            if key in first:
                continue
            if not 1 <= size <= 32:
                raise Reject(3)
            try:
                value.decode('utf-8')
            except UnicodeDecodeError:
                raise Reject(3) from None
            first[key] = value
        elif tag & 128:
            raise Reject(11, bytes((tag,)))
    return first


class Core:
    def __init__(self, endpoint, extension=None):
        self.ep = endpoint
        # Only explicit current-contract models supply this hook. Legacy profiles
        # and the core-v1 profile continue to expose fn 0 alone.
        self.extension = extension
        self.holder, self.last, self.owner = None, None, b''
        self.lease, self.deadline, self.high = 3000, 0, 0
        self.cache = OrderedDict()
        self.generation = 0
        self.renewal = None

    def release(self):
        self.generation += 1
        if self.extension is not None:
            self.extension.release()
        self.holder, self.owner = None, b''

    def tick(self):
        if self.holder is not None and self.ep.now() >= self.deadline:
            self.release()

    def locked(self):
        remaining = max(1, self.deadline - self.ep.now())
        return struct.pack('<I', remaining) + (tlv(1, self.owner) if self.owner else b'')

    def response_sent(self, token, *, check_expiry=True):
        if check_expiry:
            self.tick()
        if token is not None and token == (self.holder, self.generation):
            self.deadline = self.ep.now() + self.lease

    def handle(self, data, transport, *, admission_reason=None, defer_send=False):
        self.renewal = None
        return self._handle(data, transport, admission_reason=admission_reason, defer_send=defer_send)

    def _handle(self, data, transport, *, admission_reason=None, defer_send=False):
        self.tick()
        started = self.ep.now()
        if len(data) < 10 or data[0] != 1:
            return None
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', data)
        payload = data[10:]

        def answer(reason=None, value=b''):
            return struct.pack('<BHBB', 2, corr, int(reason is None), reason or 0) + value

        # Header refusals precede replay, session and payload checks.
        if corr == 0:
            return answer(3)
        if fn and (self.extension is None or fn not in self.extension.functions):
            return answer(1)
        fixed = FIXED if fn == 0 else self.extension.fixed
        if op not in fixed:
            return answer(2)
        free = op in FREE if fn == 0 else op in self.extension.free
        opening = fn == 0 and op == 16
        if sid == 0 and not free and not opening:
            return answer(9)
        if sid and sid == self.last:
            if corr in self.cache:
                request, result = self.cache[corr]
                if request is None or result is None:
                    return answer(12)
                return result if request == data else answer(3)
            if corr <= self.high:
                return answer(12)

        passed_session = False
        try:
            if opening:
                if sid == 0:
                    raise Reject(3)
                if self.holder is None and sid == self.last:
                    raise Reject(7)
                force = len(payload) >= 5 and payload[4] != 0
                if self.holder is not None and sid != self.holder and not force:
                    raise Reject(8, self.locked())
                passed_session = sid == self.holder
            elif sid:
                if self.holder is None:
                    raise Reject(7)
                if sid != self.holder:
                    raise Reject(8, self.locked())
                passed_session = True
            # Explicit admission models inject state refusals only after header,
            # replay and session checks. Rejected results use the same cache path.
            if admission_reason is not None:
                raise Reject(admission_reason)
            if len(payload) < fixed[op]:
                raise Reject(3)
            tags = tail(payload[fixed[op]:], owner=opening)
            if fn:
                value = self.extension.dispatch(fn, op, payload[:fixed[op]])
            elif op == 1:
                magic, minimum, maximum = payload[:4], payload[4], payload[5]
                if magic != b'OEP?' or minimum > maximum:
                    raise Reject(3)
                if not minimum <= 1 <= maximum:
                    raise Reject(11, b'\0' + tlv(1, b'\x01\x01'))
                value = b'OEP!' + struct.pack('<BBHIBI', 1, 0, self.ep.probe.max_frame,
                                              self.ep.window, self.ep.max_inflight, self.ep.boot_id)
                value += tlv(1, bytes((transport,)))
            elif op == 2:
                value = (self.extension.list_page(struct.unpack_from('<H', payload)[0],
                                                  self.ep.probe.max_frame - 5)
                         if self.extension is not None else b'\0\0\0')
            elif op == 3:
                target, first = struct.unpack_from('<HH', payload)
                if target and (self.extension is None or target not in self.extension.functions):
                    raise Reject(1)
                rows = list(self.ep.static[0] if target == 0 else self.extension.describe(target))
                page, index = bytearray(), first
                while index < len(rows) and len(page) + len(rows[index]) + 6 <= self.ep.probe.max_frame:
                    page.extend(rows[index])
                    index += 1
                value = bytes((int(index < len(rows)),)) + page
            elif op == 4:
                value = struct.pack('<IQ', self.ep.boot_id, self.ep.now_ns())
            elif op == 16:
                lease = struct.unpack_from('<I', payload)[0]
                self.lease = min(60000, max(1000, lease or self.ep.lease_default_ms))
                if sid != self.holder:
                    self.release()
                    self.holder, self.last, self.owner = sid, sid, tags.get(1, b'')
                    self.high = 0
                    self.cache.clear()
                    self.deadline = started + self.lease
                passed_session = True
                value = struct.pack('<II', self.lease, self.ep.boot_id)
            elif op == 17:
                self.release()
                value = b''
            elif op == 18:
                value = b''
            else:
                value = struct.pack('<BI', int(self.holder is not None),
                                    max(0, self.deadline - self.ep.now()) if self.holder else 0)
                if self.holder and self.owner:
                    value += tlv(1, self.owner)
            result = answer(value=value)
        except Reject as exc:
            result = answer(exc.reason, exc.payload)
        if passed_session and sid == self.holder:
            self.renewal = (sid, self.generation)
            if defer_send:
                # Pause only for actual execution. Waiting to write is idle time.
                self.deadline += self.ep.now() - started
            else:
                # Synchronous model API hands over the whole reply here.
                self.response_sent(self.renewal, check_expiry=False)
        if sid and sid == self.last:
            cap = self.ep.remember_max
            self.cache[corr] = (data if len(data) <= cap else None, result if len(result) <= cap else None)
            self.high = max(self.high, corr)
            while len(self.cache) > max(8, self.ep.max_inflight):
                self.cache.popitem(last=False)
        return result
