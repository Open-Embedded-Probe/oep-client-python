"""Current-contract mandatory link transfers composed with logical GPIO/plan."""
import struct

from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Core, tlv


class LinkTransfers(Gpio):
    functions=(1,2,3,4)

    def list_page(self,first,limit):
        names=(b'oep.probe.plan',b'oep.fixture.gpio',b'oep.fixture.gpio',b'oep.probe.link')
        rows=[struct.pack('<HHBB',i+1,int(i==2),1,len(name))+name for i,name in enumerate(names)]
        page=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)

    def describe(self,fn):
        if fn==4:return (tlv(7,b'\x10\x03'),)
        return super().describe(fn)

    def request_tags(self,fn,op,data):
        if fn==4:
            from .virtual_core import tail
            return tail(data)
        return super().request_tags(fn,op,data)

    def dispatch_request(self,fn,op,payload,tags):
        if fn!=4:return super().dispatch_request(fn,op,payload,tags)
        if op==16:
            requested=struct.unpack('<I',payload)[0]
            count=min(requested,self.frame_limit-7)
            return struct.pack('<H',count)+bytes(i%256 for i in range(count))
        return b''


class LinkTransferModel(GpioModel):
    def __init__(self,*args,max_frame=256,**kwargs):
        if max_frame not in (64,256,1024):raise ValueError('explicit transfer geometry')
        self.frame_limit=max_frame
        super().__init__(*args,**kwargs)
        self.ep.probe.max_frame=max_frame

    def install(self):
        self.extension=LinkTransfers();self.extension.frame_limit=self.frame_limit
        self.ep.current_core=Core(self.ep,self.extension)

    def handle(self,request):
        if len(request)<6 or int.from_bytes(request[3:5],'little')!=4:
            return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free
        count=int.from_bytes(request[10:12],'little') if len(request)>=12 else 0
        e.fixed={16:4,17:2+count};e.free={16,17}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free

    def link_state(self):
        state=self.gpio_state();c=self.ep.current_core
        state['ownership']=dict(holder=c.holder,owner=c.owner.hex(),lease=c.lease,generation=c.generation)
        return state
