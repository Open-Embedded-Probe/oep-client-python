"""Current Core/GPIO fixture with Endpoint reboot and delayed response barrier."""
import copy
import struct
from .virtual_core import Core,tail,tlv
from .virtual_gpio_model import Gpio,GpioModel


class RestartExtension(Gpio):
    functions=(1,2,3,4)
    def __init__(self,model):super().__init__();self.model=model
    def list_page(self,first,limit):
        names=(b'oep.probe.plan',b'oep.fixture.gpio',b'oep.fixture.gpio',b'oep.probe.restart')
        rows=[struct.pack('<HHBBB',i+1,int(i==2),1,0,len(n))+n for i,n in enumerate(names)]
        out=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(out)+len(row)>limit:break
            out.extend(row);out[2]+=1
        return bytes(out)
    def describe(self,fn):
        return (tlv(7,b'\x10\1'),tlv(64,struct.pack('<I',50))) if fn==4 else super().describe(fn)
    def request_tags(self,fn,op,data):return tail(data) if fn==4 else super().request_tags(fn,op,data)
    def dispatch_request(self,fn,op,payload,tags):
        if fn==4:self.model.requested=True;return b''
        return super().dispatch_request(fn,op,payload,tags)


class RestartModel(GpioModel):
    routes=(0,1,2)
    def __init__(self,*,boot_id=17):
        self.clock_ms=0;self.pending=None;self.requested=False;self.return_at=None;self.events=[]
        super().__init__(lambda:self.clock_ms,boot_id=boot_id)
        self.ep.probe.max_frame=256
        self.ep.static[0]=tuple(t for t in self.ep.static[0] if t[0]!=73)+tuple(tlv(73,bytes((r,k,255))) for r,k in ((0,1),(1,4),(2,6)))
    def install(self):self.extension=RestartExtension(self);self.ep.current_core=Core(self.ep,self.extension)
    def complete_boot(self):
        self.extension.release();boot=(self.ep.boot_id+1)&0xffffffff
        self.ep.reboot(boot);self.install();self.return_at=None
        self.events.append(dict(action='boot',time_ms=self.clock_ms,boot_id=boot))
    def advance(self,delta):
        if type(delta)is not int or delta<0:raise ValueError('forward logical milliseconds required')
        self.clock_ms+=delta
        if self.return_at is not None and self.clock_ms>=self.return_at:self.complete_boot()
        elif self.return_at is None:self.ep.current_core.tick()
    def begin(self,request,route=0):
        if route not in self.routes:raise ValueError('explicit route required')
        if self.pending is not None:raise ValueError('finish pending logical response first')
        if self.return_at is not None:
            self.events.append(dict(action='silent',time_ms=self.clock_ms,route=route,request_hex=request.hex()))
            return None
        self.ep.current_core.tick();e=self.extension;fixed,free=e.fixed,e.free
        fn=int.from_bytes(request[3:5],'little') if len(request)>=6 else 0;n=request[10] if len(request)>10 else 0
        if fn==4:e.fixed={16:0};e.free=set()
        elif fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
        else:e.fixed={16:1+3*n,17:1+2*n};e.free={17}
        self.requested=False
        try:reply=self.ep.current_core.handle(request,route,defer_send=True)
        finally:e.fixed,e.free=fixed,free
        self.pending=dict(request=request,reply=reply,route=route,token=self.ep.current_core.renewal,restart=self.requested)
        self.events.append(dict(action='prepared',time_ms=self.clock_ms,route=route,request_hex=request.hex(),reply_hex=reply.hex() if reply is not None else None,boot_id=self.ep.boot_id))
        return reply
    def finish(self):
        if self.pending is None:raise ValueError('no prepared response')
        p,self.pending=self.pending,None
        self.events.append(dict(action='sent' if p['reply'] is not None else 'discard',time_ms=self.clock_ms,route=p['route'],request_hex=p['request'].hex(),reply_hex=p['reply'].hex() if p['reply'] is not None else None,boot_id=self.ep.boot_id))
        if p['reply'] is not None:self.ep.current_core.response_sent(p['token'])
        if p['restart']:self.return_at=self.clock_ms+50
        return p['reply']
    def handle(self,request,route=0):
        reply=self.begin(request,route)
        return self.finish() if self.pending is not None else reply
    def restart_state(self):
        c=self.ep.current_core
        return dict(time_ms=self.clock_ms,boot_id=self.ep.boot_id,pending=self.pending is not None,restarting=self.return_at is not None,
                    return_at=self.return_at,holder=c.holder,generation=c.generation,cache_keys=list(c.cache),gpio=copy.deepcopy(self.gpio_state()))
    def restart_fixture(self):
        if self.pending is not None or self.return_at is not None:raise ValueError('idle fixture required')
        return super().restart_fixture()
