"""Wire-only sample host for ending before corr exhaustion; no automatic retry."""
import secrets
import struct
from .conformance import require, tlvs


class SampleRolloverHost:
    def __init__(self, checks, pending, route, reader_barrier):
        require(checks.session is not None and callable(reader_barrier), 'active session and explicit reader barrier required')
        self.c, self.pending, self.route, self.reader_barrier = checks, pending, route, reader_barrier
        self.boot = checks.boot
        self.used_sessions = {checks.session} | {row['session_id'] for row in pending.snapshot() if row['session_id']}
        self.bindings = {}
        self.state = 'active'

    def request(self, op, payload=b'', *, fn=0):
        require(self.state == 'active' and self.c.session is not None, 'session is not active')
        is_end = fn == 0 and op == self.c.core['end']
        require(self.c.corr < (65535 if is_end else 65534), 'reserve last corr for end before exhaustion')
        corr = self.c.corr + 1
        request = self.c.request(op,payload,session=self.c.session,fn=fn,corr=corr)
        self.pending.track(self.route,request,self.boot)
        self.c.corr = corr  # failed admission never consumes a number
        return request

    def exchange(self, request):
        frame = self.c.exchange(request)
        require(self.pending.accept(self.route,self.pending.epochs[self.route],frame), 'unexpected rollover result')
        return frame

    def bind(self, rid, fn, kind):
        require(self.state == 'active' and type(rid) is int and 1 <= rid <= 65535, 'active resource binding required')
        self.bindings[rid] = (self.boot,self.c.session,fn,kind)

    def binding(self, rid):
        require(self.state == 'active' and rid in self.bindings and self.bindings[rid][:2] == (self.boot,self.c.session),
                'old resource binding is invalid')
        return self.bindings[rid]

    def choose_sid(self):
        sid = secrets.randbelow(0xffffffff)+1
        while sid in self.used_sessions: sid = secrets.randbelow(0xffffffff)+1
        return sid

    def switch_session(self):
        require(self.state == 'active' and self.c.session is not None and self.c.corr <= 65534,
                'cannot switch after consuming reserved end number')
        require(self.route not in self.pending.quarantined, 'abandoned route requires explicit recovery, not rollover')
        require(not any(row['route']==self.route and row['status']=='pending' for row in self.pending.snapshot()),
                'resolve or explicitly abandon all route pending before switching')
        replacement = self.choose_sid()
        require(type(replacement) is int and 1 <= replacement <= 0xffffffff and replacement not in self.used_sessions,
                'fresh unpredictable nonzero session ID required')
        old = self.c.session
        end = self.request(self.c.core['end'])
        self.state = 'ending'
        self.bindings.clear()
        try:
            frame = self.c.exchange(end)
            require(frame[3:5] == b'\x01\0', 'end not confirmed')
            tlvs(frame[5:])
            require(self.pending.accept(self.route,self.pending.epochs[self.route],frame), 'unexpected end result')
        except Exception:
            self.state = 'end-unknown'
            self.pending.abandon(self.route)
            self.c.session = None  # no new-corr cleanup retry or automatic force
            raise
        self.c.session = None
        self.pending.retired_sessions.add(old)
        self.pending.abandon(self.route)  # no pending; retire the old reader without rewriting results
        self.state = 'checking'
        self.c.confirm(); self.c.identity()
        if self.c.boot != self.boot:
            self.state = 'boot-changed'
            return {'status':self.state,'old_session':old,'new_session':None}
        if self.reader_barrier() is not True:
            self.state = 'reader-unavailable'
            return {'status':self.state,'old_session':old,'new_session':None}
        self.pending.resume(self.route,transport_recovered=True,confirmed_boot=self.boot)
        self.used_sessions.add(replacement)
        self.c.corr = 0
        opening = self.c.request(self.c.core['open'],struct.pack('<IB',60000,0),session=replacement)
        self.pending.track(self.route,opening,self.boot)
        self.state = 'open-unknown'
        try:
            frame = self.c.exchange(opening)
            require(frame[3:5] == b'\x01\0' and len(frame)>=13 and
                    struct.unpack_from('<II',frame,5)==(60000,self.boot), 'new open not confirmed')
            tlvs(frame[13:])
            require(self.pending.accept(self.route,self.pending.epochs[self.route],frame), 'unexpected open result')
        except Exception:
            self.pending.abandon(self.route)
            raise
        self.c.session = replacement
        self.state = 'active'
        return {'status':'switched','old_session':old,'new_session':replacement}
