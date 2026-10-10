"""Wire-only fresh-route recovery after abandoning an exhausted session."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_recovery_sample import SampleRecoveryHost


class SampleExhaustedHost(SampleRecoveryHost):
    def __init__(self, checks, functions, pending, old_route, new_route, barrier):
        super().__init__(checks,functions)
        require(old_route!=new_route and callable(barrier), 'distinct route and explicit transport barrier required')
        self.pending,self.old_route,self.new_route,self.barrier=pending,old_route,new_route,barrier
        self.bindings={}

    def choose_sid(self):
        used={row['session_id'] for row in self.pending.snapshot()} | self.pending.retired_sessions
        sid=secrets.randbelow(0xffffffff)+1
        while sid in used:sid=secrets.randbelow(0xffffffff)+1
        return sid

    def recover(self, old_sid, boot):
        require(self.old_route in self.pending.quarantined and old_sid in self.pending.retired_sessions and
                self.new_route not in self.pending.quarantined and self.c.session is None, 'retired session and ready new route required')
        rows=self.pending.snapshot()
        require(any(row['route']==self.old_route and row['session_id']==old_sid and row['corr']==65535 and
                    row['status']=='unknown' for row in rows), 'exhausted unknown request required')
        require(not any(row['status']=='pending' and row['route'] in (self.old_route,self.new_route) for row in rows),
                'resolve or abandon route pending before recovery')
        self.bindings.clear()
        def decision(status,sid=None):
            return {'status':status,'old_session':old_sid,'new_session':sid,'original_outcomes':'unchanged'}
        if self.barrier() is not True:return decision('reader-unavailable')
        self.c.confirm();self.c.identity()
        if self.c.boot!=boot:return decision('boot-changed')
        payload=self.c.success(self.c.request(self.c.core['lock_state']))
        require(len(payload)>=5,'lock state shape')
        flag,remaining=struct.unpack_from('<BI',payload);tlvs(payload[5:])
        require(flag in (0,1) and (flag!=0 or remaining==0),'lock state values')
        if flag:return decision('lock-held')  # no old-S read, force, waiting policy or mutation
        require(not self.inventory(),'unlocked sample retains resources')
        self.c.confirm();self.c.identity()
        if self.c.boot!=boot:return decision('boot-changed')
        sid=self.choose_sid()
        used={row['session_id'] for row in rows} | self.pending.retired_sessions
        require(type(sid) is int and 1<=sid<=0xffffffff and sid not in used,'fresh nonzero unused session ID required')
        request=self.c.request(self.c.core['open'],struct.pack('<IB',60000,0),session=sid,corr=1)
        epoch=self.pending.track(self.new_route,request,boot)
        self.c.corr=1
        try:
            frame=self.c.exchange(request)
            require(frame[3:5]==b'\x01\0' and len(frame)>=13 and
                    struct.unpack_from('<II',frame,5)==(60000,boot),'new session not confirmed')
            tlvs(frame[13:])
            require(self.pending.accept(self.new_route,epoch,frame),'new open result not accounted')
        except Exception:
            self.pending.abandon(self.new_route)
            raise
        self.c.session=sid
        return decision('recovered',sid)
