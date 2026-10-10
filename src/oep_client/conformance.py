"""Independent raw-message checks against an explicitly selected SPEC checkout.

No private equipment repository imports, target driving, settings or flash.
Force is tested only between sessions acquired by this runner.
Serial/TCP have independent CLI inspectors; raw USB adapters also have an independent API.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import time

from .hardware import equipment_lock, tomllib


CORE_CASES = (
    'CORE-CONFIRM', 'CORE-IDENTITY', 'CORE-DECLARE', 'CORE-LIST', 'CORE-CLOCK',
    'CORE-ZERO-CORR', 'CORE-ZERO-SESSION', 'CORE-SESSION-REQUIRED', 'CORE-TLV-ZERO',
    'CORE-KEEPALIVE-LOCK', 'CORE-LEASE-MIN', 'CORE-LEASE-MAX', 'CORE-OPEN-AFTER-END',
    'CORE-REPLAY', 'CORE-OPEN-HISTORY', 'CORE-ALTERED-REPLAY', 'CORE-CORR-U16',
    'CORE-CORR-MAX', 'CORE-END', 'CORE-LEASE', 'CORE-REPLAY-LEASE',
)

EXTRA_CHECKS = (
    ('CORE-REVISION-ORDER', 'core §7.1', 'invalid_request', 'revision-order'),
    ('CORE-CONFIRM-SHORT', 'core §7.1', 'invalid_request', 'confirm-short'),
    ('CORE-CONFIRM-MAGIC', 'core §7.1', 'invalid_request', 'confirm-magic'),
    ('CORE-TLV-HEADER', 'core §2.2/4.3', 'invalid_request', 'tlv-header'),
    ('CORE-TLV-VALUE', 'core §2.2/4.3', 'invalid_request', 'tlv-value'),
    ('CORE-TLV-CRITICAL-ZERO', 'core §2.2/4.3', 'invalid_request', 'tlv-critical-zero'),
    ('CORE-TLV-OPTIONAL', 'core §2.3', 'invalid_request', 'unknown-optional'),
    ('CORE-TLV-CRITICAL', 'core §2.3/4.3', 'invalid_request', 'unknown-critical'),
    ('CORE-ZERO-PRIORITY', 'core §4.3', 'invalid_request', 'zero-priority'),
    ('CORE-FN-PRIORITY', 'core §4.3', 'invalid_request', 'fn-priority'),
    ('CORE-OP-PRIORITY', 'core §4.3', 'invalid_request', 'op-priority'),
    ('CORE-SESSION-PRIORITY', 'core §4.3', 'invalid_request', 'session-priority'),
    ('CORE-SESSION-BEFORE-PAYLOAD', 'core §4.3/6.2', 'invalid_request', 'session-before-payload'),
    ('CORE-UNSEEN-OLD', 'core §5.2', 'unseen_old', None),
    ('CORE-REJECTED-REPLAY', 'core §5.2', 'replay_rejected', False),
    ('CORE-HEADER-BEFORE-REPLAY', 'core §4.3/5.2', 'replay_rejected', True),
    ('CORE-OWNER', 'core §2.3/6.4', 'owners', None),
    ('CORE-HEADER-LEASE', 'core §4.3/6.1', 'lease_rejections', True),
    ('CORE-REJECTED-LEASE', 'core §4.3/6.1', 'lease_rejections', False),
    ('CORE-FORCE-OWNED', 'core §6.2/6.4', 'force_owned', None),
    ('CORE-LIST-STABILITY', 'core §7.2', 'list_stability', None),
    ('CORE-LEASE-DEFAULT', 'core §6.4', 'lease_clamp', 0),
    ('CORE-TRANSPORT-DECLARE', 'core §7.5; transports §3', 'transport_declarations', None),
    ('CORE-DESCRIBE-SIZE', 'core §7.3', 'describe_size', None),
    ('CORE-DISCOVERY-LOCKED', 'core §6.3/7', 'discovery_locked', None),
    ('CORE-CONFIRM-HISTORY', 'core §5.2/7.1', 'confirm_history', None),
)

CORE_CASES += tuple(row[0] for row in EXTRA_CHECKS)


SERIAL_CASES = tuple(('CORE-SERIAL-' + kind.upper(), kind) for kind in
                     ('split', 'coalesce', 'crc', 'cobs', 'short', 'role', 'truncated', 'oversized'))


RECONNECT_CASES = tuple(('CORE-RECONNECT-' + kind.upper(), kind) for kind in
                        ('session', 'replay', 'ended', 'lease'))


TCP_CASES = tuple(('CORE-TCP-' + kind.upper(), kind) for kind in
                  ('split', 'coalesce', 'zero', 'short', 'role', 'partial-gap', 'oversized'))


TCP_PEER_CASES = tuple(('CORE-TCP-PEER-' + kind.upper(), kind) for kind in
                       ('identity', 'lock', 'route', 'partial', 'close', 'oversized'))


class NotApplicable(Exception):
    pass


class Violation(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Violation(message)


def tlvs(data):
    """Strict TLV decoding, independent of the runtime client's registry/parser."""
    rows = []
    while data:
        require(len(data) >= 3, 'truncated TLV header')
        tag, length = struct.unpack_from('<BH', data)
        require(tag & 0x7f != 0, 'TLV ID zero')
        require(tag <= 0x7f, 'critical bit on response TLV')
        require(len(data) >= length + 3, 'truncated TLV value')
        rows.append((tag & 0x7f, data[3:3 + length]))
        data = data[3 + length:]
    return rows


