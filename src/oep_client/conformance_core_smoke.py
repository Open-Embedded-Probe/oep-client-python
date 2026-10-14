"""Small raw-message checks for the cyclic-corr draft; no transport/equipment access."""
import copy
import hashlib
from pathlib import Path
import secrets
import struct

from .conformance import ops, require, tlvs
from . import conformance as _helpers

REVIEWED_CORE_SHA256 = '4c5dcdcc779c5ba0ee85060a33b2b629deddad3e8f8f1a22e18bdeea9d471bdc'  # oep-spec a09fa45 docs/oep-core.ja.md


class CoreSmokeChecks:
    """Selected core contracts, not complete core conformance or a Host runtime."""
    def __init__(self, send, registry, unit, *, spec, observe_effects=None):
        require(spec.get('core_sha256') == REVIEWED_CORE_SHA256, 'core smoke contract needs review for this SPEC')
        limits = registry.get('limits', {})
        require((limits.get('request_header_bytes'), limits.get('result_header_bytes'), limits.get('corr_half_range')) ==
                (10, 5, 32768), 'core smoke requires corr16 cyclic draft')
        self.send, self.reg, self.unit, self.spec = send, registry, unit, dict(spec)
        self.observe = observe_effects
        self.core = {row['name']: row['code'] for row in registry['core']['op']}
        self.zero = 100
        self.trace, self.results, self.observed = [], [], {}
        self.session = None
        self.frame = 64

    def call(self, name, payload=b'', *, sid=0, corr=None, fn=0):
        if corr is None:
            self.zero += 1
            corr = self.zero
        opcode = self.core[name] if isinstance(name, str) else name
        request = struct.pack('<BHHBI', 1, corr, fn, opcode, sid) + payload
        require(len(request) <= self.frame, 'request frame limit')
        record = {'request_hex': request.hex()}
        self.trace.append(record)
        reply = self.send(request)
        record['response_hex'] = reply.hex()
        require(5 <= len(reply) <= self.frame, 'result header/frame limit')
        role, received, resolution, detail = struct.unpack_from('<BHBB', reply)
        require(role == 2 and received == corr, 'result role/corr')
        require(resolution in (0, 1) and (resolution != 1 or detail in (0, 1, 2)), 'result resolution/outcome')
        return reply

    def success(self, reply):
        require(reply[3:5] == b'\1\0', 'completed success required')
        return reply[5:]

    def rejected(self, reply, reason):
        require(reply[3:5] == bytes([0, self.reg['reject_reasons'][reason]]), 'expected rejected ' + reason)
        if reason == 'unsupported':
            require(len(reply) >= 6 and (reply[5] == 0 or reply[5] & 0x7F), 'unsupported field/tag marker')
            tlvs(reply[6:])
        else:
            tlvs(reply[5:])

    def confirm(self):
        p = self.success(self.call('confirm', b'OEP?\1\1'))
        require(len(p) >= 16 and p[:4] == b'OEP!', 'confirm fixed fields')
        revision, frame, window, inflight, boot = struct.unpack_from('<BHIBI', p, 4)
        require(revision == 1 and frame >= 64 and window >= frame and inflight >= 1, 'confirm limits')
        transports = [v for t, v in tlvs(p[16:]) if t == 1]
        require(transports and len(transports[0]) == 1, 'confirm transport')
        self.frame, self.boot = frame, boot
        self.observed['confirm'] = {'max_frame': frame, 'window': window, 'max_inflight': inflight,
                                    'boot_id': boot, 'transport': transports[0][0]}

    def describe(self, fn):
        first, rows = 0, []
        while True:
            p = self.success(self.call('describe', struct.pack('<HH', fn, first)))
            require(p and p[0] in (0, 1), 'describe more')
            page = tlvs(p[1:]); rows.extend(page); first += len(page)
            require(first <= 65535 and (not p[0] or page), 'describe progress')
            if not p[0]:
                break
        return rows

    def discovery(self):
        rows = self.describe(0)
        self.observed['core_describe'] = [{'tag': t, 'value_hex': v.hex()} for t, v in rows]
        values = dict(reversed(rows))
        tags = self.reg['core']['tlv']['describe']
        identity = values.get(tags['unit_id'], b'').decode('ascii')
        require(identity.lower() == self.unit.lower() and 1 <= len(identity) <= 32 and
                all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in identity), 'unit_id')
        require(set(self.core.values()) <= ops(values.get(self.reg['describe_common']['ops'], b'')), 'core ops')
        maximum = values.get(tags['max_op_ms'], b'')
        require(len(maximum) == 4 and 1 <= int.from_bytes(maximum, 'little') <= 600000, 'max_op_ms')
        require(any(t == tags['transport'] and len(v) == 3 and v[0] == self.observed['confirm']['transport']
                    and 1 <= v[1] <= 6 for t, v in rows), 'transport declaration')
        first, entries, total = 0, [], None
        while True:
            p = self.success(self.call('list', struct.pack('<H', first)))
            require(len(p) >= 3, 'list prefix')
            count_total, count = struct.unpack_from('<HB', p)
            require(total is None or total == count_total, 'stable list total')
            total = count_total; at = 3
            for _ in range(count):
                require(at + 6 <= len(p), 'list entry prefix')
                fn, instance, revision, n = struct.unpack_from('<HHBB', p, at); at += 6
                require(fn and revision and 1 <= n <= 48 and at + n <= len(p), 'list entry values')
                name = p[at:at+n].decode('ascii'); at += n
                require(all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-.' for c in name), 'list name')
                entries.append({'fn': fn, 'instance': instance, 'revision': revision, 'name': name})
            first += count
            require(at == len(p) and first <= total and (first == total or count), 'list progress/shape')
            if first == total:
                break
        require(len({row['fn'] for row in entries}) == len(entries), 'unique list fn')
        self.observed['interfaces'] = entries

    def clock(self):
        values = []
        for _ in range(2):
            p = self.success(self.call('clock'))
            require(len(p) >= 12, 'clock fixed prefix')
            boot, now = struct.unpack_from('<IQ', p)
            require(boot == self.boot, 'clock boot_id'); tlvs(p[12:]); values.append(now)
        require(values[1] >= values[0], 'clock monotonic; equal ticks allowed')

    def replay(self, name, payload, corr):
        original = self.call(name, payload, sid=self.session, corr=corr)
        tlvs(self.success(original))  # keepalive/end have an empty fixed prefix.
        before = copy.deepcopy(self.observe()) if self.observe else None
        repeated = self.call(name, payload, sid=self.session, corr=corr)
        result = ('retained' if repeated[3:5] == b'\1\0' else
                  'result_lost' if repeated[3:5] == bytes([0, self.reg['reject_reasons']['result_lost']]) else 'unexpected')
        observation = {'operation': name, 'corr': corr, 'result': result,
            'response_resolution': repeated[3], 'response_detail': repeated[4], 'effects_observed': self.observe is not None}
        self.observed.setdefault('replays', []).append(observation)
        if repeated[3:5] == b'\1\0':
            require(repeated == original, 'retained replay changed result')
        else:
            self.rejected(repeated, 'result_lost')
        if self.observe:
            after = copy.deepcopy(self.observe())
            observation.update(effects_before=before, effects_after=after)
            require(after == before, 'replay changed observed effects')

    def open_session(self, corr=1):
        sid = secrets.randbits(32) or 1
        self.observed['attempted_session'] = sid
        owner = b'core-smoke'
        body = struct.pack('<IB', 60000, 0) + bytes([1]) + struct.pack('<H', len(owner)) + owner
        p = self.success(self.call('open', body, sid=sid, corr=corr))
        self.session = sid
        require(len(p) >= 8 and struct.unpack_from('<II', p) == (60000, self.boot), 'open lease/boot')
        tlvs(p[8:])
        return sid

    def session_cycle(self):
        self.open_session(65534)
        self.success(self.call('keepalive', sid=self.session, corr=65535))
        self.replay('keepalive', b'', 1)
        state = self.success(self.call('lock_state'))
        require(len(state) >= 5 and state[0] == 1, 'wrap released session'); tlvs(state[5:])
        sid = self.session
        self.replay('end', b'', 2)
        self.session = None
        state = self.success(self.call('lock_state'))
        require(len(state) >= 5 and struct.unpack_from('<BI', state) == (0, 0), 'end did not release lock')
        self.rejected(self.call('open', struct.pack('<IB', 60000, 0), sid=sid, corr=3), 'no_session')

    def run(self):
        steps = [('CORE-SMOKE-CONFIRM', self.confirm), ('CORE-SMOKE-DISCOVERY', self.discovery),
                 ('CORE-SMOKE-CLOCK', self.clock), ('CORE-SMOKE-ZERO',
                    lambda: self.rejected(self.call('clock', corr=0), 'malformed')),
                 ('CORE-SMOKE-CYCLE', self.session_cycle)]
        failed = False
        for case, method in steps:
            row = {'id': case, 'level': 'core', 'status': 'not_run'}
            if failed:
                row['reason'] = 'preceding prerequisite failed'
            else:
                try:
                    method(); row['status'] = 'passed'
                except Exception as exc:
                    row.update(status='failed', error=f'{type(exc).__name__}: {exc}'); failed = True
            self.results.append(row)
        return {'status': 'failed' if failed else 'passed', 'full_conformance': False,
            'spec': self.spec, 'checker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'helper_sha256': hashlib.sha256(Path(_helpers.__file__).read_bytes()).hexdigest(),
            'scope': 'selected raw-message discovery, clock and cyclic-session/replay contracts',
            'checks': self.results, 'observed': self.observed, 'exchanges': self.trace,
            'unfinished_session': self.session,
            'unchecked': ['physical framing and transport faults', 'full lease/force and owner display behavior',
                          'resources and interface operation behavior', 'Host half-range admission and old-reply draining',
                          'fn 0 quick-operation processing bound (core_quick_op_max_ms) and other timing',
                          'optional policy admission of session operations (unavailable cause 7)',
                          'all mandatory core conditions'] + ([] if self.observe else ['replay side effects not independently observed'])}
