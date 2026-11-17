"""USB-specific stimuli against a raw adapter; usable by custom equipment runners."""
from . import NotApplicable, Violation, require, tlvs
from .serial import WireError
from .usb import frame

USB_CASES = tuple(('CORE-USB-' + name.upper(), name) for name in
                  ('split', 'coalesce', 'zero', 'short', 'role', 'truncated', 'oversized', 'boundary'))
BULK_CASES = (('CORE-USB-IN-END', 'in-end'),)   # the probe's IN transfers end (transports §1, the USB bundling)
HID_CASES = tuple(('CORE-HID-' + name.upper(), name) for name in
                  ('empty-report', 'padding', 'report-id', 'count', 'lost-report', 'set-report'))


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
            wire.abandon()   # HID: the next report's first restarts the probe's reading (transports §1 rule 6)
            result(exchange([good], 1))
        return
    if kind == 'oversized':
        if check.max_frame == 65535:
            raise NotApplicable('u16 length cannot express max_frame + 1')
        replies = exchange([(check.max_frame + 1).to_bytes(2, 'little') + good], 0, silence=gap + 0.1)
        # The valid request inside the oversized frame's bytes is discarded with them: on vendor bulk through the gap,
        # on HID up to the next report with a first (none of its reports has one).
        require(not replies, 'USB corruption did not discard input through frame gap')
        wire.abandon()
        recovery = check.request(check.core['clock'])
        result(exchange([frame(recovery)], 1), recovery)
        return
    if kind == 'count':
        # HID rule 7: a count over what the report holds - the report dropped, the stream broken till the next report
        # with a first. The dropped report begins another request, its reports after it have no first: a probe that
        # read the report anyway would answer that one too; the valid request (with a first) is answered.
        at = int(bool(wire.report_id))
        other = check.request(check.core['clock'])
        reps = wire.reports(frame(other))                    # its own reports: the valid request starts a new one
        bad = bytearray(reps[0])
        bad[at:at + 2] = (wire.output_size - wire_head(wire) + 1).to_bytes(2, 'little')
        result(exchange([bytes(bad)] + reps[1:] + wire.reports(good), 1, raw=True), req)
        return
    if kind == 'lost-report':
        # HID rule 6: a report missing inside a frame - the probe's own reading disagrees with the next report's
        # first, drops the frame begun and reads on from first: the valid request after it is answered.
        room = wire.output_size - wire_head(wire)
        cut = wire.reports(frame(bytes((1,)) + bytes(3 * room)))   # a frame of 4 reports or more
        if len(cut) < 3:
            raise NotApplicable('HID reports too large for a frame of three')
        result(exchange(cut[:1] + cut[2:] + wire.reports(good), 1, raw=True), req)
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
        capacity = wire.output_size - wire_head(wire)
        chunks = [wire.pack(b'', padding=255)]
        # The first report deliberately holds one counted byte, the rest of it nonzero padding (whatever the sizes).
        chunks += [wire.pack(good[:1], padding=255)]
        chunks += [wire.pack(good[i:i + capacity], padding=255) for i in range(1, len(good), capacity)]
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


def wire_head(wire):
    """An output report's bytes before the stream: count, first and the report ID if one is declared."""
    from .usb import HID_HEAD
    return HID_HEAD + bool(wire.report_id)