def ops(value):
    require(len(value) >= 2, 'ops must have base and nonempty bitmap')
    require(value[0] + (len(value) - 1) * 8 <= 256, 'ops extends beyond 0xFF')
    return {value[0] + i for i in range(8 * (len(value) - 1))
            if value[1 + i // 8] & (1 << (i % 8))}



CHECKER_SOURCES = ('conformance.py', 'conformance_serial.py', 'conformance_tcp.py',
                   'conformance_tcp_peers.py', 'conformance_usb.py', 'conformance_usb_cases.py',
                   'pytest_conformance.py', 'conformance_resources.py', 'conformance_resource_sample.py',
                   'conformance_subscriptions.py', 'conformance_subscription_sample.py',
                   'conformance_data.py', 'conformance_data_sample.py',
                   'conformance_routes.py', 'conformance_route_sample.py',
                   'conformance_pipeline.py', 'conformance_pipeline_sample.py',
                   'conformance_replay_pressure.py', 'conformance_lease.py', 'conformance_lease_sample.py',
                   'conformance_retention.py', 'conformance_retention_sample.py',
                   'conformance_retention_lifecycle.py', 'conformance_retention_lifecycle_sample.py')


def checker_sources_sha256():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in CHECKER_SOURCES}


def spec_identity(path):
    path = Path(path).resolve(strict=True)
    raw = (path / 'registry/oep-v1.toml').read_bytes()
    # Require a SPEC checkout; record dirty state as well as commit, never just revision 1.
    commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = bool(subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain'], text=True))
    return tomllib.loads(raw.decode()), {'commit': commit, 'dirty': dirty,
                                         'registry_sha256': hashlib.sha256(raw).hexdigest()}


class Checks:
    def __init__(self, send, registry, expected_unit):
        self.send = send
        self.reg = registry
        self.unit = expected_unit
        self.core = {x['name']: x['code'] for x in registry['core']['op']}
        self.reasons = registry['reject_reasons']
        self.zero_corr = 1000  # transport discovery has already drained its requests
        self.session = None
        self.corr = 0
        self.boot = None
        self.confirm_transport = None
        self.max_frame = 64
        self.observed = {}
        self.trace = []
        self.results = []
        self.abort = False
        self.wire = None
        self.reopen = None
        self.tcp_wire = None
        self.usb_wire = None
        self.tcp_peer_open = None
        self.tcp_reopen = None

    def request(self, op, payload=b'', *, session=0, corr=None, fn=0):
        if corr is None:
            if session:
                self.corr += 1
                corr = self.corr
            else:
                self.zero_corr += 1
                corr = self.zero_corr
        return struct.pack('<BHHBI', self.reg['roles']['request'], corr, fn, op, session) + payload

    def exchange(self, request):
        record = {'request_hex': request.hex(), 'started_monotonic_ns': time.monotonic_ns()}
        self.trace.append(record)
        wire_records = [(name, wire, wire.last_exchange) for name, wire in
                        (('serial_wire', self.wire), ('tcp_wire', self.tcp_wire), ('usb_wire', self.usb_wire))
                        if wire is not None]
        try:
            try:
                reply = self.send(request)
            except Exception:
                self.abort = True
                raise
            record['response_hex'] = reply.hex()
            if self.wire is not None:
                record['serial_wire'] = self.wire.last_exchange
            if self.tcp_wire is not None:
                record['tcp_wire'] = self.tcp_wire.last_exchange
            if self.usb_wire is not None:
                record['usb_wire'] = self.usb_wire.last_exchange
            require(5 <= len(reply) <= self.max_frame, 'result length outside negotiated bounds')
            role, corr, resolution, detail = struct.unpack_from('<BHBB', reply)
            require(role == self.reg['roles']['result'], 'unexpected result role')
            require(corr == struct.unpack_from('<H', request, 1)[0], 'wrong result corr')
            require(resolution in self.reg['resolutions'].values(), 'undefined resolution')
            if resolution == self.reg['resolutions']['completed']:
                require(detail in self.reg['outcomes'].values(), 'undefined completed outcome')
            else:
                require(detail in self.reasons.values() or 0x40 <= detail <= 0x7f,
                        'undefined rejected reason')
            if resolution == self.reg['resolutions']['rejected']:
                reason = next((k for k, v in self.reasons.items() if v == detail), None)
                if reason in {'unknown_function', 'unknown_operation', 'malformed', 'window_exceeded',
                              'no_session', 'session_required', 'no_resource', 'result_lost'}:
                    tlvs(reply[5:])
                elif reason == 'locked':
                    require(len(reply) >= 9 and int.from_bytes(reply[5:9], 'little') > 0, 'locked remaining time')
                    tlvs(reply[9:])
                elif reason == 'unsupported':
                    require(len(reply) >= 6, 'unsupported tag required')
                    tlvs(reply[6:])
                elif reason == 'unavailable':
                    tlvs(reply[5:])
            return reply
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            # Do not silently recover a failed transport and hide framing/timeout failures.
            if isinstance(exc, (TimeoutError, OSError)):
                self.abort = True
            raise
        finally:
            for name, wire, previous in wire_records:
                if wire.last_exchange is not previous:
                    record[name] = wire.last_exchange  # include bytes even when send/decode failed
            record['elapsed_ns'] = time.monotonic_ns() - record['started_monotonic_ns']

    def success(self, request):
        reply = self.exchange(request)
        require(reply[3:5] == bytes((self.reg['resolutions']['completed'], self.reg['outcomes']['success'])),
                f'expected completed success, got {reply.hex()}')
        return reply[5:]

    def rejected(self, request, reason):
        reply = self.exchange(request)
        require(reply[3:5] == bytes((self.reg['resolutions']['rejected'], self.reasons[reason])),
                f'expected rejected {reason}, got {reply.hex()}')
        return reply[5:]

    def check(self, name, clause, function):
        start = len(self.trace)
        row = {'id': name, 'clause': clause,
               'level': 'interface' if name.startswith('IF-') else 'core'}
        if self.abort:
            row.update(status='blocked', error='endpoint, transport or cleanup failure; remaining checks not executed')
        else:
            try:
                function()
                row['status'] = 'passed'
            except NotApplicable as exc:
                row.update(status='not_applicable', reason=str(exc))
            except Exception as exc:
                row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        row['exchanges'] = self.trace[start:]
        self.results.append(row)

    def confirm(self):
        p = self.success(self.request(self.core['confirm'], b'OEP?\x01\x01'))
        require(17 <= len(p) <= 59 and p[:4] == b'OEP!', 'confirm shape/magic')
        revision, flags, frame, window, inflight, boot = struct.unpack_from('<BBHIBI', p, 4)
        require(revision == 1 and flags == 0, 'confirm revision/flags')
        require(frame >= 64 and window >= frame and inflight >= 1, 'confirm limits')
        rows = tlvs(p[17:])
        tag = self.reg['core']['tlv']['confirm_answer']['transport']
        values = [v for t, v in rows if t == tag]
        require(values and len(values[0]) == 1, 'confirm transport required, u8')
        self.boot, self.max_frame, self.confirm_transport = boot, frame, values[0][0]
        if self.wire is not None:
            self.wire.max_frame = frame
        if self.tcp_wire is not None:
            self.tcp_wire.max_frame = frame
        if self.usb_wire is not None:
            self.usb_wire.max_frame = frame
        self.observed['confirm'] = dict(revision=revision, max_frame=frame, window=window,
                                        max_inflight=inflight, boot_id=boot, transport=self.confirm_transport)

    def describe(self, fn):
        rows, first = [], 0
        for _ in range(65536):
            p = self.success(self.request(self.core['describe'], struct.pack('<HH', fn, first)))
            require(p and p[0] in (0, 1), 'describe more flag')
            page = tlvs(p[1:])
            require(not p[0] or page, 'describe page makes no progress')
            rows += page
            first += len(page)
            require(first <= 65535, 'describe cursor overflow')
            if p[0] == 0:
                break
        else:
            raise Violation('describe page limit')
        end = self.success(self.request(self.core['describe'], struct.pack('<HH', fn, 65535)))
        require(end == b'\0', 'describe past end must have no TLVs')
        return rows

    def identity(self):
        rows = self.describe(0)
        self.observed['core_describe'] = [{'tag': t, 'value_hex': v.hex()} for t, v in rows]
        tags = self.reg['core']['tlv']['describe']
        values = dict(reversed(rows))  # first non-repeating TLV wins
        unit = values.get(tags['unit_id'], b'').decode('ascii')
        require(unit.lower() == self.unit.lower(), 'unit_id differs from explicit equipment selection')
        require(1 <= len(unit) <= 32 and all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in unit),
                'unit_id format')
        for field in ('unit_id', 'firmware', 'model', 'chip'):
            self.observed[field] = values.get(tags[field], b'').decode('utf-8') or None

    def declarations(self):
        rows = [(x['tag'], bytes.fromhex(x['value_hex'])) for x in self.observed['core_describe']]
        values = dict(reversed(rows))
        tag = self.reg['describe_common']['ops']
        require(tag in values, 'core describe missing current ops tag')
        declared = ops(values[tag])
        require(set(self.core.values()) <= declared, 'mandatory core operation absent')
        self.transport_declarations()
        channels = values.get(self.reg['core']['tlv']['describe']['channels'], b'\0\0')
        require(len(channels) == 2, 'channels must be u16')
        require(rows == self.describe(0), 'core describe changed within same boot')

    def transport_declarations(self):
        rows = [(x['tag'], bytes.fromhex(x['value_hex'])) for x in self.observed['core_describe']]
        values = dict(reversed(rows))
        tags = self.reg['core']['tlv']['describe']
        maximum = values.get(tags['max_op_ms'], b'')
        require(len(maximum) == 4 and 1 <= int.from_bytes(maximum, 'little') <= 600000, 'max_op_ms')
        transports = [v for t, v in rows if t == tags['transport']]
        require(transports and all(len(v) == 3 and 1 <= v[1] <= 6 for v in transports), 'transport declaration')
        require(len({v[0] for v in transports}) == len(transports), 'duplicate transport index')
        require(self.confirm_transport in {v[0] for v in transports}, 'confirm transport has no describe entry')
        require(all(v[2] == 255 for v in transports if v[1] in (1, 6)),
                'UART/TCP transport interface must be 0xFF')
        selected = next(v for v in transports if v[0] == self.confirm_transport)
        if self.wire is not None:
            require(selected[1] in (1, 2, 3), 'serial endpoint declares a non-serial kind')
        if self.tcp_wire is not None:
            require(selected[1] == 6, 'TCP endpoint declares a non-TCP kind')
        if self.usb_wire is not None:
            require(selected[1] == (4 if self.usb_wire.kind == 'bulk' else 5),
                    'USB endpoint declares a different transport kind')

    def describe_size(self):
        rows = [(x['tag'], bytes.fromhex(x['value_hex'])) for x in self.observed['core_describe']]
        require(all(len(v) + 9 <= self.max_frame for _, v in rows), 'core describe TLV exceeds frame limit')
        for entry in self.observed.get('interfaces', []):
            for _, value in self.describe(entry['fn']):
                require(len(value) + 9 <= self.max_frame, 'interface describe TLV exceeds frame limit')

    def discovery_locked(self):
        with self.holding(60000) as sid:
            before = self.observed['confirm'].copy()
            self.confirm()
            require(before == self.observed['confirm'], 'confirm changed while locked')
            self.identity()
            require(self.list_snapshot() == [{k: v for k, v in e.items() if k != 'describe'}
                                             for e in self.observed.get('interfaces', [])],
                    'list changed while locked')
            tlvs(self.success(self.request(self.core['keepalive'], session=sid)))

    def confirm_history(self):
        with self.holding() as sid:
            request, original = self.clock(sid)
            before = self.observed['confirm'].copy()
            self.confirm()
            require(before == self.observed['confirm'], 'repeat confirm changed limits or boot')
            reply = self.exchange(request)
            require(reply[3:5] == bytes((0, self.reasons['result_lost'])) or
                    (reply[3:5] == b'\x01\0' and reply[5:] == original),
                    'confirm discarded replay history or repeated execution')

    def reconnect(self, kind):
        lease = 1000 if kind == 'lease' else 60000
        with self.holding(lease) as sid:
            request, original = self.clock(sid)
            if kind == 'ended':
                tlvs(self.success(self.request(self.core['end'], session=sid)))
                self.session = None
            before = self.observed['confirm'].copy()
            if kind == 'lease':
                time.sleep(0.6)
            record = {'reconnect': kind, 'started_monotonic_ns': time.monotonic_ns()}
            self.trace.append(record)
            try:
                self.reopen()
                # Stop immediately on restart or replacement. Never end a session on a
                # different endpoint, or mistake a hardware reset for transport closure.
                try:
                    self.confirm()
                    require(self.boot == before['boot_id'],
                            'probe restarted while reopening; retention not established')
                    self.identity()
                except Exception:
                    self.session = None
                    self.abort = True
                    raise
                require(before == self.observed['confirm'], 'reopen changed confirm limits/transport')
                if kind == 'session':
                    state = self.success(self.request(self.core['lock_state']))
                    require(len(state) >= 5 and state[0] == 1, 'close released session lock')
                    tlvs(self.success(self.request(self.core['keepalive'], session=sid)))
                elif kind == 'replay':
                    reply = self.exchange(request)
                    require(reply[3:5] == bytes((0, self.reasons['result_lost'])) or
                            (reply[3:5] == b'\x01\0' and reply[5:] == original),
                            'reconnect lost replay history or repeated execution')
                elif kind == 'ended':
                    opened = self.request(self.core['open'], struct.pack('<IB', 3000, 0), session=sid)
                    reply = self.exchange(opened)
                    if reply[3:5] == b'\x01\0':
                        self.session = sid  # clean up buggy resurrection
                    require(reply[3:5] == bytes((0, self.reasons['no_session'])),
                            'reconnect forgot ended session and allowed reopening')
                elif kind == 'lease':
                    time.sleep(0.6)
                    state = self.success(self.request(self.core['lock_state']))
                    require(len(state) >= 5 and struct.unpack_from('<BI', state) == (0, 0),
                            'reconnect renewed or stopped lease')
                    self.session = None
                    self.rejected(self.request(self.core['keepalive'], session=sid), 'no_session')
                else:
                    raise ValueError(kind)
            except (OSError, TimeoutError):
                self.abort = True
                raise
            finally:
                record['elapsed_ns'] = time.monotonic_ns() - record['started_monotonic_ns']

    def list_snapshot(self):
        first, total, entries = 0, None, []
        while True:
            p = self.success(self.request(self.core['list'], struct.pack('<H', first)))
            require(len(p) >= 3, 'list fixed header')
            size, count = struct.unpack_from('<HB', p)
            require(total is None or size == total, 'list total changed')
            total = size
            at = 3
            for _ in range(count):
                require(len(p) >= at + 7, 'list truncated entry')
                fn, instance, revision, flags, length = struct.unpack_from('<HHBBB', p, at)
                at += 7
                require(fn and 1 <= length <= 48 and len(p) >= at + length, 'list fn/name length')
                require(revision >= 1 and flags == 0, 'interface revision/flags')
                name = p[at:at + length].decode('ascii')
                require(all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-.' for c in name), 'interface name format')
                entries.append(dict(fn=fn, instance=instance, revision=revision, name=name))
                at += length
            require(at == len(p) and first + count <= total, 'list trailing bytes/count')
            first += count
            if first == total:
                break
            require(count, 'list makes no progress')
        require(len({x['fn'] for x in entries}) == len(entries), 'duplicate fn')
        end = self.success(self.request(self.core['list'], struct.pack('<H', 65535)))
        require(end == struct.pack('<HB', total, 0), 'list past end')
        return entries

    def interfaces(self):
        entries = self.list_snapshot()
        self.observed['interfaces'] = entries
        for entry in entries:
            self.check('IF-DESCRIBE-' + str(entry['fn']), 'core §1.2/7.2/7.3/7.4',
                       lambda entry=entry: self.interface(entry, entries))

    def list_stability(self):
        expected = [{key: value for key, value in row.items() if key != 'describe'}
                    for row in self.observed.get('interfaces', [])]
        require(self.list_snapshot() == expected, 'interface list changed within one boot')

    def interface(self, entry, entries):
        rows = self.describe(entry['fn'])
        values = dict(reversed(rows))
        entry['describe'] = [{'tag': t, 'value_hex': v.hex()} for t, v in rows]
        require(self.reg['describe_common']['ops'] in values, f'{entry["name"]}: missing ops')
        declared = ops(values[self.reg['describe_common']['ops']])
        require(0 not in declared and not declared.intersection(range(3, 16)),
                'unassigned common operation declared')
        require((1 in declared) == (2 in declared), 'subscribe/unsubscribe must be declared together')
        require(rows == self.describe(entry['fn']), f'{entry["name"]}: unstable describe')
        peers = sorted((x for x in entries if (x['name'], x['revision']) == (entry['name'], entry['revision'])),
                       key=lambda x: x['fn'])
        require(entry['instance'] == peers.index(entry), 'interface instance numbering')

    @contextmanager
    def holding(self, lease=3000, tail=b''):
        sid = secrets.randbelow(0xffffffff) + 1
        self.corr = 0
        p = self.success(self.request(self.core['open'], struct.pack('<IB', lease, 0) + tail, session=sid))
        self.session = sid  # cleanup even if the successful open payload is malformed
        try:
            require(len(p) >= 8, 'open payload length')
            actual, boot = struct.unpack_from('<II', p)
            tlvs(p[8:])
            require((1000 <= actual <= 60000) if lease == 0 else
                    actual == min(60000, max(1000, lease)), 'open lease clamp')
            require(boot == self.boot, 'open boot differs from confirm')
            yield sid
        finally:
            try:
                if self.session is not None:
                    p = self.success(self.request(self.core['end'], session=self.session))
                    tlvs(p)
                    self.session = None
                    state = self.success(self.request(self.core['lock_state']))
                    require(len(state) >= 5 and struct.unpack_from('<BI', state) == (0, 0),
                            'cleanup end left session locked')
            except Exception:
                self.abort = True
                raise

    def clock(self, sid=0):
        request = self.request(self.core['clock'], session=sid)
        p = self.success(request)
        require(len(p) >= 12 and struct.unpack_from('<I', p)[0] == self.boot, 'clock boot/fixed payload')
        tlvs(p[12:])
        return request, p

    def replay(self, across_open=False):
        with self.holding() as sid:
            request, p = self.clock(sid)
            if across_open:
                self.success(self.request(self.core['open'], struct.pack('<IB', 3000, 0), session=sid))
            time.sleep(0.01)
            reply = self.exchange(request)
            lost = bytes((self.reg['resolutions']['rejected'], self.reasons['result_lost']))
            require(reply[3:5] == lost or (reply[3:5] == b'\x01\0' and reply[5:] == p),
                    'replayed clock executed again or history discarded')

    def altered(self):
        with self.holding() as sid:
            request, _ = self.clock(sid)
            changed = bytearray(request)
            changed[5] = self.core['keepalive']
            reply = self.exchange(bytes(changed))
            require(reply[3] == self.reg['resolutions']['rejected'] and
                    reply[4] in (self.reasons['malformed'], self.reasons['result_lost']),
                    'same corr with different request accepted')

    def highwater(self):
        with self.holding() as sid:
            self.corr = 65529
            self.success(self.request(self.core['keepalive'], session=sid))
            # session zero has its own independent corr space, including a half-range jump.
            self.zero_corr = max(self.zero_corr, 39999)
            self.clock()
            self.success(self.request(self.core['end'], session=sid))
            self.session = None
            self.must_not_reopen(sid)

    def must_not_reopen(self, sid):
        reply = self.exchange(self.request(self.core['open'], struct.pack('<IB', 3000, 0), session=sid))
        if reply[3:5] == b'\x01\0':
            self.session = sid  # buggy reopen must also be cleaned up
        require(reply[3:5] == bytes((0, self.reasons['no_session'])), 'ended session reopened')

    def corr_maximum(self):
        with self.holding() as sid:
            self.corr = 65533
            tlvs(self.success(self.request(self.core['keepalive'], session=sid)))
            tlvs(self.success(self.request(self.core['end'], session=sid)))
            self.session = None  # the end used corr 65535; do not wrap this session

    def ended(self):
        with self.holding() as sid:
            end = self.request(self.core['end'], session=sid)
            original = self.success(end)
            tlvs(original)
            self.session = None
            reply = self.exchange(end)
            require((reply[3:5] == b'\x01\0' and reply[5:] == original) or reply[3:5] == bytes((0, self.reasons['result_lost'])),
                    'end replay changed')
            self.rejected(self.request(self.core['keepalive'], session=sid), 'no_session')
            self.must_not_reopen(sid)

    def lease_expiry(self, replay=False):
        with self.holding(1000) as sid:
            request, _ = self.clock(sid)
            if replay:
                # Duplicates must not renew the lease; fresh session-zero reads must not either.
                for _ in range(3):
                    time.sleep(0.4)
                    self.exchange(request)
                    self.clock()
            else:
                time.sleep(1.2)
            p = self.success(self.request(self.core['lock_state']))
            require(len(p) >= 5 and struct.unpack_from('<BI', p) == (0, 0), 'lease not expired')
            self.session = None
            self.rejected(self.request(self.core['keepalive'], session=sid), 'no_session')

    def clock_progress(self):
        _, a = self.clock()
        time.sleep(0.01)
        _, b = self.clock()
        require(int.from_bytes(b[4:12], 'little') >= int.from_bytes(a[4:12], 'little'),
                'uptime decreased within one boot')

    def keepalive(self):
        with self.holding() as sid:
            tlvs(self.success(self.request(self.core['keepalive'], session=sid)))
            p = self.success(self.request(self.core['lock_state']))
            require(len(p) >= 5, 'lock_state fixed payload')
            locked, remaining = struct.unpack_from('<BI', p)
            require(locked == 1 and 0 < remaining <= 3000, 'lock_state not held')
            tlvs(p[5:])
            other = secrets.randbelow(0xffffffff) + 1
            while other == sid:
                other = secrets.randbelow(0xffffffff) + 1
            self.rejected(self.request(self.core['open'], struct.pack('<IB', 3000, 0),
                                       session=other, corr=1), 'locked')

    def zero_tlv(self):
        self.rejected(self.request(self.core['confirm'], b'OEP?\x01\x01\x00\x00\x00'), 'malformed')

    def replayed_open(self):
        with self.holding() as sid:
            request = self.request(self.core['open'], struct.pack('<IB', 3000, 0), session=sid)
            p = self.success(request)
            self.success(self.request(self.core['end'], session=sid))
            self.session = None
            reply = self.exchange(request)
            require((reply[3:5] == b'\x01\0' and reply[5:] == p) or
                    reply[3:5] == bytes((0, self.reasons['result_lost'])), 'open replay result changed')
            state = self.success(self.request(self.core['lock_state']))
            if len(state) >= 5 and state[0]:
                self.session = sid  # clean up a buggy resurrection before reporting failure
            require(len(state) >= 5 and struct.unpack_from('<BI', state) == (0, 0),
                    'replayed open resurrected ended session')

    def lease_clamp(self, lease):
        with self.holding(lease):
            pass

    def absent_fn(self):
        present = {entry['fn'] for entry in self.observed.get('interfaces', [])} | {0}
        return next(fn for fn in range(65535, 0, -1) if fn not in present)

    def invalid_request(self, mode):
        if mode == 'revision-order':
            self.rejected(self.request(self.core['confirm'], b'OEP?\x02\x01'), 'malformed')
        elif mode == 'confirm-short':
            self.rejected(self.request(self.core['confirm'], b'OEP?\x01'), 'malformed')
        elif mode == 'confirm-magic':
            self.rejected(self.request(self.core['confirm'], b'BAD?\x01\x01'), 'malformed')
        elif mode == 'tlv-header':
            self.rejected(self.request(self.core['confirm'], b'OEP?\x01\x01\x7f\x00'), 'malformed')
        elif mode == 'tlv-value':
            self.rejected(self.request(self.core['confirm'], b'OEP?\x01\x01\x7f\x02\x00x'), 'malformed')
        elif mode == 'tlv-critical-zero':
            self.rejected(self.request(self.core['confirm'], b'OEP?\x01\x01\x80\x00\x00'), 'malformed')
        elif mode == 'unknown-optional':
            p = self.success(self.request(self.core['confirm'], b'OEP?\x01\x01\x7f\x01\x00x'))
            require(len(p) >= 17 and p[:5] == b'OEP!\x01', 'ignored TLV broke confirm')
        elif mode == 'unknown-critical':
            p = self.rejected(self.request(self.core['confirm'], b'OEP?\x01\x01\xff\x01\x00x'), 'unsupported')
            require(p and p[0] == 255, 'unsupported did not echo critical tag')
        elif mode == 'zero-priority':
            self.rejected(self.request(255, b'x', fn=self.absent_fn(), session=0xffffffff, corr=0), 'malformed')
        elif mode == 'fn-priority':
            self.rejected(self.request(255, b'x', fn=self.absent_fn(), session=0xffffffff), 'unknown_function')
        elif mode == 'op-priority':
            self.rejected(self.request(0, b'x', session=0xffffffff), 'unknown_operation')
        elif mode == 'session-priority':
            self.rejected(self.request(self.core['keepalive'], b'x'), 'session_required')
        elif mode == 'session-before-payload':
            self.rejected(self.request(self.core['keepalive'], b'x', session=secrets.randbelow(0xffffffff)+1), 'no_session')
        else:
            raise ValueError(mode)

    def unseen_old(self):
        with self.holding() as sid:
            self.corr = 100
            self.clock(sid)
            self.rejected(self.request(self.core['clock'], session=sid, corr=2), 'result_lost')

    def replay_rejected(self, header=False):
        with self.holding() as sid:
            req = self.request(self.core['keepalive'], b'\x7f\x01\x00', session=sid)
            self.rejected(req, 'malformed')
            reply = self.exchange(req)
            require(reply[3:5] in (bytes((0, self.reasons['malformed'])), bytes((0, self.reasons['result_lost']))),
                    'rejected result replay changed')
            if header:
                changed = bytearray(req)
                changed[5] = 0  # even a cached corr must pass header validation first
                self.rejected(bytes(changed), 'unknown_operation')
            else:
                changed = req[:10]  # fixing payload must not execute under the same corr
                reply = self.exchange(changed)
                require(reply[3:5] in (bytes((0, self.reasons['malformed'])), bytes((0, self.reasons['result_lost']))),
                        'corrected rejected request reused corr')

    def owners(self):
        owner = b'conformance-first'
        tail = b'\x01' + struct.pack('<H', len(owner)) + owner
        tail += b'\x81\x06\x00second'  # same tag, critical flag, first occurrence wins
        with self.holding(tail=tail) as sid:
            def observed_owner():
                p = self.success(self.request(self.core['lock_state']))
                require(len(p) >= 5 and p[0] == 1, 'owner lock state')
                values = dict(reversed(tlvs(p[5:])))
                require(values.get(1) == owner, 'owner changed or first duplicate not used')
            observed_owner()
            self.success(self.request(self.core['open'], struct.pack('<IB', 3000, 0) + b'\x01\x06\x00second', session=sid))
            observed_owner()
            tlvs(self.success(self.request(self.core['end'], session=sid)))
            self.session = None
            state = self.success(self.request(self.core['lock_state']))
            if len(state) >= 5 and state[0]:
                self.session = sid
            require(len(state) >= 5 and struct.unpack_from('<BI', state) == (0, 0), 'owner end left lock held')
            require(1 not in dict(tlvs(state[5:])), 'ended session retained owner')

    def lease_rejections(self, header):
        with self.holding(1000) as sid:
            for _ in range(3):
                time.sleep(0.4)
                if header:
                    self.rejected(self.request(0, session=sid), 'unknown_operation')
                else:
                    self.rejected(self.request(self.core['keepalive'], b'\x7f\x01\x00', session=sid), 'malformed')
            p = self.success(self.request(self.core['lock_state']))
            require(len(p) >= 5, 'lock_state shape')
            if header:
                if p[0] == 0:
                    self.session = None
                require(struct.unpack_from('<BI', p) == (0, 0), 'header refusal renewed lease')
            else:
                require(p[0] == 1 and int.from_bytes(p[1:5], 'little') > 0, 'payload refusal did not renew lease')

    def force_owned(self):
        # Force only a session acquired by this runner while holding the shared equipment lock.
        with self.holding() as old:
            new = secrets.randbelow(0xffffffff)+1
            while new == old:
                new = secrets.randbelow(0xffffffff)+1
            p = self.success(self.request(self.core['open'], struct.pack('<IB', 3000, 1), session=new, corr=1))
            self.session, self.corr = new, 1
            require(len(p) >= 8 and struct.unpack_from('<II', p) == (3000, self.boot), 'force open shape')
            tlvs(p[8:])
            self.rejected(self.request(self.core['keepalive'], session=old, corr=2), 'locked')
            tlvs(self.success(self.request(self.core['keepalive'], session=new)))

    def serial_case(self, kind):
        from .conformance_serial import frame, WireError
        with self.holding(60000):
            req = self.request(self.core['clock'])
            good = frame(req)
            pause = 0
            if kind == 'split':
                chunks = [bytes((byte,)) for byte in good]
            elif kind == 'coalesce':
                bad = bytes((3,)) + req[1:]
                chunks = [frame(bad) + good]
            elif kind == 'crc':
                chunks = [frame(self.request(self.core['clock']), corrupt_crc=True) + good]
            elif kind == 'cobs':
                chunks = [b'\0\x05\x01\0' + good]
            elif kind == 'short':
                chunks = [frame(req[:9]) + good]
            elif kind == 'role':
                chunks = [frame(bytes((0,)) + req[1:]) + good]
            elif kind == 'truncated':
                chunks, pause = [good[:4], good], 0.35
            elif kind == 'oversized':
                chunks = [frame(req + b'x' * (self.max_frame + 1 - len(req)))]
            else:
                raise ValueError(kind)
            record = {'serial_stimulus': kind}
            self.trace.append(record)
            try:
                if kind == 'oversized':
                    # Oversize discards input through a frame gap: never append the recovery
                    # request immediately and demand that it be accepted.
                    gap = self.reg.get('timing', {}).get('probe_frame_gap_ms', 200) / 1000
                    discarded = self.wire.exchange(chunks, 0, silence=gap + 0.1)
                    record['oversize_wire'] = self.wire.last_exchange
                    record['oversize_results_hex'] = [r.hex() for r in discarded]
                    require(not discarded, 'oversized request produced a response')
                    chunks = [good]
                replies = self.wire.exchange(chunks, 1, pause=pause)
                record['responses_hex'] = [r.hex() for r in replies]
                if kind == 'split':
                    writes = self.wire.last_exchange['writes']
                    gap_ms = self.reg.get('timing', {}).get('probe_frame_gap_ms', 200)
                    require(all((b['monotonic_ns'] - a['monotonic_ns']) < gap_ms * 1000000
                                for a, b in zip(writes, writes[1:])), 'tester exceeded frame gap while splitting')
                require(len(replies) == 1, 'ignored/corrupt input produced a response or duplicate')
                original_send = self.send
                try:
                    self.send = lambda _: replies[0]
                    p = self.success(req)
                    require(len(p) >= 12 and int.from_bytes(p[:4], 'little') == self.boot, 'serial clock result')
                    tlvs(p[12:])
                finally:
                    self.send = original_send
            except (TimeoutError, OSError, WireError):
                self.abort = True
                raise
            finally:
                record['serial_wire'] = self.wire.last_exchange

    def tcp_case(self, kind):
        from .conformance_tcp import frame
        from .conformance_serial import WireError
        req = self.request(self.core['clock'])  # no session or target mutations
        good = frame(req)
        pause, close = 0, False
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
        elif kind == 'partial-gap':
            gap = self.reg.get('timing', {}).get('probe_frame_gap_ms', 200) / 1000
            chunks, pause = [good[:1], good[1:]], gap + 0.1
        elif kind == 'oversized':
            if self.max_frame == 65535:
                raise NotApplicable('u16 length cannot express max_frame + 1')
            chunks, close = [(self.max_frame + 1).to_bytes(2, 'little')], True
        else:
            raise ValueError(kind)
        record = {'tcp_stimulus': kind}
        self.trace.append(record)
        try:
            replies = self.tcp_wire.exchange(chunks, 0 if close else 1, pause=pause, expect_close=close)
            record['responses_hex'] = [reply.hex() for reply in replies]
            if close:
                require(not replies, 'oversized TCP frame produced a response')
                return  # deliberately closed; all request-bearing tests precede this case
            require(len(replies) == 1, 'ignored input produced a response or duplicate')
            original_send = self.send
            try:
                self.send = lambda _: replies[0]
                payload = self.success(req)
                require(len(payload) >= 12 and int.from_bytes(payload[:4], 'little') == self.boot,
                        'TCP clock result')
                tlvs(payload[12:])
            finally:
                self.send = original_send
        except (TimeoutError, OSError, WireError):
            self.abort = True
            raise
        finally:
            record['tcp_wire'] = self.tcp_wire.last_exchange

    def run(self):
        self.check('CORE-CONFIRM', 'core §7.1', self.confirm)
        if self.results[-1]['status'] != 'passed':
            self.abort = True  # confirm-only discovery: never describe a failed handshake
        self.check('CORE-IDENTITY', 'core §7.3/7.5', self.identity)
        if self.results[-1]['status'] != 'passed' or self.results[0]['status'] != 'passed':
            self.abort = True  # no session mutations on an unidentified endpoint
        self.check('CORE-DECLARE', 'core §1.2/7.3/7.4/7.5', self.declarations)
        self.check('CORE-LIST', 'core §7.2/7.3/7.4', self.interfaces)
        self.check('CORE-CLOCK', 'core §7.7', self.clock_progress)
        self.check('CORE-ZERO-CORR', 'core §4.1/5.2', lambda: self.rejected(
            self.request(self.core['clock'], corr=0), 'malformed'))
        self.check('CORE-ZERO-SESSION', 'core §4.1/6.1', lambda: self.rejected(
            self.request(self.core['open'], struct.pack('<IB', 3000, 0)), 'malformed'))
        self.check('CORE-SESSION-REQUIRED', 'core §4.1', lambda: self.rejected(
            self.request(self.core['keepalive']), 'session_required'))
        self.check('CORE-TLV-ZERO', 'core §2.2', self.zero_tlv)
        self.check('CORE-KEEPALIVE-LOCK', 'core §6.1/6.2/6.4', self.keepalive)
        self.check('CORE-LEASE-MIN', 'core §6.4', lambda: self.lease_clamp(1))
        self.check('CORE-LEASE-MAX', 'core §6.4', lambda: self.lease_clamp(60001))
        self.check('CORE-OPEN-AFTER-END', 'core §5.2/6.2', self.replayed_open)
        self.check('CORE-REPLAY', 'core §5.2', self.replay)
        self.check('CORE-OPEN-HISTORY', 'core §5.2/6.2', lambda: self.replay(True))
        self.check('CORE-ALTERED-REPLAY', 'core §5.2', self.altered)
        self.check('CORE-CORR-U16', 'core §4.1/5.2/6.2', self.highwater)
        self.check('CORE-CORR-MAX', 'core §4.1/5.2', self.corr_maximum)
        self.check('CORE-END', 'core §5.2/6.2', self.ended)
        self.check('CORE-LEASE', 'core §6.1', self.lease_expiry)
        self.check('CORE-REPLAY-LEASE', 'core §5.2/6.1', lambda: self.lease_expiry(True))
        for name, clause, method, argument in EXTRA_CHECKS:
            self.check(name, clause, lambda method=method, argument=argument:
                       getattr(self, method)(argument) if argument is not None else getattr(self, method)())
        if self.wire is not None:
            for name, kind in SERIAL_CASES:
                self.check(name, 'transports §1/2; core §2.4', lambda kind=kind: self.serial_case(kind))
        if self.reopen is not None:
            for name, kind in RECONNECT_CASES:
                self.check(name, 'transports §3; core §5.2/6.1/9', lambda kind=kind: self.reconnect(kind))
        if self.usb_wire is not None:
            from .conformance_usb_cases import USB_CASES, HID_CASES, run
            cases = USB_CASES + (HID_CASES if self.usb_wire.kind == 'hid' else ())
            for name, kind in cases:
                self.check(name, 'transports §1/2/3; core §2.4', lambda kind=kind: run(self, kind))
        if self.tcp_peer_open is not None:
            from .conformance_tcp_peers import Peers
            peers = Peers(self)
            for name, kind in TCP_PEER_CASES:
                self.check(name, 'transports §1/3; core §4.2/4.4/6', lambda kind=kind: getattr(peers, kind)())
        if self.tcp_wire is not None:
            for name, kind in TCP_CASES:
                self.check(name, 'transports §1/2; core §2.4', lambda kind=kind: self.tcp_case(kind))
        return {'status': 'passed' if all(x['status'] in ('passed', 'not_applicable') for x in self.results) else 'failed',
                'scope': 'selected core and common interface checks on selected transport; no target operations',
                'full_conformance': False,
                'levels': {'core': 'partial coverage', 'interface': 'declarations only',
                           'oep-interface': 'not executed'},
                'framing_backend': 'independent USB ' + self.usb_wire.kind if self.usb_wire is not None else
                                   'independent serial' if self.wire is not None else
                                   'independent TCP' if self.tcp_wire is not None else 'client',
                'unchecked': ['USB descriptors, packet termination and physical USB behavior',
                              'alternate transport types and mixed-route isolation',
                              'TCP same-session migration/replay on a new connection',
                              'target resource lifetime', 'multi-target isolation',
                              'interface operation behavior', 'electrical behavior'] +
                             ([] if self.wire is not None else ['independent serial framing/faults']) +
                             ([] if self.reopen is not None else ['reconnect/session retention']) +
                             ([] if self.tcp_wire is not None else ['independent TCP framing/faults']) +
                             ([] if self.tcp_peer_open is not None else ['two-connection TCP isolation and close retention']) +
                             ([] if self.usb_wire is not None and self.usb_wire.kind == 'bulk' else ['independent bulk framing/faults']) +
                             ([] if self.usb_wire is not None and self.usb_wire.kind == 'hid' else ['independent HID framing/faults']),
                'observed': self.observed, 'checks': self.results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name, env in [('address', 'OEP_CONFORMANCE_ADDRESS'), ('unit', 'OEP_CONFORMANCE_UNIT_ID'),
                      ('spec', 'OEP_CONFORMANCE_SPEC'), ('out', 'OEP_CONFORMANCE_OUT'), ('lock', 'OEP_HW_LOCK')]:
        parser.add_argument('--' + name, default=os.environ.get(env))
    parser.add_argument('--framing', choices=('client', 'serial', 'tcp'),
                        default=os.environ.get('OEP_CONFORMANCE_FRAMING', 'client'))
    parser.add_argument('--tcp-peer', default=os.environ.get('OEP_CONFORMANCE_TCP_PEER'))
    args = parser.parse_args(argv)
    if not all(getattr(args, name) for name in ('address', 'unit', 'spec', 'out', 'lock')):
        parser.error('explicit address, unit, SPEC checkout, new output file and existing shared lock required')
    if args.tcp_peer and args.framing != 'tcp':
        parser.error('explicit TCP peer requires framing=tcp')
    registry, spec = spec_identity(args.spec)
    from . import __version__, link
    report = {'status': 'failed', 'spec': spec, 'client_version': __version__,
              'checker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'checker_sources_sha256': checker_sources_sha256(),
              'address': args.address, 'tcp_peer_address': args.tcp_peer, 'expected_unit': args.unit,
              'started_at': datetime.now(timezone.utc).isoformat()}
    # Reserve a new artifact before any device operation. Never overwrite evidence.
    with Path(args.out).open('x') as artifact:
        try:
            with equipment_lock(args.lock):
                hst = None
                try:
                    if args.framing == 'serial':
                        if args.address.startswith(('usb:', 'tcp:', 'tcp://')):
                            raise ValueError('serial framing needs an explicit serial port')
                        from .conformance_serial import SerialWire
                        stream = link.open_serial(args.address)
                        try:
                            wire = SerialWire(stream)
                            checks = Checks(wire.send, registry, args.unit)
                            checks.wire = wire
                            def reopen():
                                link._exclusive_off(wire.stream)
                                wire.stream.close()
                                wire.stream = link.open_serial(args.address)
                            checks.reopen = reopen
                            report.update(checks.run())
                        finally:
                            link._exclusive_off(wire.stream)
                            wire.stream.close()
                    elif args.framing == 'tcp':
                        from .conformance_tcp import TcpWire
                        wire = TcpWire.open(args.address)
                        try:
                            checks = Checks(wire.send, registry, args.unit)
                            checks.tcp_wire = wire
                            if args.tcp_peer:
                                checks.tcp_peer_open = lambda: TcpWire.open(args.tcp_peer)
                                def reopen_tcp():
                                    fresh = TcpWire.open(args.address)
                                    wire.stream = fresh.stream
                                checks.tcp_reopen = reopen_tcp
                            report.update(checks.run())
                        finally:
                            wire.close()
                    else:
                        hst = link.open_host(args.address, keep_session=False, resend=False, port_speed=None)
                        report.update(Checks(hst.link.send, registry, args.unit).run())
                finally:
                    if hst is not None:
                        hst.link.close()
        except Exception as exc:
            report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        finally:
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            artifact.write(json.dumps(report, indent=2) + '\n')
    print(args.out)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
