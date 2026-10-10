"""Explicit logical GPIO/plan fixture; no physical pins or persisted config."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, Reject, tlv


class Gpio(Resources):
    functions=(1,2,3)
    def __init__(self,selectable=True):
        super().__init__();self.selectable=selectable;self.plans={};self.events=[]
        self.idle={ch:(4 if ch==0 else 0,1 if ch==0 and selectable else 0) for ch in range(8)};self.pins=dict(self.idle);self.external={ch:ch%2 for ch in range(8)}
    def list_page(self,first,limit):
        rows=[]
        for fn,name,instance in [(1,b'oep.probe.plan',0),(2,b'oep.fixture.gpio',0),(3,b'oep.fixture.gpio',1)]:rows.append(struct.pack('<HHBBB',fn,instance,1,0,len(name))+name)
        page=bytearray(struct.pack('<HB',3,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return (tlv(7,b'\x10\x03'),tlv(64,struct.pack('<I',4)))
        return (tlv(7,b'\x10\x03'),tlv(1,b'\1\0\0\xff'),tlv(64,struct.pack('<I',127)))+((tlv(65,struct.pack('<BBHH',0,2,2,8)),) if self.selectable else ())
    def request_tags(self,fn,op,data):
        result=[]
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            value,data=data[3:3+size],data[3+size:]
            expected=5 if fn==1 and op==16 and key==16 else 2 if fn in (2,3) and op==16 and key==1 else None
            if expected is not None:
                if len(value)!=expected:raise Reject(3)
                result.append((raw,value))
            elif raw&128:raise Reject(11,bytes((raw,)))
        return result
    def restore(self,ch):self.pins[ch]=self.idle[ch];self.events.append(('idle',ch,*self.pins[ch]))
    def release_plans(self,functions):
        for fn in functions:
            for ch in self.plans.pop(fn,[]):self.restore(ch)
    def release(self):self.release_plans(list(self.plans))
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:
            if op==17:
                n=payload[0];functions=list(struct.unpack('<'+'H'*n,payload[1:]));self.release_plans(functions if n else list(self.plans));return b''
            assignments=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags];seen=set()
            for raw,owner,role,ch in assignments:
                if owner==0 or (owner,role,ch) in seen:raise Reject(3)
                seen.add((owner,role,ch))
            for raw,owner,role,ch in assignments:
                if owner not in self.functions:raise Reject(1)
                if owner not in (2,3) or role!=1 or ch not in range(8):raise Reject(11,bytes((raw,)))
            replaced={owner for _,owner,_,_ in assignments};new={owner:list(chs) for owner,chs in self.plans.items() if owner not in replaced}
            for _,owner,_,ch in assignments:new.setdefault(owner,[]).append(ch)
            all_pins=[ch for chs in new.values() for ch in chs]
            if len(all_pins)>4:raise Reject(4,tlv(1,b'\2'))
            if len(set(all_pins))!=len(all_pins):raise Reject(4,tlv(1,b'\1'))
            self.release_plans(sorted(replaced));self.plans=new;return b''
        n=payload[0]
        if op==17:
            channels=list(struct.unpack('<'+'H'*n,payload[1:]))
            for index,ch in enumerate(channels):
                if ch not in self.plans.get(fn,[]):raise Reject(4,tlv(2,struct.pack('<H',ch))+tlv(64,bytes((index,))))
            return bytes((n,))+bytes(self.level(ch) for ch in channels)
        elements=[struct.unpack_from('<HB',payload,1+3*i) for i in range(n)];drive={}
        for raw,v in tags:
            index,level=v
            if index>=n or index in drive or elements[index][1] not in (3,4):raise Reject(3)
            drive[index]=(raw,level)
        for index,(raw,level) in drive.items():
            if not self.selectable or level not in (0,1,255):raise Reject(11,bytes((raw,)))
        for index,(ch,mode) in enumerate(elements):
            metadata=tlv(2,struct.pack('<H',ch))+tlv(64,bytes((index,)))
            if mode>6:raise Reject(11,b'\0'+metadata)
            if ch not in self.plans.get(fn,[]):raise Reject(4,metadata)
        for index,(ch,mode) in enumerate(elements):
            chosen=drive[index][1] if index in drive else self.idle[ch][1]
            if chosen==255:chosen=0
            if mode not in (3,4):chosen=0
            self.pins[ch]=(mode,chosen);self.events.append(('set',ch,mode,chosen))
        return b''
    def level(self,ch):
        mode,_=self.pins[ch]
        return 1 if mode in (1,4) else 0 if mode in (2,3,5) else self.external[ch]


class GpioModel(ResourceModel):
    def __init__(self,*args,selectable=True,**kwargs):
        super().__init__(*args,**kwargs);self.selectable=selectable;self.install()
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',8)),)
    def install(self):self.extension=Gpio(self.selectable);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            else:e.fixed={16:1+3*n,17:1+2*n};e.free={17}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def restart_fixture(self):
        if self.ep.current_core.holder is not None:raise ValueError('logical restart while owned')
        boot=(self.ep.boot_id+1)&0xffffffff;self.ep.reboot(boot);self.install();return {'scope':'logical-model','boot_id':boot}
    def gpio_state(self):
        e=self.extension;return {'plans':{fn:list(chs) for fn,chs in e.plans.items()},'pins':dict(e.pins),'idle':dict(e.idle),'events':list(e.events)}
