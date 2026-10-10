"""Explicit same-route recovery sample; no transport replacement or automatic force."""
import struct
from .conformance import require, tlvs
from .conformance_recovery_sample import SampleRecoveryAdapter, SampleRecoveryHost


class SampleSessionLossAdapter(SampleRecoveryAdapter):
    def discard_request(self, request):
        self.trace.append({'method':'discard_request','request_hex':request.hex(),
                           'delivered_to_probe':False,'scope':'logical-model'})


class SampleSessionLossHost(SampleRecoveryHost):
    def recover_session(self, request, boot, reader_barrier):
        # The caller retains the original bytes and has only this request unresolved.
        require(type(request) is bytes and len(request)>=10 and callable(reader_barrier), 'saved request and barrier required')
        role,corr,fn,op,sid = struct.unpack_from('<BHHBI',request)
        require(role==1 and fn==0 and sid!=0 and
                ((op==self.c.core['end'] and corr==65535 and len(request)>=10) or
                 (op==self.c.core['open'] and corr==1 and len(request)>=15)), 'reserved end or initial open required')
        if op==self.c.core['open']:
            require(request[14]==0, 'sample recovery never forces ownership')
        tlvs(request[15:] if op==self.c.core['open'] else request[10:])
        require(self.c.corr==corr, 'no intervening session request allowed')
        def decision(status, outcome='unknown', lease=None):
            return {'status':status,'original_outcome':outcome,'lease_ms':lease}
        if reader_barrier() is not True: return decision('reader-unavailable')
        self.c.confirm(); self.c.identity()
        if self.c.boot!=boot: return decision('boot-changed')
        frame=self.c.exchange(request)  # exact original bytes/corr; never allocate another mutation
        success=frame[3:5]==b'\x01\0'
        if not success:
            require(frame[3]==0 and frame[4] in (self.c.reasons['result_lost'],self.c.reasons['no_session'],self.c.reasons['locked']),
                    'unexpected session replay refusal')
            # Checks.exchange already validates reason-specific fixed fields/TLVs.
            if frame[4]!=self.c.reasons['result_lost']: return decision('session-unavailable')
        outcome='success' if success else 'unknown'
        lease=None
        if op==self.c.core['open']:
            if success:
                require(len(frame)>=13, 'open reply shape')
                lease,reply_boot=struct.unpack_from('<II',frame,5)
                require(1000<=lease<=60000 and reply_boot==boot, 'open lease/boot')
                tlvs(frame[13:])
            status=self.live_session(sid,boot)
            if status!='live': return decision(status,outcome)
            self.c.confirm(); self.c.identity()
            if self.c.boot!=boot:return decision('boot-changed')
            status=self.live_session(sid,boot)
            if status!='live':return decision(status,outcome)
            return decision('open-active',outcome,lease)
        if success: tlvs(frame[5:])
        payload=self.c.success(self.c.request(self.c.core['lock_state']))
        require(len(payload)>=5, 'lock state shape')
        flag,remaining=struct.unpack_from('<BI',payload)
        require(flag in (0,1) and (flag!=0 or remaining==0), 'lock state values')
        tlvs(payload[5:])
        if flag:return decision('lock-held',outcome)
        require(not self.inventory(), 'unlocked sample retains resources')
        self.c.confirm(); self.c.identity()
        if self.c.boot!=boot:return decision('boot-changed')
        return decision('end-confirmed' if success else 'observed-ended',outcome)
