"""Logical optional ndmreset fixture; no reset pin or physical DM."""
import struct
from .virtual_step_model import Steps, StepModel
from .virtual_block_model import HARTSEL
from .virtual_debug_model import fixed_dmi
from .virtual_dmseq_model import initial
from .virtual_core import Core, Completed, Reject, tlv


class Resets(Steps):
    def __init__(self,ep):super().__init__(ep);self.reset_result='ok'
    def describe(self,fn):return (tlv(7,b'\x10\xbf'),tlv(3,struct.pack('<H',8))) if fn==5 else super().describe(fn)
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=5 or op!=19:return super().dispatch_request(fn,op,payload,tags)
        parent,mode=struct.unpack('<HB',payload);self.resource(parent,'connection')
        if self.connections[parent]['fn']==3:raise Reject(4,tlv(1,b'\x06'))
        if mode not in (0,1,2):raise Reject(11,b'\0')
        target=self.targets[parent];result=self.reset_result;related=[rid for rid,(p,_) in self.streams.items() if p==parent]
        for rid in related:self.mailboxes[rid]['debug_busy']=True
        control=target['registers'].get(0x10,0)&~(HARTSEL|0x80000002)
        target['haltreq']=mode==2;control|=0x80000000 if mode==2 else 0
        status=0;flags=0;pc=0
        try:
            self.writereg(parent,0x10,control|2);self.access.append(('ndmreset-assert',parent))
            for rid in related:self.streams[rid][1].mark(1,1)
            self.writereg(parent,0x10,control);self.access.append(('ndmreset-release',parent,target['haltreq']))
            target['pc']=0x2000;target['halted']=mode==2
            failures=5 if result=='dm-line' else 2 if result=='dm-recover' else 0
            responded=False
            for attempt in range(5):
                self.interleave(parent);line=attempt<failures;self.access.append(('reset-dm-read',parent,2 if line else 0,attempt*200))
                if not line:responded=True;break
            if not responded:status=2
            else:
                self.access.append(('ack-havereset',parent))
                for rid in related:
                    self.streams[rid][1].mark(2,1)
                    if rid in self.protocols:self.protocols[rid]=initial()
                if result=='state-timeout':status=4;target['halted']=mode!=2
                elif result=='fault':status=3
                elif result=='verify-timeout':status=4;flags=int(mode==1)
                else:
                    flags=1;pc=target['pc'] if mode==2 else 0
                    if mode==1:
                        saved={reg:target['registers'].get(reg,0) for reg in (4,5)};dcsr=target['dcsr'];self.access.append(('reset-verify-before',parent,dcsr,dict(saved)))
                        target['halted']=True;target['haltreq']=True;target['dcsr']|=4
                        self.writereg(parent,5,0x7b0);self.writereg(parent,4,target['dcsr']);self.interleave(parent)
                        self.access.append(('reset-verify-resume',parent));target['pc']=0x2004;target['dcsr']=dcsr
                        for reg in (5,4):self.writereg(parent,reg,saved[reg]);self.restore.append(('reset-restore',parent,reg,saved[reg]))
                        target['halted']=result=='verified-halted';target['haltreq']=False;flags=2 if result=='verified-halted' else 3;pc=target['pc']
        finally:
            if mode!=2:target['haltreq']=False
            self.writereg(parent,0x10,control if target['haltreq'] else control&~0x80000000)
            for rid in related:self.mailboxes[rid]['debug_busy']=False
        good=bool(flags&(2 if mode==1 else 1))
        return Completed(struct.pack('<BBI',status,flags,pc)+tlv(127,b'x'),int(not good))


class ResetModel(StepModel):
    def install(self):self.extension=Resets(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        if len(request)<6 or struct.unpack_from('<H',request,3)[0]!=5:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free;e.fixed={16:fixed_dmi(request[10:]),17:2,18:2,19:3,20:8,21:8,23:2};e.free=set()
        if request[5]==21 and len(request)>=18:e.fixed[21]=8+4*struct.unpack_from('<H',request,16)[0]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def reset_state(self):
        state=self.step_state();state['reset_result']=self.extension.reset_result;return state
    def reset_behavior(self,result):
        if result not in ('ok','state-timeout','verify-timeout','dm-recover','dm-line','fault','verified-halted'):raise ValueError('explicit logical reset result required')
        self.extension.reset_result=result
