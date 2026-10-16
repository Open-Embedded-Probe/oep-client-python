"""Minimal standard op declaration fixture; does not implement advertised ops."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, Reject, tlv
from . import registry as _reg


# Each interface's mandatory ops, by name (looked up in the registry: never literal numbers).
_MANDATORY={
    'oep.probe.plan':('plan_apply','plan_release'),'oep.probe.restart':('restart',),'oep.probe.link':('source','sink'),
    **{w:('attach','detach','connections','holds') for w in ('oep.wire.rvswd','oep.wire.swio','oep.wire.swd')},
    'oep.target.riscv-dm':('dmi',),'oep.target.arm-adi':('transfer',),
    'oep.target.console':('open','read','close','streams'),
    'oep.fixture.gpio':('set','read'),'oep.fixture.uart':('configure','read','status'),
    'oep.fixture.i2c-target':('configure','read_rx','preload_tx','status'),'oep.fixture.spi-target':('configure','arm','read_rx','status'),
    **{c:('configure','start','stop','status','read','segments') for c in ('oep.fixture.logic','oep.fixture.analog')},
    'oep.fixture.capture-group':('bind','start','stop','status'),'oep.probe.config':('get','set','unset'),
}
MINIMAL={name:tuple(_reg.INTERFACES[name].op[o] for o in ops) for name,ops in _MANDATORY.items()}


class RegisteredOps(Resources):
    def __init__(self):
        super().__init__();self.selection=dict(enumerate(MINIMAL,1));self.functions=tuple(self.selection);self.offered={fn:set(MINIMAL[name]) for fn,name in self.selection.items()};self.extra={}
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name.encode() for fn,name in self.selection.items()];page=bytearray(struct.pack('<HB',len(rows),0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        offered=self.offered[fn];bitmap=bytearray(32)
        for op in offered:bitmap[op//8]|=1<<(op%8)
        while len(bitmap)>1 and bitmap[-1]==0:bitmap.pop()
        return (tlv(7,b'\0'+bitmap),*self.extra.get(fn,()))
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
