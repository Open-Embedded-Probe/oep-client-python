"""USB-specific stimuli against a raw adapter; usable by custom equipment runners."""
from . import NotApplicable, Violation, require, tlvs
from .serial import WireError
from .usb import frame

USB_CASES = tuple(('CORE-USB-' + name.upper(), name) for name in
                  ('split', 'coalesce', 'zero', 'short', 'role', 'truncated', 'oversized', 'boundary'))
BULK_CASES = (('CORE-USB-IN-END', 'in-end'),)   # the probe's IN transfers end (transports §1, the USB bundling)
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

    if kind == 'in-end':
        in_end(check, wire, exchange)
        return
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
        req = check.request(check.core['clock'], b'\x7f' + size.to_bytes(2, 'little') + b'\x04io.x' + b'x' * (size - 5))
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


def in_end(check, wire, exchange):
    """A probe whose last bulk IN transfer is a multiple of wMaxPacketSize ends it (a zero-length packet, a length-0
    frame, or the last byte split off, transports §1): answers framed to exactly 1 and 2 packets must arrive without
    more data after them. oep.probe.link's source makes them (its length chosen); without it: not applicable."""
    import struct
    if not wire.in_packet_size:
        raise NotApplicable('the IN packet size is not known')
    link = next((e for e in check.observed.get('interfaces', []) if e['name'] == 'oep.probe.link'), None)
    if link is None:
        raise NotApplicable('oep.probe.link is not listed (no answer of a chosen length)')
    spec = next(i for i in check.reg['interface'] if i['name'] == 'oep.probe.link')
    source = next(o['code'] for o in spec['op'] if o['name'] == 'source')
    head = 2 + 5 + 2                      # length(u16), the answer's header (core §4.2), len(u16)
    for packets in (1, 2):
        n = packets * wire.in_packet_size - head
        if n + 7 > check.max_frame or n < 0:
            raise NotApplicable(f'max_frame {check.max_frame} below an answer of {packets} IN packets')
        req = check.request(source, struct.pack('<I', n), fn=link['fn'])
        try:
            replies = exchange([frame(req)], 1)
        except TimeoutError as exc:
            raise Violation(f'an answer of exactly {packets} IN packets did not arrive: its transfer not ended'
                                 f' ({exc})') from exc
        require(len(replies) == 1, 'one answer expected')
        saved = check.send
        try:
            check.send = lambda _: replies[0]
            payload = check.success(req)
        finally:
            check.send = saved
        size = int.from_bytes(payload[:2], 'little')
        require(size == n and payload[2:2 + n] == bytes(k & 0xFF for k in range(n)),
                f'source of {n} bytes answered {size}')
