"""Minimal standard op declaration fixture; does not implement advertised ops."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, Reject, tlv


MINIMAL={
    'oep.probe.plan':(16,17),'oep.probe.restart':(16,),'oep.probe.link':(16,17),
    'oep.wire.rvswd':(17,18,19),'oep.wire.swio':(17,18,19),'oep.wire.swd':(17,18,19),
    'oep.target.riscv-dm':(16,17,18),'oep.target.arm-adi':(16,17,18),
    'oep.target.console':tuple(range(16,24)),
    'oep.fixture.gpio':(16,17),'oep.fixture.uart':tuple(range(16,23)),
    'oep.fixture.i2c-target':tuple(range(16,20)),'oep.fixture.spi-target':tuple(range(16,20)),
    'oep.fixture.logic':(16,17,18,20,21,22,23),'oep.fixture.analog':(16,17,18,20,21,22,23,25),
    'oep.fixture.capture-group':(16,17,18,20),'oep.probe.config':(16,17,20,21),
}


class RegisteredOps(Resources):
    def __init__(self):
        super().__init__();self.selection=dict(enumerate(MINIMAL,1));self.functions=tuple(self.selection);self.offered={fn:set(MINIMAL[name]) for fn,name in self.selection.items()}
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBBB',fn,0,1,0,len(name))+name.encode() for fn,name in self.selection.items()];page=bytearray(struct.pack('<HB',len(rows),0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        offered=self.offered[fn];bitmap=bytearray(32)
        for op in offered:bitmap[op//8]|=1<<(op%8)
        while len(bitmap)>1 and bitmap[-1]==0:bitmap.pop()
        return (tlv(7,b'\0'+bitmap),)
    def dispatch(self,*args):raise Reject(10)


class RegisteredOpsModel(ResourceModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.extension=RegisteredOps();self.selection=dict(self.extension.selection);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn=struct.unpack_from('<H',request,3)[0]
            if fn in e.offered:e.fixed={op:0 for op in e.offered[fn]};e.free=set(e.fixed)
        try:return super().handle(request)
        finally:e.fixed,e.free=fixed,free
