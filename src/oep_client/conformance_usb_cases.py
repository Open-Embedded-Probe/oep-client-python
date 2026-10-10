"""USB-specific stimuli against a raw adapter; usable by custom equipment runners."""
from .conformance import NotApplicable, require, tlvs
from .conformance_serial import WireError
from .conformance_usb import frame

USB_CASES = tuple(('CORE-USB-' + name.upper(), name) for name in
                  ('split', 'coalesce', 'zero', 'short', 'role', 'truncated', 'oversized', 'boundary'))
HID_CASES = tuple(('CORE-HID-' + name.upper(), name) for name in
                  ('empty-report', 'padding', 'report-id', 'count', 'set-report'))


def run(check, kind):
    wire = check.usb_wire
    req = check.request(check.core['clock'])  # no session, pins or target state
    good = frame(req)
    gap = check.reg.get('timing', {}).get('probe_frame_gap_ms', 200) / 1000

    def exchange(chunks, count, **kwargs):
        record = {'usb_stimulus': kind}
        check.trace.append(record)
        try:
            replies = wire.exchange(chunks, count, **kwargs)
            record['responses_hex'] = [reply.hex() for reply in replies]
            return replies
        except (WireError, TimeoutError, OSError):
            check.abort = True
            raise
        finally:
            record['usb_wire'] = wire.last_exchange

    def result(replies, request=req):
        require(len(replies) == 1, 'ignored USB input produced a result or duplicate')
        saved = check.send
        try:
            check.send = lambda _: replies[0]
            payload = check.success(request)
            require(len(payload) >= 12 and int.from_bytes(payload[:4], 'little') == check.boot,
                    'USB clock result')
            tlvs(payload[12:])
        finally:
            check.send = saved

    if kind == 'truncated':
        for cut in (1, 2, len(good) - 1):
            # The partial stream must produce nothing; its remainder is abandoned.
            replies = exchange([good[:cut]], 0, silence=gap + 0.1)
            if replies:
                check.abort = True
            require(not replies, 'incomplete USB input produced a result')
            result(exchange([good], 1))
        return
    if kind in ('oversized', 'count'):
        if kind == 'oversized':
            if check.max_frame == 65535:
                raise NotApplicable('u16 length cannot express max_frame + 1')
            chunks = [(check.max_frame + 1).to_bytes(2, 'little') + good]
            kwargs = {}
        else:
            at = int(bool(wire.report_id))
            bad = bytearray(wire.pack(b''))
            bad[at:at + 2] = (wire.output_size - at - 1).to_bytes(2, 'little')
            chunks, kwargs = [bytes(bad)] + wire.reports(good), {'raw': True}
        replies = exchange(chunks, 0, silence=gap + 0.1, **kwargs)
        # All bytes through the gap, including the following valid request, are discarded.
        require(not replies, 'USB corruption did not discard input through frame gap')
        recovery = check.request(check.core['clock'])
        result(exchange([frame(recovery)], 1), recovery)
        return
    kwargs = {}
    if kind == 'split':
        chunks = [bytes((byte,)) for byte in good]
    elif kind == 'coalesce':
        chunks = [frame(bytes((3,)) + req[1:]) + good]
    elif kind == 'zero':
        chunks = [b'\0\0' * 3 + good]
    elif kind == 'short':
        chunks = [frame(req[:9]) + good]
    elif kind == 'role':
        chunks = [frame(bytes((0,)) + req[1:]) + good]
    elif kind == 'boundary':
        size = check.max_frame - 13
        req = check.request(check.core['clock'], b'\x7f' + size.to_bytes(2, 'little') + b'x' * size)
        require(len(req) == check.max_frame, 'tester max_frame stimulus size')
        chunks = [frame(req)]
    elif kind == 'empty-report':
        chunks, kwargs = [wire.pack(b'')] * 3 + wire.reports(good), {'raw': True}
    elif kind == 'padding':
        capacity = wire.output_size - 2 - bool(wire.report_id)
        chunks = [wire.pack(b'', padding=255)]
        # Last report is deliberately short in counted bytes, with nonzero padding.
        chunks += [wire.pack(good[i:i + capacity], padding=255) for i in range(0, len(good), capacity)]
        kwargs = {'raw': True}
    elif kind == 'report-id':
        if not wire.report_id:
            raise NotApplicable('HID descriptor declares no report ID')
        wrong = wire.report_id % 255 + 1
        chunks = [bytes((wrong,)) + report[1:] for report in wire.reports(good)] + wire.reports(good)
        kwargs = {'raw': True}
    elif kind == 'set-report':
        chunks, kwargs = [good], {'method': 'set-report'}
    else:
        raise ValueError(kind)
    result(exchange(chunks, 1, **kwargs), req)
