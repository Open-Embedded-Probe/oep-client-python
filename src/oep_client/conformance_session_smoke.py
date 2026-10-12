"""Selected raw session lifetime checks with caller-supplied time/resources."""
import copy
import hashlib
from pathlib import Path
import secrets
import struct

from .conformance import require, tlvs


class SessionSmokeChecks:
    def __init__(self, core, *, wait_ms, prepare_resource=None, resources_released=None):
        require(callable(wait_ms), 'explicit time adapter required')
        require((prepare_resource is None) == (resources_released is None), 'resource adapter and predicate must be paired')
        self.c, self.wait = core, wait_ms
        self.prepare_resource, self.released = prepare_resource, resources_released
        self.results, self.observations = [], []
        self.used = set()

    def new_sid(self):
        while True:
            sid = secrets.randbits(32)
            if sid and sid not in self.used:
                self.used.add(sid)
                return sid

    def open(self, *, force=False):
        sid = self.new_sid()
        self.c.observed.setdefault('attempted_sessions', []).append(sid)
        reply = self.c.call('open', struct.pack('<IB', 1000, force), sid=sid, corr=1)
        payload = self.c.success(reply)
        require(len(payload) >= 8 and struct.unpack_from('<II', payload) == (1000, self.c.boot), 'open lease/boot')
        tlvs(payload[8:])
        self.c.session, self.corr = sid, 1
        return sid

    def owned(self, operation, payload=b'', *, fn=0):
        self.corr += 1
        return self.c.call(operation, payload, sid=self.c.session, corr=self.corr, fn=fn)

    def seed(self):
        if self.prepare_resource:
            self.prepare_resource(self.owned)
            require(not self.released(), 'resource adapter did not establish observed ownership')

    def advance(self, ms):
        self.observations.append(dict(action='wait', requested_ms=ms))
        self.wait(ms)

    def state(self, held):
        payload = self.c.success(self.c.call('lock_state'))
        require(len(payload) >= 5, 'lock_state fixed fields')
        locked, remaining = struct.unpack_from('<BI', payload)
        require(locked == int(held) and (0 < remaining <= 1000 if held else remaining == 0), 'lock/lease state')
        tlvs(payload[5:])

    def release_observation(self, action):
        if self.released:
            released = self.released()
            self.observations.append(dict(action=action, resources_released=released))
            require(released, action + ' leaked observed resource')

    def locked(self, reply):
        require(reply[3:5] == bytes([0, self.c.reg['reject_reasons']['locked']]), 'expected locked')
        require(len(reply) >= 9 and 0 < int.from_bytes(reply[5:9], 'little') <= 1000, 'locked remaining time')
        tlvs(reply[9:])

    def replay(self, corr, original):
        before = copy.deepcopy(self.c.observe()) if self.c.observe else None
        reply = self.c.call('keepalive', sid=self.c.session, corr=corr)
        if reply[3:5] == bytes([0, self.c.reg['reject_reasons']['result_lost']]):
            self.c.rejected(reply, 'result_lost')
        else:
            require(reply == original, 'retained keepalive result differs')
        row = dict(action='keepalive replay', effects_observed=self.c.observe is not None)
        if self.c.observe:
            after = copy.deepcopy(self.c.observe())
            row.update(before=before, after=after)
            require(before == after, 'replay changed observed effects/lease')
        self.observations.append(row)

    def expiry(self):
        sid = self.open(); self.seed()
        original = self.owned('keepalive'); tlvs(self.c.success(original))
        corr = self.corr
        self.advance(700)
        self.replay(corr, original)
        self.state(True)
        other = self.new_sid()
        self.locked(self.c.call('open', struct.pack('<IB', 1000, 0), sid=other, corr=1))
        self.advance(400)
        self.state(False); self.release_observation('expiry')
        self.replay(corr, original)
        self.state(False); self.release_observation('expired replay')
        self.c.rejected(self.owned('keepalive'), 'no_session')
        self.c.session = None

    def rejected_renewal(self):
        self.open(); self.seed()
        self.advance(700)
        # This known, owner request passes the session stage before its bad body.
        self.c.rejected(self.owned('keepalive', b'\0'), 'malformed')
        self.advance(700)
        self.state(True)
        tlvs(self.c.success(self.owned('end')))
        self.c.session = None
        self.release_observation('end')

    def force(self):
        previous = self.open(); self.seed()
        self.open(force=True)
        self.release_observation('force')
        before = copy.deepcopy(self.c.observe()) if self.c.observe else None
        self.locked(self.c.call('keepalive', sid=previous, corr=100))
        if self.c.observe:
            require(before == self.c.observe(), 'old owner request changed effects')
        self.seed()
        tlvs(self.c.success(self.owned('end')))
        self.c.session = None
        self.release_observation('new owner end')

    def prepare(self):
        self.c.confirm(); self.c.discovery()

    def run(self):
        failed = False
        for label, method in [('CONTEXT', self.prepare), ('EXPIRY', self.expiry),
                              ('REJECTED-RENEWAL', self.rejected_renewal), ('FORCE', self.force)]:
            row = dict(id='SESSION-SMOKE-' + label, status='not_run', level='core')
            if failed:
                row['reason'] = 'preceding prerequisite failed'
            else:
                try:
                    method(); row['status'] = 'passed'
                except Exception as exc:
                    row.update(status='failed', error=f'{type(exc).__name__}: {exc}'); failed = True
            self.results.append(row)
        return dict(status='failed' if failed else 'passed', full_conformance=False,
                    scope='selected lease/replay/force timing with explicit caller adapters',
                    spec=self.c.spec, checks=self.results, observations=self.observations,
                    exchanges=self.c.trace, observed=self.c.observed, unfinished_session=self.c.session,
                    checker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    core_checker_sha256=hashlib.sha256(Path(__file__).with_name('conformance_core_smoke.py').read_bytes()).hexdigest(),
                    unchecked=['operation execution longer than lease and queued force', 'physical timer/framing/transport accuracy',
                               'all lease limits, owner display and other core conditions'] +
                              ([] if self.c.observe else ['replay/old-owner side effects not independently observed']) +
                              ([] if self.released else ['resource release: no resource adapter']))
