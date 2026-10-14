"""Independent logic capture with explicit samples and no acquisition timing claims."""
import copy
import struct
from fractions import Fraction
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Core, Reject, tlv


class Logic(Gpio):
    functions=(1,2)
    def __init__(self,ep):
        super().__init__();self.ep=ep;self.generation=0;self.roles=[];self.config=None;self.clear(0)
    def clear(self,state):
        self.state=state;self.data=bytearray();self.base=0;self.segments=[];self.serial=0;self.flags=0;self.error=0;self.current=[];self.history=[];self.previous=None;self.gap=False;self.trigger_index=0xffffffff;self.start_ns=0
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in ((1,b'oep.probe.plan'),(2,b'oep.fixture.logic'))];page=bytearray(struct.pack('<HB',2,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(1)
        return (tlv(7,b'\x10\xf7\x01'),*(tlv(1,bytes((role,0,0,7))) for role in range(3)),tlv(5,b'\0'*4),tlv(64,struct.pack('<BII',1,16,1)),tlv(64,struct.pack('<BII',2,16,2)),tlv(65,struct.pack('<IIB',100,1000,1)),tlv(68,b'\3'),tlv(69,struct.pack('<II',7,8)))
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        got={};seen=set()
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            value,data=data[3:3+size],data[3+size:]
            if key in seen:continue
            seen.add(key)
            if op in (16,24) and key in (64,66,67,68,69,70):
                if size!=({64:1,69:6}.get(key,4)):raise Reject(3)
                got[key]=(raw,value)
            elif raw&128:raise Reject(11,bytes((raw,)))
        return got
    def release_plans(self,functions):
        if 2 in functions:self.config=None;self.roles=[];self.clear(0)
        super().release_plans(functions)
    def plan(self,op,payload,tags):
        if op==17:
            n=payload[0];fns=list(struct.unpack('<'+'H'*n,payload[1:]));self.release_plans(fns if n else list(self.plans));return b''
        rows=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags];seen=set()
        for raw,fn,role,ch in rows:
            if fn==0 or (fn,role,ch) in seen:raise Reject(3)
            seen.add((fn,role,ch))
        for raw,fn,role,ch in rows:
            if fn not in self.functions:raise Reject(1)
            if fn!=2 or role not in range(3) or ch not in range(3):raise Reject(11,bytes((raw,)))
        if not rows:return b''
        if len({row[2] for row in rows})!=len(rows) or len({row[3] for row in rows})!=len(rows):raise Reject(3)
        self.release_plans([2]);rows.sort(key=lambda row:row[2]);self.roles=[row[2] for row in rows];self.plans[2]=[row[3] for row in rows];return b''
    def settings(self,tags):
        if self.state in (2,3,5):raise Reject(4,tlv(1,b'\6'))
        if not all(key in tags for key in (64,66)):raise Reject(3)
        mode=tags[64][1][0];rate=struct.unpack('<I',tags[66][1])[0]
        if rate==0 or any(key in tags and struct.unpack('<I',tags[key][1])[0]==0 for key in (67,68)):raise Reject(3)
        if mode in (1,2) and 67 not in tags:raise Reject(3)
        for key in (67,68,70):
            if key in tags and ((key==67 and mode==3) or (key==68 and mode!=2) or (key==70 and (69 not in tags or tags[69][1][0]==0))):raise Reject(11,bytes((tags[key][0],)))
        if mode not in (1,2):raise Reject(11,bytes((tags[64][0],)))
        if not 100<=rate<=1000:raise Reject(11,bytes((tags[66][0],)))
        trigger=struct.unpack('<BBI',tags[69][1]) if 69 in tags else (0,0,0)
        if trigger[0] not in (0,1,2) or (trigger[0]==1 and trigger[2]>1) or (trigger[0]==2 and trigger[2]>2):raise Reject(11,bytes((tags[69][0],)))
        if not self.roles or (trigger[0] and trigger[1] not in self.roles):raise Reject(4,tlv(1,b'\6'))
        samples=min(16,struct.unpack('<I',tags[67][1])[0]);segments=min(2,struct.unpack('<I',tags[68][1])[0]) if 68 in tags else 2 if mode==2 else 1;pre=struct.unpack('<I',tags[70][1])[0] if 70 in tags else 0
        if pre>8 or pre>=samples:raise Reject(11,bytes((tags[70][0],)))
        return dict(mode=mode,rate=rate,samples=samples,segments=segments,trigger=trigger,pretrigger=pre)
    def answer(self,c):
        rows=[tlv(80,struct.pack('<II',c['rate'],1)),tlv(81,bytes((3,len(self.roles),*range(len(self.roles))))),tlv(82,struct.pack('<I',c['samples'])),tlv(86,b'\0'*4)]
        if c['mode']==2:rows.append(tlv(83,struct.pack('<I',c['segments'])))
        return b''.join(rows)
    def finish(self,short=False):
        if not self.current:return
        data=bytearray((len(self.current)*3+7)//8)
        for i,sample in enumerate(self.current):
            value=4 if len(self.roles)<3 else 0
            for k,bit in enumerate(sample):value|=bit<<k
            for k in range(3):data[(3*i+k)//8]|=((value>>k)&1)<<((3*i+k)%8)
        next_ns=self.start_ns+len(self.current)*1000000000//self.config['rate']
        position=self.base+len(self.data);flags=int(self.gap)|(2 if short else 0);self.data.extend(data)
        self.segments.append((self.serial,position,len(self.current),self.start_ns,1000000,self.trigger_index,flags,self.generation));self.serial=(self.serial+1)&0xffffffff;self.current=[];self.gap=False;self.trigger_index=0xffffffff;self.start_ns=next_ns
        if self.config['mode']==1:self.state=4
        elif len(self.segments)>=self.config['segments']:self.state=5
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if op in (16,24):
            config=self.settings(tags);answer=self.answer(config)
            if op==16:self.config=config;self.clear(1)
            return answer
        if op==20:return struct.pack('<BIQBI',self.state,self.serial,self.base+len(self.data),self.flags,self.generation)+(tlv(1,bytes((self.error,))) if self.state==6 else b'')
        if op==17:
            if self.state not in (1,4,6):raise Reject(4,tlv(1,b'\6'))
            self.clear(2 if self.config['trigger'][0] else 3);self.generation=(self.generation+1)&0xffffffff or 1;self.start_ns=self.ep.now_ns();return struct.pack('<II',0,self.generation)
        if op==18:
            previous=self.state
            if self.state==3:self.finish(True)
            if previous in (2,3,5,6):self.state=1;self.current=[];self.history=[]
            return b''
        if op==22:
            first=struct.unpack('<I',payload)[0]
            if first==self.serial or not self.segments:rows=[]
            else:
                index=next((i for i,row in enumerate(self.segments) if row[0]==first),0);rows=self.segments[index:]
            limit=(self.ep.probe.max_frame-7)//37;page=rows[:limit];return bytes((int(len(rows)>len(page)),len(page)))+b''.join(struct.pack('<IQIQIIBI',*row) for row in page)
        generation=struct.unpack_from('<I',payload)[0]
        if generation!=self.generation:raise Reject(4,tlv(1,b'\6'))
        if op==23:
            serial=struct.unpack_from('<I',payload,4)[0]
            if self.config and self.config['mode']==2:
                self.segments[:]=[row for row in self.segments if ((serial-row[0])&0xffffffff)>=0x80000000]
                new=self.segments[0][1] if self.segments else self.base+len(self.data);del self.data[:new-self.base];self.base=new
                if self.state==5 and len(self.segments)<self.config['segments']:self.state=3;self.gap=True;self.start_ns=self.ep.now_ns()
            return b''
        position,maximum=struct.unpack_from('<QI',payload,4);start=max(self.base,min(position,self.base+len(self.data)));count=min(maximum,self.base+len(self.data)-start,self.ep.probe.max_frame-18);part=bytes(self.data[start-self.base:start-self.base+count]);flags=(1 if start+count<self.base+len(self.data) else 0)|(2 if position<self.base else 0);return struct.pack('<QBI',start,flags,count)+part
    def feed(self,samples):
        accepted=0
        for sample in samples:
            if self.state not in (2,3):break
            accepted+=1
            if self.state==2:
                kind,role,value=self.config['trigger'];bit=sample[self.roles.index(role)];previous=self.previous
                hit=(kind==1 and bit==value) or (kind==2 and previous is not None and bit!=previous and (value==2 or bit==1-value))
                self.previous=bit
                if not hit:self.history.append(sample);self.history=self.history[-self.config['pretrigger']:] if self.config['pretrigger'] else [];continue
                self.state=3;self.current=list(self.history);self.trigger_index=len(self.current);self.start_ns=max(0,self.ep.now_ns()-self.trigger_index*1000000000//self.config['rate']);self.history=[]
            self.current.append(sample)
            target=self.config['samples']
            if self.trigger_index!=0xffffffff:target-=self.config['pretrigger']-self.trigger_index
            if len(self.current)>=target:self.finish()
        return accepted


class LogicModel(GpioModel):
    def __init__(self,boot_id=None):self.milliseconds=0;self.sample_ns=Fraction(0);super().__init__(lambda:self.milliseconds,boot_id=boot_id)
    def install(self):self.extension=Logic(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            else:e.fixed={16:0,17:0,18:0,20:0,21:16,22:4,23:8,24:0};e.free={20,21,22,24}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def logic_state(self):
        e=self.extension;return copy.deepcopy({'plans':e.plans,'pins':e.pins,'idle':e.idle,'roles':e.roles,'config':e.config,'state':e.state,'generation':e.generation,'data':bytes(e.data),'base':e.base,'segments':e.segments,'serial':e.serial,'current':e.current,'history':e.history,'flags':e.flags,'error':e.error,'clock_scope':'explicit-logical-ms'})
    def logic_feed(self,samples):
        roles=len(self.extension.roles)
        if type(samples)is not list or len(samples)>128 or any(type(row)is not list or len(row)!=roles or any(type(bit)is not int or bit not in (0,1) for bit in row) for row in samples):raise ValueError('explicit finite binary samples in plan role order required')
        accepted=0
        for sample in samples:
            count=self.extension.feed([sample]);accepted+=count
            if not count:break
            self.sample_ns+=Fraction(1000000000,self.extension.config['rate']);self.milliseconds=int(self.sample_ns//1000000)
        return accepted
    def logic_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit 0..60000 logical ms required')
        self.milliseconds+=ms;self.sample_ns+=ms*1000000
    def logic_fail(self,error):
        e=self.extension
        if type(error)is not int or error not in (1,2,3) or e.state not in (2,3):raise ValueError('active logical acquisition and defined error required')
        e.current=[];e.history=[];e.state=6;e.flags|=1;e.error=error
    def logic_seed(self,generation):
        if self.ep.current_core.holder is not None or self.extension.state!=0 or type(generation)is not int or not 0<=generation<=0xffffffff:raise ValueError('unowned state0 and explicit u32 generation seed required')
        self.extension.generation=generation
