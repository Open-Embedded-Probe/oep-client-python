"""Independent ADC fixture using explicit raw rows; no physical sampling claims.

Shares the finite capture lifecycle of the independent logic model, not the
legacy virtual bench sampler or Host capture decoder.
"""
import struct
from fractions import Fraction
from .virtual_logic_model import Logic, LogicModel
from .virtual_core import Core, Reject, tlv


def refuse_output_idle(idle,channels):
    """An analog plan onto a channel whose idle is an output (mode 3 / 4): rejected unavailable cause 5 naming the
    channel, before anything changes (capture §1.2, plan §2.4)."""
    for ch in channels:
        if idle.get(ch) in (3,4):raise Reject(4,tlv(1,b'\5')+tlv(2,struct.pack('<H',ch)))


class Analog(Logic):
    def __init__(self,ep):
        super().__init__(ep);self.calibration_ns=None;self.factory_raw=b'\x12\x34'
        self.idle={ch:0 for ch in range(8)};self.pins=dict(self.idle)   # the analog channels' idle is no output (capture §1.2)
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in
              ((1,b'oep.probe.plan'),(2,b'oep.fixture.analog'))]
        page=bytearray(struct.pack('<HB',2,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(fn)
        return (tlv(7,b'\x10\xf7\x03'),*(tlv(1,bytes((role,0,0,3))) for role in (1,3)),
                tlv(5,b'\0'*4),tlv(64,struct.pack('<BII',1,16,0)),tlv(64,struct.pack('<BII',2,16,2)),
                tlv(65,struct.pack('<II',100,1000)),tlv(68,b'\2'),tlv(69,struct.pack('<II',25,8)),
                tlv(70,struct.pack('<Bii',0,0,3300)),tlv(70,struct.pack('<Bii',1,-1000,1000)))
    def plan(self,op,payload,tags):
        if op==17:return super().plan(op,payload,tags)
        rows=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags]
        if len(set((fn,role,ch) for _,fn,role,ch in rows))!=len(rows):raise Reject(3)
        for raw,fn,role,ch in rows:
            if fn==0:raise Reject(3)
            if fn not in self.functions:raise Reject(1)
            if fn!=2 or role not in (1,3) or ch not in (0,1):raise Reject(11,bytes((raw,)))
        if not rows:return b''
        if len({r[2] for r in rows})!=len(rows) or len({r[3] for r in rows})!=len(rows):raise Reject(11,bytes((rows[0][0],)))   # one channel per role: unsupported (plan §2.4)
        refuse_output_idle(self.idle,(r[3] for r in rows))
        if sorted((r[2],r[3]) for r in rows)==sorted(zip(self.roles,self.plans.get(2,[]))):return b''   # the same set: nothing (plan §2.1)
        if self.state in (2,3,5):raise Reject(4,tlv(1,b'\6'))           # running: a change is cause 6 (capture §3.2)
        self.release_plans([2]);rows.sort(key=lambda r:r[2]);self.roles=[r[2] for r in rows];self.plans[2]=[r[3] for r in rows]
        return b''
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        got={};seen=set();frontends=[];later=[]
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            value,data=data[3:3+size],data[3+size:]
            known=op in (16,24) and key in (64,66,67,68,69,70,71)
            if known and size!=({64:1,69:6,71:2}.get(key,4)):raise Reject(3)   # a later copy too (core §2.3)
            if not known:                                  # every copy of an unknown tag: critical -> unsupported (core §2.3)
                if raw&128:raise Reject(11,bytes((raw,)))
                continue
            if key in seen and key!=71:later.append((key,raw,value));continue   # a later copy: checked in settings (core §2.3)
            seen.add(key)
            if key==71:frontends.append((raw,value))
            else:got[key]=(raw,value)
        got[71]=frontends;got['later']=later
        return got
    def settings(self,tags):
        if not self.roles:raise Reject(4,tlv(1,b'\6'))
        if not all(k in tags for k in (64,66)):raise Reject(3)
        mode=tags[64][1][0];rate=struct.unpack('<I',tags[66][1])[0]
        if rate==0 or any(k in tags and struct.unpack('<I',tags[k][1])[0]==0 for k in (67,68)):raise Reject(3)
        if mode in (1,2) and 67 not in tags:raise Reject(3)
        later=tags.get('later',[])                         # every later copy is checked too, the first used (core §2.3)
        if any(k in (66,67,68) and not struct.unpack('<I',v)[0] for k,_,v in later):raise Reject(3)
        for key in (67,68,70):
            if key in tags and ((key==67 and mode==3) or (key==68 and mode!=2) or (key==70 and (69 not in tags or tags[69][1][0]==0))):raise Reject(11,bytes((tags[key][0],)))
        if mode not in (1,2):raise Reject(11,bytes((tags[64][0],)))
        if not 100<=rate<=1000:raise Reject(11,bytes((tags[66][0],)))
        trigger=struct.unpack('<BBI',tags[69][1]) if 69 in tags else (0,0,0)
        if trigger[0] not in (0,3,4) or (trigger[0] and trigger[2]>4095):raise Reject(11,bytes((tags[69][0],)))
        for k,raw,v in later:                              # a value this probe does not handle: that copy's tag as received
            t=struct.unpack('<BBI',v) if k==69 else None
            if (k==64 and v[0] not in (1,2) or k==66 and not 100<=struct.unpack('<I',v)[0]<=1000 or k==70 and struct.unpack('<I',v)[0]>8
                    or t is not None and (t[0] not in (0,3,4) or (t[0] and t[2]>4095))):raise Reject(11,bytes((raw,)))
        if trigger[0] and trigger[1] not in self.roles:raise Reject(4,tlv(1,b'\6'))
        samples=min(16,struct.unpack('<I',tags[67][1])[0]);segments=min(2,struct.unpack('<I',tags[68][1])[0]) if 68 in tags else 2 if mode==2 else 1
        pre=struct.unpack('<I',tags[70][1])[0] if 70 in tags else 0
        if pre>8 or pre>=samples:raise Reject(11,bytes((tags[70][0],)))
        frontend={role:0 for role in self.roles};seen=set()
        for raw,value in tags[71]:
            role,fe=value
            if role in seen:raise Reject(3)
            seen.add(role)
            if role not in self.roles:raise Reject(11,bytes((raw,)))   # capture §3.3: an unlisted refusal is unsupported
            if fe not in (0,1):raise Reject(11,bytes((raw,)))
            frontend[role]=fe
        return dict(mode=mode,rate=rate,samples=samples,segments=segments,trigger=trigger,pretrigger=pre,frontend=frontend)
    def answer(self,c):
        rows=[tlv(80,struct.pack('<II',c['rate'],1)),tlv(81,bytes((16,2,12,len(self.roles),*reversed(self.roles)))),
              tlv(82,struct.pack('<I',c['samples'])),tlv(89,struct.pack('<BIB',2,1100,0))]
        if c['mode']==2:rows.append(tlv(83,struct.pack('<I',c['segments'])))
        for role in self.roles:rows.extend((tlv(85,struct.pack('<Bii',role,-17,-1000000 if c['frontend'][role] else 1000000)),
                                           tlv(87,struct.pack('<BI',role,0)),tlv(88,bytes((role,c['frontend'][role])))))
        return b''.join(rows)
    def finish(self,short=False):
        if not self.current:return
        data=b''.join(struct.pack('<H',(row[self.roles.index(role)]<<2)|0xc003) for row in self.current for role in reversed(self.roles))
        next_ns=self.start_ns+len(self.current)*1000000000//self.config['rate']
        position=self.base+len(self.data);flags=int(self.gap)|(2 if short else 0);self.data.extend(data)
        self.segments.append((self.serial,position,len(self.current),self.start_ns,1000000,self.trigger_index,flags,self.generation))
        self.serial=(self.serial+1)&0xffffffff;self.current=[];self.gap=False;self.trigger_index=0xffffffff;self.start_ns=next_ns
        if self.config['mode']==1:self.state=4
        elif len(self.segments)>=self.config['segments']:self.state=5
    def dispatch_request(self,fn,op,payload,tags):
        if fn==2 and op==16 and self.state in (2,3,5):raise Reject(4,tlv(1,b'\6'))
        if fn==2 and op==25:
            scheme=b'org.example.adc';out=tlv(1,bytes((255,len(scheme)))+scheme+struct.pack('<H',len(self.factory_raw))+self.factory_raw)
            if self.calibration_ns is not None:out+=tlv(2,struct.pack('<IQI',1365,self.calibration_ns,1100))
            if 5+len(out)>self.ep.probe.max_frame:raise Reject(11,b'\0')
            return out
        out=super().dispatch_request(fn,op,payload,tags)
        if fn==2 and op==17:self.calibration_ns=self.ep.now_ns()
        return out
    def feed(self,samples):
        accepted=0
        for sample in samples:
            if self.state not in (2,3):break
            accepted+=1
            if self.state==2:
                kind,role,value=self.config['trigger'];raw=sample[self.roles.index(role)];previous=self.previous
                hit=previous is not None and ((kind==3 and previous<value<=raw) or (kind==4 and previous>value>=raw))
                self.previous=raw
                if not hit:
                    self.history.append(sample);self.history=self.history[-self.config['pretrigger']:] if self.config['pretrigger'] else []
                    continue
                self.state=3;self.current=list(self.history);self.trigger_index=len(self.current)
                self.start_ns=max(0,self.ep.now_ns()-self.trigger_index*1000000000//self.config['rate']);self.history=[]
            self.current.append(sample);target=self.config['samples']
            if self.trigger_index!=0xffffffff:target-=self.config['pretrigger']-self.trigger_index
            if len(self.current)>=target:self.finish()
        return accepted


class AnalogModel(LogicModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.ep.probe.max_frame=256
    def install(self):self.extension=Analog(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            else:e.fixed={16:0,17:0,18:0,20:0,21:14,22:4,23:8,24:0,25:0};e.free={20,21,22,24,25}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def analog_state(self):return dict(self.logic_state(),calibration_ns=self.extension.calibration_ns,factory_raw=self.extension.factory_raw,now_ns=self.ep.now_ns())
    def analog_factory(self,size):
        if type(size)is not int or not 0<=size<=4096:raise ValueError('explicit 0..4096 factory raw byte count required')
        self.extension.factory_raw=bytes(i*7&255 for i in range(size));return self.extension.factory_raw
    def analog_feed(self,samples):
        if type(samples)is not list or len(samples)>128 or any(type(row)is not list or len(row)!=len(self.extension.roles) or any(type(value)is not int or not 0<=value<=4095 for value in row) for row in samples):raise ValueError('explicit finite u12 raw samples in increasing plan role order required')
        accepted=0
        for sample in samples:
            count=self.extension.feed([sample]);accepted+=count
            if not count:break
            self.sample_ns+=Fraction(1000000000,self.extension.config['rate']);self.milliseconds=int(self.sample_ns//1000000)
        return accepted
    def analog_advance(self,ms):return self.logic_advance(ms)
    def analog_fail(self,error):return self.logic_fail(error)
    def analog_seed(self,generation):return self.logic_seed(generation)
