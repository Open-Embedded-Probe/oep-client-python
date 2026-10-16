"""Independent logical I2C target fixture, completed transactions only."""
import copy
import struct
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Core, Reject, Completed, tlv
from . import registry as _reg
from .catalog import pack_ops
from .virtual_names import R, DC, cause

# oep.fixture.i2c-target's numbers by name (fixture §3)
_I = _reg.FIXTURE_I2C_TARGET
OP, ROLE, FEAT, D = _I.op, _I.enum["role"], _I.enum["features"], _I.tlv["describe"]
PLAN = _reg.PROBE_PLAN.op
MODE = _reg.FIXTURE_GPIO.enum["mode"]                     # the pins' states the model shows
UNSUPPORTED_FIXED = b'\0'                                 # the unsupported payload's fixed-part marker (core §4.3)


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
        ops=pack_ops({OP[k] for k in ('configure','read_rx','preload_tx','status')}|({OP['stretch']} if self.stretch_available else set()))
        rows=(tlv(DC['ops'],ops),tlv(DC['role_channels'],bytes((ROLE['sda'],0,0,3))),tlv(DC['role_channels'],bytes((ROLE['scl'],0,0,3))),tlv(DC['max_clock_hz'],struct.pack('<I',400000)),tlv(DC['max_length'],struct.pack('<H',8)),tlv(DC['features'],struct.pack('<I',FEAT['internal_pullups'] if self.internal_pullups else 0)),tlv(D['queue_depth'],b'\3'))
        return rows+((tlv(D['max_stretch_us'],struct.pack('<I',1000)),) if self.stretch_available else ())
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        while data:
            if len(data)<3:raise Reject(R['malformed'])
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(R['malformed'])
            data=data[3+size:]
            if raw&128:raise Reject(R['unsupported'],bytes((raw,)))   # no request TLV defined: every copy, a later one too (core §2.3)
        return []
    def release_plans(self,functions):
        if 2 in functions:self.reset();self.events.append(('i2c-stop',))
        super().release_plans(functions)
    def plan(self,op,payload,tags):
        if op==PLAN['plan_release']:
            n=payload[0];fns=list(struct.unpack('<'+'H'*n,payload[1:]))
            if any(fn and fn not in self.functions for fn in fns):raise Reject(R['unknown_function'])   # every fn first; fn 0 has no plan (plan §2.2)
            self.release_plans(fns if n else list(self.plans));return b''
        assignments=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags];seen=set()
        for raw,fn,role,ch in assignments:
            if fn==0 or (fn,role,ch) in seen:raise Reject(R['malformed'])
            seen.add((fn,role,ch))
        for raw,fn,role,ch in assignments:
            if fn not in self.functions:raise Reject(R['unknown_function'])
            if fn!=2 or role not in ROLE.values() or ch not in (0,1):raise Reject(R['unsupported'],bytes((raw,)))
        if not assignments:return b''
        if len(assignments)!=2 or {r for _,_,r,_ in assignments}!=set(ROLE.values()) or len({ch for _,_,_,ch in assignments})!=2:raise Reject(R['unsupported'],bytes((assignments[0][0],)))   # each role once, distinct channels: unsupported (fixture §3/§4)
        channels=[row[3] for row in sorted(assignments,key=lambda row:row[2])]
        if channels==self.plans.get(2):return b''                       # the same set: nothing changes (plan §2.1)
        if self.configured:raise Reject(R['unavailable'],cause('wrong_state'))   # state 1: a change is cause 6
        self.release_plans([2]);self.plans[2]=[ch for _,_,_,ch in sorted(assignments,key=lambda row:row[2])];return b''
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if op==OP['status']:return struct.pack('<BBIBI',int(self.configured),min(255,len(self.rx)),self.rx_frames,min(255,len(self.tx)),self.errors)
        if op==OP['stretch']:
            value=struct.unpack('<I',payload)[0]
            if value>1000:raise Reject(R['unsupported'],UNSUPPORTED_FIXED)
            self.stretch_us=value;return b''
        if op==OP['configure']:
            address=payload[0]
            if address>127:raise Reject(R['malformed'])
            if address<8 or address>=120:raise Reject(R['unsupported'],UNSUPPORTED_FIXED)
            if 2 not in self.plans:raise Reject(R['unavailable'],cause('wrong_state'))
            if self.fail_next:                                     # taken, not brought up: state 0, queues and counters
                self.fail_next=False;stretch=self.stretch_us;self.reset();self.stretch_us=stretch   # gone, stretch kept
                for ch in self.plans[2]:self.pins[ch]=self.idle[ch]
                self.events.append(('configure-failed',address));return Completed(b'',1)
            stretch=self.stretch_us;self.reset();self.stretch_us=stretch;self.configured=True;self.address=address;self.pullups=self.internal_pullups
            for ch in self.plans[2]:self.pins[ch]=MODE['input_pullup'] if self.pullups else MODE['open_drain_release']
            self.events.append(('configure',address));return b''
        if not self.configured:raise Reject(R['unavailable'],cause('wrong_state'))
        if op==OP['read_rx']:
            if not self.rx:return b'\0\0\0'
            data,ns=self.rx.pop(0);return struct.pack('<BH',min(255,len(self.rx)),len(data))+data+tlv(_I.tlv['read_rx_answer']['ns'],struct.pack('<Q',ns))
        count=struct.unpack_from('<H',payload)[0]
        if count==0:raise Reject(R['malformed'])
        if count>8:raise Reject(R['unsupported'],UNSUPPORTED_FIXED)
        if len(self.tx)>=3:raise Reject(R['unavailable'],cause('limit'))
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
            if fn==1:e.fixed={PLAN['plan_apply']:0,PLAN['plan_release']:1+2*n};e.free=set()
            else:
                e.fixed={OP['configure']:1,OP['read_rx']:0,OP['preload_tx']:2,OP['status']:0};e.free={OP['status']}
                if e.stretch_available:e.fixed[OP['stretch']]=4
                if op==OP['preload_tx'] and len(request)>=12:e.fixed[OP['preload_tx']]=2+struct.unpack_from('<H',request,10)[0]
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
