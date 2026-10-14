"""Independent logical I2C target fixture, completed transactions only."""
import copy
import struct
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Core, Reject, Completed, tlv


class I2c(Gpio):
    functions=(1,2)
    def __init__(self,ep,stretch=True,pullups=True):
        super().__init__();self.ep=ep;self.stretch_available=stretch;self.internal_pullups=pullups;self.idle={ch:0 for ch in range(8)};self.pins=dict(self.idle);self.fail_next=False;self.reset()
    def reset(self):self.configured=False;self.address=None;self.rx=[];self.tx=[];self.rx_frames=0;self.errors=0;self.stretch_us=0;self.pullups=False
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in ((1,b'oep.probe.plan'),(2,b'oep.fixture.i2c-target'))];page=bytearray(struct.pack('<HB',2,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(1)
        rows=(tlv(7,bytes((16,31 if self.stretch_available else 15))),tlv(1,b'\1\0\0\3'),tlv(1,b'\2\0\0\3'),tlv(2,struct.pack('<I',400000)),tlv(3,struct.pack('<H',8)),tlv(5,struct.pack('<I',4 if self.internal_pullups else 0)),tlv(64,b'\3'))
        return rows+((tlv(65,struct.pack('<I',1000)),) if self.stretch_available else ())
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        seen=set()
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            data=data[3+size:]
            if key in seen:continue
            seen.add(key)
            if raw&128:raise Reject(11,bytes((raw,)))
        return []
    def release_plans(self,functions):
        if 2 in functions:self.reset();self.events.append(('i2c-stop',))
        super().release_plans(functions)
    def plan(self,op,payload,tags):
        if op==17:
            n=payload[0];fns=list(struct.unpack('<'+'H'*n,payload[1:]));self.release_plans(fns if n else list(self.plans));return b''
        assignments=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags];seen=set()
        for raw,fn,role,ch in assignments:
            if fn==0 or (fn,role,ch) in seen:raise Reject(3)
            seen.add((fn,role,ch))
        for raw,fn,role,ch in assignments:
            if fn not in self.functions:raise Reject(1)
            if fn!=2 or role not in (1,2) or ch not in (0,1):raise Reject(11,bytes((raw,)))
        if not assignments:return b''
        if len(assignments)!=2 or {r for _,_,r,_ in assignments}!={1,2} or len({ch for _,_,_,ch in assignments})!=2:raise Reject(11,bytes((assignments[0][0],)))   # each role once, distinct channels: unsupported (fixture §3/§4)
        channels=[row[3] for row in sorted(assignments,key=lambda row:row[2])]
        if channels==self.plans.get(2):return b''                       # the same set: nothing changes (plan §2.1)
        if self.configured:raise Reject(4,tlv(1,b'\6'))                # state 1: a change is cause 6
        self.release_plans([2]);self.plans[2]=[ch for _,_,_,ch in sorted(assignments,key=lambda row:row[2])];return b''
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if op==19:return struct.pack('<BBIBI',int(self.configured),min(255,len(self.rx)),self.rx_frames,min(255,len(self.tx)),self.errors)
        if op==20:
            value=struct.unpack('<I',payload)[0]
            if value>1000:raise Reject(11,b'\0')
            self.stretch_us=value;return b''
        if op==16:
            address=payload[0]
            if address>127:raise Reject(3)
            if address<8 or address>=120:raise Reject(11,b'\0')
            if 2 not in self.plans:raise Reject(4,tlv(1,b'\6'))
            if self.fail_next:                                     # taken, not brought up: state 0, queues and counters
                self.fail_next=False;stretch=self.stretch_us;self.reset();self.stretch_us=stretch   # gone, stretch kept
                for ch in self.plans[2]:self.pins[ch]=self.idle[ch]
                self.events.append(('configure-failed',address));return Completed(b'',1)
            stretch=self.stretch_us;self.reset();self.stretch_us=stretch;self.configured=True;self.address=address;self.pullups=self.internal_pullups
            for ch in self.plans[2]:self.pins[ch]=1 if self.pullups else 6
            self.events.append(('configure',address));return b''
        if not self.configured:raise Reject(4,tlv(1,b'\6'))
        if op==17:
            if not self.rx:return b'\0\0\0'
            data,ns=self.rx.pop(0);return struct.pack('<BH',min(255,len(self.rx)),len(data))+data+tlv(1,struct.pack('<Q',ns))
        count=struct.unpack_from('<H',payload)[0]
        if count==0:raise Reject(3)
        if count>8:raise Reject(11,b'\0')
        if len(self.tx)>=3:raise Reject(4,tlv(1,b'\2'))
        self.tx.append(payload[2:]);return b''
    def write(self,address,data,end):
        ack=self.configured and address==self.address;ns=self.ep.now_ns();result={'ack':ack,'acked_bytes':len(data) if ack else 0,'stretches':[self.stretch_us]*len(data) if ack and self.stretch_us else [],'ended_ns':ns}
        self.events.append(('write',address,data,end,ack,ns))
        if not ack or not data:return result
        overflow=len(self.rx)>=3;long=len(data)>8
        if overflow or long:self.errors=min(self.errors+1,0xffffffff)
        if not overflow:self.rx.append((data[:8],ns));self.rx_frames=min(self.rx_frames+1,0xffffffff)
        return result
    def read(self,address,count):
        ack=self.configured and address==self.address;data=b''
        if ack:
            slot=self.tx.pop(0) if self.tx else b'';data=slot[:count].ljust(count,b'\xff')
        self.events.append(('read',address,count,ack,data));return {'ack':ack,'data':data,'stretches':[self.stretch_us] if ack and self.stretch_us else []}


class I2cModel(GpioModel):
    def __init__(self,boot_id=None,*,stretch=True,pullups=True):
        self.milliseconds=0;self.i2c_stretch=stretch;self.i2c_pullups=pullups;super().__init__(lambda:self.milliseconds,boot_id=boot_id)
    def install(self):self.extension=I2c(self.ep,self.i2c_stretch,self.i2c_pullups);self.ep.current_core=Core(self.ep,self.extension)
    def i2c_fail_configure(self):self.extension.fail_next=True   # the next accepted configure does not come up
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            else:
                e.fixed={16:1,17:0,18:2,19:0};e.free={19}
                if e.stretch_available:e.fixed[20]=4
                if op==18 and len(request)>=12:e.fixed[18]=2+struct.unpack_from('<H',request,10)[0]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def i2c_state(self):
        e=self.extension;c=self.ep.current_core
        return copy.deepcopy({'plans':e.plans,'pins':e.pins,'idle':e.idle,'events':e.events,'configured':e.configured,'address':e.address,'rx':e.rx,'tx':e.tx,'rx_frames':e.rx_frames,'errors':e.errors,'stretch_us':e.stretch_us,'pullups':e.pullups,'clock_scope':'explicit-logical-ms','now_ms':self.milliseconds,'holder':c.holder,'deadline':c.deadline})
    def i2c_write(self,address,data,end):
        if type(address)is not int or not 0<=address<=127 or not isinstance(data,bytes) or end not in ('stop','start'):raise ValueError('explicit 7-bit address, bytes, STOP/next START required')
        return self.extension.write(address,data,end)
    def i2c_read(self,address,count):
        if type(address)is not int or not 0<=address<=127 or type(count)is not int or not 1<=count<=32:raise ValueError('explicit 7-bit address and finite positive read count required')
        return self.extension.read(address,count)
    def i2c_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit 0..60000 logical ms required')
        self.milliseconds+=ms
