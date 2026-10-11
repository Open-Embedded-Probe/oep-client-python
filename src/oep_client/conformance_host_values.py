"""Host unknown-value policy, with explicit reader and selected SPEC."""
import hashlib
from pathlib import Path
import struct
from .conformance import Violation, require, spec_identity


def result(resolution, detail, payload=b''):
    return struct.pack('<BHBB', 2, 7, resolution, detail) + payload


class HostValueChecks:
    def __init__(self, decoder, *, spec):
        self.decoder = decoder
        registry, self.identity = spec_identity(spec)
        require(registry['resolutions']['rejected'] == 0 and registry['resolutions']['completed'] == 1,
                'selected resolution meanings differ')
        require(registry['outcomes'] == {'success': 0, 'failed': 1, 'partial': 2},
                'selected outcome meanings differ')
        require(registry['status'] == {'ok': 0, 'wait': 1, 'line': 2, 'fault': 3, 'timeout': 4, 'state': 5}
                and registry['reject_reasons']['unavailable'] == 4, 'selected status/reason meanings differ')
        self.reasons = set(registry['reject_reasons'].values())
        self.observations = []

    def read(self, frame, shape, *, failure=False):
        record = {'frame_hex': frame.hex(), 'shape': shape}
        self.observations.append(record)
        try:
            decoded = self.decoder(frame, shape)
        except Violation:
            raise
        except ValueError as exc:
            record['unusable'] = f'{type(exc).__name__}: {exc}'
            require(failure, 'reader rejected a value it must preserve or ignore')
            return None
        require(isinstance(decoded, dict), 'reader mapping required')
        record['decoded'] = decoded
        return decoded

    def expect(self, frame, shape, expected, *, failure=False):
        decoded = self.read(frame, shape, failure=failure)
        if decoded is None:
            require(failure, 'unexpected unusable result')
        else:
            require(all(decoded.get(key) == value for key, value in expected.items()),
                    'unknown-value classification or fixed shape changed')

    def resolution(self):
        for value in range(2, 256):
            self.expect(result(value, 0, b'not a known payload'), 'debug', {'successful': False}, failure=True)

    def outcome(self):
        for value in range(3, 256):
            self.expect(result(1, value, b'not a known payload'), 'debug', {'successful': False}, failure=True)

    def status(self):
        for value in range(256):
            self.expect(result(1, 0, struct.pack('<HBII', 2, value, 11, 22)), 'debug',
                        {'successful': value == 0, 'done': 2, 'status': value, 'values': [11, 22]},
                        failure=value != 0)

    def reason(self):
        for value in sorted(set(range(256)) - self.reasons):
            self.expect(result(0, value), 'debug', {'successful': False}, failure=True)

    def cause(self):
        for value in range(256):
            self.expect(result(0, 4, bytes((1, 1, 0, value))), 'debug',
                        {'successful': False, 'reason': 4, 'cause': value})

    def scan_kind(self):
        for value in range(256):
            payload = struct.pack('<BBBHHI', 1, 1, value, 20, 21, 0x89abcdef)
            self.expect(result(1, 0, payload), 'scan',
                        {'successful': True, 'tried': 1, 'entries': [[value, 20, 21, 0x89abcdef]]})

    def flags(self):
        for value in range(256):
            payload = struct.pack('<4sBBHIBI', b'OEP!', 1, value, 1024, 4096, 4, 17) + b'\x01\x01\x00\x00'
            self.expect(result(1, 0, payload), 'confirm',
                        {'successful': True, 'revision': 1, 'maximum': 1024, 'window': 4096,
                         'inflight': 4, 'boot': 17})

    def run(self):
        rows = []
        for name in ('resolution', 'outcome', 'status', 'reason', 'cause', 'scan_kind', 'flags'):
            start = len(self.observations)
            row = {'id': 'HOST-VALUES-' + name.upper(), 'clause': 'core §2.4/7.1; debug §3/4',
                   'status': 'passed'}
            try: getattr(self, name)()
            except Exception as exc: row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            row['observations'] = self.observations[start:]
            rows.append(row)
        return {'status': 'passed' if all(r['status'] == 'passed' for r in rows) else 'failed',
                'full_conformance': False, 'spec': self.identity, 'checks': rows,
                'subject': 'Host reader for selected debug/scan/confirm/rejected shapes',
                'decoder': {'module': self.decoder.__module__, 'name': self.decoder.__qualname__},
                'sources_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                   for name in ('conformance_host_values.py', 'conformance_host_values_sample.py')},
                'unchecked': ['other status/reason/flags contexts', 'all payloads and TLV widths',
                              'production Host integration', 'external decoder source provenance',
                              'Probe emission conformity', 'target execution and transport recovery']}
