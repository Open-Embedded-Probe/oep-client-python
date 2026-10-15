"""Core reader TLV contracts for an explicitly adopted example payload.

The decoder adapter returns fixed/blob/first/repeated, or raises ValueError for
an unusable payload. This is a Host component check, never a Probe verdict.
"""
import hashlib
from pathlib import Path
import struct

from .conformance import require, spec_identity


def field(tag, value):
    return struct.pack('<BH', tag, len(value)) + value


def payload(tail=b'', blob=b'abc'):
    return struct.pack('<HH', 0x1234, len(blob)) + blob + tail


class HostTlvChecks:
    def __init__(self, decoder, *, spec):
        self.decoder = decoder
        _, self.identity = spec_identity(spec)
        self.observations = []

    def read(self, raw):
        record = {'payload_hex': raw.hex()}
        self.observations.append(record)
        try:
            result = self.decoder(raw)
        except ValueError as exc:
            record['unusable'] = type(exc).__name__
            return None
        # JSON-safe evidence; raw bytes remain observable rather than repr strings.
        require(isinstance(result, dict), 'decoder result mapping required')
        record['decoded'] = {k: v.hex() if isinstance(v, bytes) else v for k, v in result.items()}
        return result

    def expect(self, raw, *, first=None, repeated=None, blob=b'abc'):
        require(self.read(raw) == {'fixed': 0x1234, 'blob': blob,
                                   'first': first, 'repeated': repeated or []},
                'reader changed known fields, duplicate order or unknown-field policy')

    def unknown(self):
        for tag in range(2, 128):
            if tag == 3: continue
            self.expect(payload(field(tag, b'unknown') + field(1, b'\x07')), first=7)

    def critical(self):
        for tag in range(129, 256):
            # Includes high-bit aliases of both implemented tags and tag 127.
            self.expect(payload(field(tag, b'\x63') + field(1, b'\x07')), first=7)

    def duplicates(self):
        self.expect(payload(field(1, b'\x07') + field(1, b'\x09')), first=7)
        self.expect(payload(field(3, b'\x09') + field(1, b'\x07') + field(3, b'\x02')),
                    first=7, repeated=[9, 2])

    def broken(self):
        for raw in [payload()[:i] for i in range(7)]:
            require(self.read(raw) is None, 'short fixed or counted value accepted')
        for tail in (b'\x02', b'\x02\x00', b'\x02\x02\x00x',
                     field(1, b'\x07') + b'\x02\xff\xffx'):
            require(self.read(payload(tail)) is None, 'truncated TLV accepted')
        for tag in (0, 128):
            require(self.read(payload(field(tag, b''))) is None, 'zero TLV ID accepted')

    def known_width(self):
        for tag in (1, 3):
            for value in (b'', b'\x07\x08'):
                result = self.read(payload(field(tag, value)))
                # SPEC permits refusing the payload or not using the broken field.
                require(result is None or result == {'fixed': 0x1234, 'blob': b'abc',
                        'first': None, 'repeated': []}, 'broken known TLV value used')
        # core §2.3 (a09fa45): a later same-tag TLV is validated as well; a broken duplicate is never used.
        for tail, kept in ((field(1, b'\x07') + field(1, b'\x09\x08'), {'first': 7, 'repeated': []}),
                           (field(3, b'\x07') + field(3, b'\x09\x08'), {'first': None, 'repeated': [7]})):
            result = self.read(payload(tail))
            # Refusing the payload, or dropping the broken value (and possibly the whole field), never uses it.
            require(result is None or result['fixed'] == 0x1234 and result['blob'] == b'abc' and
                    result['first'] in (kept['first'], None) and result['repeated'] in (kept['repeated'], []),
                    'broken later duplicate TLV used')
        # core §2.3: the reader uses the first copy of a non-repeating tag; a broken first copy is not used, and a
        # later copy never stands in for it.
        result = self.read(payload(field(1, b'\x07\x08') + field(1, b'\x09')))
        require(result is None or result['fixed'] == 0x1234 and result['first'] is None,
                'a later copy used in place of a broken first one')

    def sizes(self):
        for blob in (b'', bytes(range(256)), b'x' * 65535):
            self.expect(payload(field(1, b'\x07'), blob), first=7, blob=blob)
        self.expect(payload(field(127, b'x' * 65535) + field(1, b'\x07')), first=7)

    def run(self):
        rows = []
        for name in ('unknown', 'critical', 'duplicates', 'broken', 'known_width', 'sizes'):
            start = len(self.observations)
            row = {'id': 'HOST-TLV-' + name.upper(), 'clause': 'core §2.2/2.3',
                   'status': 'passed'}
            try:
                getattr(self, name)()
            except Exception as exc:
                row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            row['observations'] = self.observations[start:]
            rows.append(row)
        return {'status': 'passed' if all(r['status'] == 'passed' for r in rows) else 'failed',
                'level': 'core', 'subject': 'Host example payload reader component',
                'spec': self.identity, 'full_conformance': False, 'checks': rows,
                'decoder': {'module': self.decoder.__module__, 'name': self.decoder.__qualname__},
                'adoption': {'fixed': 'u16 value, u16 blob length, blob, TLVs',
                             'tags': {'1': 'optional u8, first occurrence',
                                      '3': 'repeated u8 in wire order'}},
                'sources_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                   for name in ('conformance_host_tlv.py', 'conformance_host_tlv_sample.py')},
                'unchecked': ['other fixed shapes and known TLV definitions',
                              'Host response correlation and notification dispatch',
                              'all production readers', 'external decoder source provenance',
                              'transport frame bounds', 'Probe emission conformity']}
