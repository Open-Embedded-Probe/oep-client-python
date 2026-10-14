"""Logical counted-write and debug-progress fixture; no target delivery."""
import struct
from .virtual_stream_model import StreamModel, Stream
from .virtual_core import Core, Completed, Reject, tlv


class Outcomes(Stream):
    functions=(1,2)
    tx_capacity=4

    def __init__(self, ep, position=0, serial=0):
        super().__init__(ep,position,serial)
        self.failure_after=None;self.failure_status=1;self.executed=[]

    def list_page(self, first, limit):
        names=[self.name,b'io.github.open-embedded-probe.progress']
        entries=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in zip(self.functions,names)]
        result=bytearray(struct.pack('<HB',2,0))
        for row in entries[first:]:
            if len(result)+len(row)>limit:break
            result.extend(row);result[2]+=1
        return bytes(result)

    def describe(self, fn):return super().describe(fn) if fn==1 else (tlv(7,b'\x10\x03'),)

    def dispatch(self, fn, op, payload):
        tail=tlv(0x7f,b'x')
        if fn==1:
            if op!=20:return super().dispatch(fn,op,payload)
            count=struct.unpack_from('<H',payload)[0]
            accepted=min(count,self.tx_capacity-len(self.tx))
            self.tx.extend(payload[2:2+accepted])
            outcome=0 if accepted==count else (2 if accepted else 1)
            return Completed(struct.pack('<H',accepted)+tail,outcome)
        if op==17:
            if self.failure_after is None:return struct.pack('<I',0x12345678)+tail
            return Completed(bytes((self.failure_status,))+tail,1)
        count=struct.unpack('<H',payload)[0]
        if count>8:raise Reject(11,b'\0')
        done=count if self.failure_after is None else min(count,self.failure_after)
        failed=done<count
        status=self.failure_status if failed else 0
        values=[0xa000+i for i in range(done)]
        self.executed.extend(values)
        return Completed(struct.pack('<HB',done,status)+b''.join(struct.pack('<I',value) for value in values)+tail,
                         (2 if done else 1) if failed else 0)


class OutcomeModel(StreamModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms,boot_id)
        self.extension=Outcomes(self.ep);self.ep.current_core=Core(self.ep,self.extension)

    def handle(self, request):
        extension=self.extension;fixed,free=extension.fixed,extension.free
        if len(request)>=6 and struct.unpack_from('<H',request,3)[0]==2:
            extension.fixed={16:2,17:0};extension.free=set()
        try:return super().handle(request)
        finally:extension.fixed=fixed;extension.free=free

    def restart_fixture(self, position=0, serial=0):
        result=super().restart_fixture(position,serial)
        self.extension=Outcomes(self.ep,position,serial);self.ep.current_core=Core(self.ep,self.extension)
        return result

    def configure_debug(self, done, status):
        if done is not None and (type(done) is not int or not 0<=done<=8):raise ValueError('logical failure index')
        if type(status) is not int or status not in range(1,6):raise ValueError('logical standard failure status')
        self.extension.failure_after=done;self.extension.failure_status=status

    def drain_tx(self, count):
        if type(count) is not int or not 0<=count<=len(self.extension.tx):raise ValueError('logical queued bytes only')
        result=bytes(self.extension.tx[:count]);del self.extension.tx[:count];return result

    def outcome_state(self):
        s=self.extension
        return {'queued':bytes(s.tx),'executed':list(s.executed),'stream':self.stream_state(),
                'failure_after':s.failure_after,'failure_status':s.failure_status}
