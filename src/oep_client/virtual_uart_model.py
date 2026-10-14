"""Explicit logical fixture UART with plan-owned stream and monotonic counters."""
import struct
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_stream_model import Stream
from .virtual_core import Core, Reject, Completed, tlv


class Uart(Gpio):
    functions=(1,2)
    clock_hz=1843200
    def __init__(self,ep):
        super().__init__();self.ep=ep;self.idle={ch:0 for ch in range(8)};self.pins=dict(self.idle)
        self.stream=None;self.position=0;self.serial=0;self.baud=115200;self.format=0
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in [(1,b'oep.probe.plan'),(2,b'oep.fixture.uart')]];page=bytearray(struct.pack('<HB',2,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(1)
        return (tlv(7,b'\x10\x7f'),tlv(1,b'\1\0\0\1'),tlv(1,b'\2\1\0\1'),tlv(64,b'\3\0\4\x11'))
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        result={}
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            value,data=data[3:3+size],data[3+size:]
            if key in result:continue
            if op==16 and key==1:
                if size!=1:raise Reject(3)
                result[key]=(raw,value)
            elif raw&128:raise Reject(11,bytes((raw,)))
        return result
    def close(self,reason):
        if self.stream is not None:
            self.stream.mark(9,reason);self.position=self.stream.position;self.serial=self.stream.serial;self.stream=None;self.events.append(('stream-close',reason))
        self.baud=115200;self.format=0
    def release_plans(self,functions,reason=1):
        if 2 in functions:self.close(reason)
        super().release_plans(functions)
    def release(self):self.release_plans(list(self.plans),2)
    def plan(self,op,payload,tags):
        if op==17:
            n=payload[0];fns=list(struct.unpack('<'+'H'*n,payload[1:]));self.release_plans(fns if n else list(self.plans));return b''
        assignments=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags];seen=set()
        for raw,fn,role,ch in assignments:
            if fn==0 or (fn,role,ch) in seen:raise Reject(3)
            seen.add((fn,role,ch))
        for raw,fn,role,ch in assignments:
            if fn not in self.functions:raise Reject(1)
            if fn!=2 or (role,ch) not in ((1,0),(2,1)):raise Reject(11,bytes((raw,)))
        if not assignments:return b''
        self.release_plans([2]);self.plans[2]=[ch for _,_,_,ch in assignments];self.stream=Stream(self.ep,self.position,self.serial)
        if 1 in self.plans[2]:self.pins[1]=4;self.events.append(('uart-tx-idle',1))
        return b''
    def actual(self,baud):
        if baud==0:return None
        divisor=max(1,min(65535,(self.clock_hz+baud//2)//baud));actual=(self.clock_hz+divisor//2)//divisor
        return actual if abs(actual-baud)*100<=baud*5 else None
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if op==22:return struct.pack('<IB',self.baud,self.format)
        if op==16:
            requested=struct.unpack('<I',payload)[0];raw,format_value=tags.get(1,(1,b'\0'));format_value=format_value[0]
            if format_value not in (0,4,17):raise Reject(11,bytes((raw,)))
            baud=self.actual(requested)
            if baud is None:raise Reject(11,b'\0')
            if self.stream is None:raise Reject(4,tlv(1,b'\6'))
            self.baud=baud;self.format=format_value;self.events.append(('configure',baud,format_value));return struct.pack('<I',baud)
        if self.stream is None:raise Reject(4,tlv(1,b'\6'))
        if op==21:
            if 1 not in self.plans[2]:raise Reject(4,tlv(1,b'\6'))
            count=struct.unpack_from('<H',payload)[0];accepted=min(count,4-len(self.stream.tx));self.stream.tx.extend(payload[2:2+accepted])
            return Completed(struct.pack('<H',accepted),0 if accepted==count else 2 if accepted else 1)
        return self.stream.dispatch(1,op-1,payload)


class UartModel(GpioModel):
    def __init__(self,boot_id=None):
        self.milliseconds=0;super().__init__(lambda:self.milliseconds,boot_id=boot_id)
    def install(self):self.extension=Uart(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            else:
                e.fixed={16:4,17:11,18:4,19:0,20:1,21:2,22:0};e.free={17,18,22}
                if op==21 and len(request)>=12:e.fixed[21]=2+struct.unpack_from('<H',request,10)[0]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def uart_state(self):
        e=self.extension;s=e.stream;core=self.ep.current_core
        return {'plans':{fn:list(chs) for fn,chs in e.plans.items()},'pins':dict(e.pins),'idle':dict(e.idle),'events':list(e.events),'live':s is not None,'baud':e.baud,'format':e.format,'position':s.position if s else e.position,'next_serial':s.serial if s else e.serial,'data':bytes(s.data) if s else b'','marks':list(s.marks) if s else [],'tx':bytes(s.tx) if s else b'','clock_scope':'explicit-logical-ms','now_ms':self.milliseconds,'holder':core.holder,'deadline':core.deadline}
    def uart_feed(self,data):
        if not isinstance(data,bytes) or self.extension.stream is None or 0 not in self.extension.plans[2]:raise ValueError('live RX plan and explicit bytes required')
        self.extension.stream.feed(data)
    def uart_error(self,detail):
        if type(detail)is not int or detail not in (2,3) or self.extension.stream is None or 0 not in self.extension.plans[2]:raise ValueError('live RX plan and framing/parity detail required')
        self.extension.stream.mark(5,detail)
    def uart_drain(self,count):
        if type(count)is not int or count<0 or self.extension.stream is None:raise ValueError('live stream and nonnegative logical drain required')
        s=self.extension.stream;data=bytes(s.tx[:count]);del s.tx[:count];return data
    def uart_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit 0..60000 logical milliseconds required')
        self.milliseconds+=ms
    def uart_seed(self,position,serial):
        if self.ep.current_core.holder is not None or self.extension.stream is not None or type(position)is not int or not 0<=position<=0xffffffffffffffff or type(serial)is not int or not 0<=serial<=0xffffffff:raise ValueError('unowned logical fixture and explicit u64/u32 seed required')
        self.extension.position=position;self.extension.serial=serial
    def uart_mark_external(self,kind,detail):
        if self.extension.stream is None or type(kind)is not int or not 1<=kind<=127 or type(detail)is not int or not 0<=detail<=255:raise ValueError('live stream and explicit mark kind/detail required')
        self.extension.stream.mark(kind,detail)
