"""Core discovery cursor and channel-label invariants, without model imports."""
import struct
from .conformance import require, tlvs


class DiscoveryChecks:
    def __init__(self, checks, *, supported_revisions=None):
        self.c = checks
        self.supported_revisions = supported_revisions

    def lists(self):
        total = len(self.entries)
        for first in (*range(total + 1), min(total + 1, 65535), 65535):
            p = self.c.success(self.c.request(self.c.core['list'], struct.pack('<H', first)))
            require(len(p) >= 3, 'list fixed header')
            size, count = struct.unpack_from('<HB', p); at = 3; page = []
            require(size == total and count <= max(0, total-first), 'list total/count/cursor')
            for _ in range(count):
                require(len(p) >= at + 6, 'list entry header')
                fn, instance, revision, length = struct.unpack_from('<HHBB', p, at); at += 6
                require(fn and 1 <= length <= 48 and len(p) >= at+length,
                        'list entry fields')                    # revision: any u8 (core §7.2)
                name = p[at:at+length].decode('ascii'); at += length
                page.append(dict(fn=fn, instance=instance, revision=revision, name=name))
            tlvs(p[at:])                                          # TLVs may follow the entries (core §2.3)
            require(page == self.entries[first:first+count], 'list cursor does not select entry suffix')
            require(first >= total or count > 0, 'list cursor made no progress')
            if first + count < total:  # core §7.2: as many as one frame holds - the next entry would not fit
                require(5 + len(p) + 6 + len(self.entries[first+count]['name']) > self.c.max_frame,
                        'list page stops before the frame is full')

    def describes(self):
        rows = self.rows
        for first in (*range(len(rows)+1), len(rows)+1, 65535):
            p = self.c.success(self.c.request(self.c.core['describe'], struct.pack('<HH', 0, first)))
            require(p and p[0] in (0, 1), 'core describe more flag')
            page = tlvs(p[1:]); suffix = rows[first:]
            require(page == suffix[:len(page)] and len(page) <= len(suffix), 'core describe cursor does not select TLV suffix')
            require(p[0] == int(len(page) < len(suffix)) and (not p[0] or page), 'core describe more/progress/end')
            if p[0]:  # core §7.3: from first, as many as one frame holds - the next TLV would not fit
                require(5 + len(p) + 3 + len(suffix[len(page)][1]) > self.c.max_frame,
                        'core describe page stops before the frame is full')
            require(all(len(value)+9 <= self.c.max_frame for _, value in page), 'core TLV frame bound')

    def locked(self):
        with self.c.holding(60000): self.lists(); self.describes()
        self.lists(); self.describes()

    def revision_profile(self):
        supported = self.supported_revisions
        require(isinstance(supported, tuple) and len(supported) == 2 and
                all(type(v) is int for v in supported) and 0 <= supported[0] <= 1 <= supported[1] <= 255,
                'explicit supported revision interval containing revision1 required')
        return supported

    def revisions(self):
        supported = self.revision_profile()
        low, high = supported
        points = sorted({0, 1, low, high, min(low + 1, 255), min(high + 1, 255), 254, 255})
        observations = []
        original_boot = self.c.boot
        with self.c.holding(60000) as sid:
            original, original_clock = self.c.clock(sid)
            for minimum in points:
                for maximum in points:
                    request = self.c.request(self.c.core['confirm'], b'OEP?' + bytes((minimum, maximum)))
                    if minimum > maximum:
                        reason = 'malformed'
                        payload = self.c.rejected(request, reason); tlvs(payload)
                    elif maximum < low or minimum > high:
                        reason = 'unsupported'
                        payload = self.c.rejected(request, reason)
                        require(payload[:1] == b'\0', 'confirm unsupported fixed tag must be zero')
                        rows = tlvs(payload[1:]); values = [v for tag, v in rows if tag == 1]
                        require(values and values[0] == bytes(supported), 'confirm supported interval missing/wrong')
                    else:
                        reason = None
                        payload = self.c.success(request)
                        require(16 <= len(payload) <= 59 and payload[:4] == b'OEP!', 'range confirm shape/magic')
                        revision, frame, window, inflight, boot = struct.unpack_from('<BHIBI', payload, 4)
                        require(revision == min(maximum, high), 'confirm did not choose highest supported revision')
                        require(frame >= 64 and window >= frame and inflight >= 1 and boot == self.c.boot,
                                'range confirm limits/boot')
                        values = [v for tag, v in tlvs(payload[16:]) if tag == 1]
                        require(values and values[0] == bytes((self.c.confirm_transport,)), 'range confirm transport changed')
                    observations.append({'minimum': minimum, 'maximum': maximum, 'reason': reason,
                                         'request_hex': request.hex(), 'reply_payload_hex': payload.hex()})
            # Return the injection route to the known revision before normal session operations.
            self.c.confirm()
            require(self.c.boot == original_boot, 'range injection changed boot')
            # A replay may return result_lost when its response exceeds retention bounds.
            answer = self.c.exchange(original)
            require((answer[3:5] == b'\x01\0' and answer[5:] == original_clock) or
                    answer[3:5] == bytes((0, self.c.reasons['result_lost'])),
                    'range confirm changed owned replay result')
            tlvs(self.c.success(self.c.request(self.c.core['keepalive'], session=sid)))
        self.c.observed['confirm_revision_ranges'] = observations

    def labels(self):
        tags = self.c.reg['core']['tlv']['describe']; values = dict(reversed(self.rows))
        channels = values.get(tags['channels'], b'\0\0')
        require(len(channels) == 2, 'core channels u16')
        count = int.from_bytes(channels, 'little')
        for tag, value in self.rows:
            if tag != tags['label']: continue
            require(len(value) >= 2, 'label channel u16 required')
            require(int.from_bytes(value[:2], 'little') < count, 'label channel outside declared channels')
            value[2:].decode('utf-8')  # Text may contain C0; rendering is a Host contract.

    def unknown(self):
        used = {e['fn'] for e in self.entries} | {0}
        missing = next(fn for fn in range(1,65536) if fn not in used) if len(used)<65536 else None
        if missing is not None:
            for first in (0,65535):
                self.c.rejected(self.c.request(self.c.core['describe'], struct.pack('<HH', missing, first)), 'unknown_function')
        require(self.c.list_snapshot() == self.entries and self.c.describe(0) == self.rows, 'unknown describe changed declarations')

    def run(self):
        start = len(self.c.results)
        def context():
            if self.supported_revisions is not None: self.revision_profile()
            self.c.confirm(); self.c.identity(); self.c.declarations()
            self.entries = self.c.list_snapshot(); self.rows = self.c.describe(0)
        self.c.check('CORE-DISCOVERY-CONTEXT', 'core §7.1–7.5', context)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        for name, method in [('LIST-CURSORS', self.lists), ('CORE-CURSORS', self.describes), ('LOCKED', self.locked),
                             ('LABELS', self.labels), ('UNKNOWN-FN', self.unknown)]:
            self.c.check('CORE-DISCOVERY-'+name, 'core §2.1/7.2/7.3/7.5', method)
        if self.supported_revisions is not None:
            self.c.check('CORE-DISCOVERY-REVISIONS', 'core §7.1; explicit supported revision profile', self.revisions)
        rows = self.c.results[start:]
        return dict(status='passed' if all(x['status']=='passed' for x in rows) else 'failed',checks=rows,
                    observed=self.c.observed,full_conformance=False,scope='all list and fn-zero describe cursors; labels; lock stability; optional explicit revision interval',
                    unchecked=['other-route minimum frame size and firmware-version stability', 'concurrent declaration changes',
                               'Host text rendering', 'multiple-revision fixture execution and per-route revision application', 'complete core/common normative audit'])
