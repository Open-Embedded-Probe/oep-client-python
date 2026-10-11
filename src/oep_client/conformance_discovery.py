"""Core discovery cursor and channel-label invariants, without model imports."""
import struct
from .conformance import require, tlvs


class DiscoveryChecks:
    def __init__(self, checks): self.c = checks

    def lists(self):
        total = len(self.entries)
        for first in (*range(total + 1), min(total + 1, 65535), 65535):
            p = self.c.success(self.c.request(self.c.core['list'], struct.pack('<H', first)))
            require(len(p) >= 3, 'list fixed header')
            size, count = struct.unpack_from('<HB', p); at = 3; page = []
            require(size == total and count <= max(0, total-first), 'list total/count/cursor')
            for _ in range(count):
                require(len(p) >= at + 7, 'list entry header')
                fn, instance, revision, flags, length = struct.unpack_from('<HHBBB', p, at); at += 7
                require(fn and revision and flags == 0 and 1 <= length <= 48 and len(p) >= at+length,
                        'list entry fields')
                name = p[at:at+length].decode('ascii'); at += length
                page.append(dict(fn=fn, instance=instance, revision=revision, name=name))
            require(at == len(p) and page == self.entries[first:first+count], 'list cursor does not select entry suffix')
            require(first >= total or count > 0, 'list cursor made no progress')

    def describes(self):
        rows = self.rows
        for first in (*range(len(rows)+1), len(rows)+1, 65535):
            p = self.c.success(self.c.request(self.c.core['describe'], struct.pack('<HH', 0, first)))
            require(p and p[0] in (0, 1), 'core describe more flag')
            page = tlvs(p[1:]); suffix = rows[first:]
            require(page == suffix[:len(page)] and len(page) <= len(suffix), 'core describe cursor does not select TLV suffix')
            require(p[0] == int(len(page) < len(suffix)) and (not p[0] or page), 'core describe more/progress/end')
            require(all(len(value)+9 <= self.c.max_frame for _, value in page), 'core TLV frame bound')

    def locked(self):
        with self.c.holding(60000): self.lists(); self.describes()
        self.lists(); self.describes()

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
            self.c.confirm(); self.c.identity(); self.c.declarations()
            self.entries = self.c.list_snapshot(); self.rows = self.c.describe(0)
        self.c.check('CORE-DISCOVERY-CONTEXT', 'core §7.1–7.5', context)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        for name, method in [('LIST-CURSORS', self.lists), ('CORE-CURSORS', self.describes), ('LOCKED', self.locked),
                             ('LABELS', self.labels), ('UNKNOWN-FN', self.unknown)]:
            self.c.check('CORE-DISCOVERY-'+name, 'core §2.1/7.2/7.3/7.5', method)
        rows = self.c.results[start:]
        return dict(status='passed' if all(x['status']=='passed' for x in rows) else 'failed',checks=rows,
                    observed=self.c.observed,full_conformance=False,scope='all list and fn-zero describe cursors; labels; lock stability',
                    unchecked=['other-route minimum frame size and firmware-version stability', 'concurrent declaration changes',
                               'Host text rendering', 'complete core/common normative audit'])
