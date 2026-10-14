"""Independent SPI target with explicit logical CS/SCK edges; no physical timing."""
import copy
import struct
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Core, Reject, Completed, tlv


class Spi(Gpio):
    functions=(1,2)
    def __init__(self,ep,lsb=True):
        super().__init__(False);self.ep=ep;self.lsb_available=lsb;self.idle={ch:(0,0) for ch in range(8)};self.pins=dict(self.idle);self.reset()
    def reset(self):
        self.configured=False;self.mode=0;self.bit_order=0;self.armed=None;self.rx=[];self.transactions=0;self.errors=0;self.active=False;self.sck=0;self.bits=[];self.output_index=0;self.ready=True
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in ((1,b'oep.probe.plan'),(2,b'oep.fixture.spi-target'))];page=bytearray(struct.pack('<HB',2,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(1)
        return (tlv(7,b'\x10\x0f'),*(tlv(1,bytes((role,0,0,15))) for role in range(1,5)),tlv(2,struct.pack('<I',1000000)),tlv(3,struct.pack('<H',8)),tlv(5,struct.pack('<I',int(self.lsb_available))),tlv(64,b'\3'),tlv(67,b'\0'*4))
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
        if 2 in functions:self.reset();self.events.append(('spi-stop',))
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
            if fn!=2 or role not in range(1,5) or ch not in range(4):raise Reject(11,bytes((raw,)))
        if not assignments:return b''
        if len(assignments)!=4 or {row[2] for row in assignments}!=set(range(1,5)) or len({row[3] for row in assignments})!=4:raise Reject(3)
        self.release_plans([2]);self.plans[2]=[row[3] for row in sorted(assignments,key=lambda row:row[2])];return b''
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if op==19:return struct.pack('<BBBBBII',int(self.configured),self.mode,self.bit_order,int(self.armed is not None),min(255,len(self.rx)),self.transactions,self.errors)
        if op==16:
            mode,order=payload
            if mode>3 or order>1:raise Reject(3)
            if order and not self.lsb_available:raise Reject(11,b'\0')
            if 2 not in self.plans:raise Reject(4,tlv(1,b'\6'))
            self.reset();self.configured=True;self.mode=mode;self.bit_order=order;self.sck=mode>>1
            for ch in self.plans[2]:self.pins[ch]=(0,0)
            self.events.append(('configure',mode,order));return b''
        if not self.configured:raise Reject(4,tlv(1,b'\6'))
        if op==18:
            if not self.rx:return b'\0'*7
            bits,data,ns=self.rx.pop(0);return struct.pack('<BIH',min(255,len(self.rx)),bits,len(data))+data+tlv(1,struct.pack('<Q',ns))
        length,count=struct.unpack_from('<HH',payload)
        if length==0 or count>length:raise Reject(3)
        if length>8:raise Reject(11,b'\0')
        if self.armed is not None:raise Reject(4,tlv(1,b'\6'))
        if not self.ready:return Completed(b'',1)
        self.armed=(length,payload[4:]);return b''
    def miso(self,index):
        if self.armed is None:return 0
        data=self.armed[1]
        return (data[index//8]>>(index%8 if self.bit_order else 7-index%8))&1 if index//8<len(data) else 0
    def drive(self,value):
        channel=self.plans[2][2];self.pins[channel]=(4 if value else 3,0);self.events.append(('miso',channel,value))
    def cs(self,active):
        self.active=active;self.events.append(('cs',int(active)))
        if active:
            self.bits=[];self.output_index=0
            if self.configured:self.drive(self.miso(0))
            return None
        ended_ns=self.ep.now_ns();count=len(self.bits)
        if self.configured:
            if count:
                self.transactions+=1
                if self.armed is None:self.errors+=1
                else:
                    length,_=self.armed;overflow=len(self.rx)>=3;long=count>8*length
                    if overflow or long:self.errors+=1
                    if not overflow:
                        data=bytearray(min(length,(count+7)//8))
                        for k,bit in enumerate(self.bits[:8*length]):data[k//8]|=bit<<(k%8 if self.bit_order else 7-k%8)
                        self.rx.append((min(count,0xffffffff),bytes(data),ended_ns))
                    self.armed=None
            channel=self.plans[2][2];self.pins[channel]=(0,0);self.events.append(('miso-hiz',channel))
        return ended_ns
    def clock(self,level,mosi):
        old=self.sck;self.sck=level;leading=level!=(self.mode>>1);sample=(leading and not self.mode&1) or (not leading and bool(self.mode&1))
        if not self.configured:sample=False
        if self.configured and self.mode&1 and leading:self.drive(self.miso(self.output_index))
        mode=self.pins[self.plans[2][2]][0] if self.configured else 0
        value=1 if mode==4 else 0 if mode==3 else None
        if sample:self.bits.append(mosi)
        if self.configured and not leading:
            self.output_index+=1
            if not self.mode&1:self.drive(self.miso(self.output_index))
        self.events.append(('edge',old,level,mosi,value,sample));return {'sample':sample,'miso':value,'level':level}


class SpiModel(GpioModel):
    def __init__(self,boot_id=None,*,lsb=True):
        self.milliseconds=0;self.spi_lsb=lsb;super().__init__(lambda:self.milliseconds,boot_id=boot_id)
    def install(self):self.extension=Spi(self.ep,self.spi_lsb);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            else:
                e.fixed={16:2,17:4,18:0,19:0};e.free={19}
                if op==17 and len(request)>=14:e.fixed[17]=4+struct.unpack_from('<H',request,12)[0]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def spi_state(self):
        e=self.extension;c=self.ep.current_core
        return copy.deepcopy({'plans':e.plans,'pins':e.pins,'idle':e.idle,'events':e.events,'configured':e.configured,'mode':e.mode,'bit_order':e.bit_order,'armed':e.armed,'rx':e.rx,'transactions':e.transactions,'errors':e.errors,'active':e.active,'sck':e.sck,'bits':e.bits,'ready':e.ready,'clock_scope':'explicit-logical-ms','now_ms':self.milliseconds,'holder':c.holder,'deadline':c.deadline})
    def spi_cs(self,active):
        if type(active)is not bool or active==self.extension.active:raise ValueError('explicit CS transition required')
        return self.extension.cs(active)
    def spi_clock(self,level,mosi):
        if type(level)is not int or level not in (0,1) or type(mosi)is not int or mosi not in (0,1) or not self.extension.active or level==self.extension.sck:raise ValueError('active CS and explicit binary SCK transition/MOSI required')
        return self.extension.clock(level,mosi)
    def spi_transfer(self,bits):
        if type(bits)is not list or len(bits)>256 or any(type(bit)is not int or bit not in (0,1) for bit in bits) or not self.extension.configured or self.extension.active:raise ValueError('configured idle fixture and finite explicit binary bits required')
        e=self.extension;self.spi_cs(True);miso=[];edges=[];cpol=e.mode>>1
        for bit in bits:
            for level in (1-cpol,cpol):
                result=self.spi_clock(level,bit)
                if result['sample']:miso.append(result['miso']);edges.append(level)
        ended_ns=self.spi_cs(False);return {'miso':miso,'sampling_edges':edges,'ended_ns':ended_ns}
    def spi_ready(self,value):
        if type(value)is not bool or self.extension.active:raise ValueError('explicit readiness and inactive CS required')
        self.extension.ready=value;self.extension.events.append(('ready',value))
    def spi_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit 0..60000 logical ms required')
        self.milliseconds+=ms
