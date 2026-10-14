"""Independent cross-rate capture-group fixture with a separate rational sample grid per track.

Self-contained ADC/logic track engine for capture a09fa45: one-shot and streaming,
immediate trigger only, no repeat/force. It does not reuse the legacy capture-group,
the bench sampler or a Host decoder. Notifications are queued for an explicit drain.
"""
import copy
import struct
from fractions import Fraction
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Completed, Core, Reject, tlv

NO_TIME=(1<<64)-1
NONE=0xffffffff
LAST=0xffffffff
UNCERTAINTY=1000
MAX_FRAME=96
STREAM_SAMPLES={2:12,3:5}   # probe-chosen streaming segment sizes (ADC splits across frames, logic pads)
ROLES={2:(1,3),3:(0,1)}
CHANNELS={2:(0,1),3:(2,3)}


def wave(fn,index):
    """Explicit fixture waveform, in increasing plan role order."""
    return [(17*index+3)&4095,(4095-29*index)%4096] if fn==2 else [index&1,(index//3)&1]


def refusal(cause,fn=None):
    return Reject(4,tlv(1,bytes((cause,)))+(tlv(5,struct.pack('<H',fn)) if fn is not None else b''))


class Track:
    def __init__(self,fn,ep,out):
        self.fn=fn;self.ep=ep;self.out=out;self.generation=0;self.roles=[];self.config=None
        self.subscribed=False;self.seq=0;self.fail_start=None;self.clear(0)
    def clear(self,state):
        self.state=state;self.data=bytearray();self.base=0;self.segments=[];self.serial=0;self.flags=0;self.error=0
        self.current=[];self.first=0;self.position=0;self.start_ns=0;self.sent=0;self.origin=None;self.next=0
    # layout: ADC s=16 o=2 b=12 order [3,1]; logic w=3 pos [0,1], bit 2 undefined
    def encode(self,rows):
        if self.fn==2:return b''.join(struct.pack('<H',(row[self.roles.index(role)]<<2)|0xc003) for row in rows for role in reversed(self.roles))
        out=bytearray((3*len(rows)+7)//8)
        for i,row in enumerate(rows):
            value=4|row[0]|(row[1]<<1)
            for k in range(3):out[(3*i+k)//8]|=((value>>k)&1)<<((3*i+k)%8)
        return bytes(out)
    def confirmed(self,n):return 4*n if self.fn==2 else 3*n//8
    def full(self,n):return 4*n if self.fn==2 else (3*n+7)//8
    def size(self):return self.config['samples'] if self.config['mode']==1 else STREAM_SAMPLES[self.fn]
    def write_pos(self):return self.position+(self.confirmed(len(self.current)) if self.state==3 else 0)
    def view(self):return bytes(self.data)+(self.encode(self.current)[:self.confirmed(len(self.current))] if self.state==3 else b'')
    def due(self):return self.origin+Fraction(self.next*1000000000,self.config['rate'])
    def grid_ns(self,index):return int(self.origin+Fraction(index*1000000000,self.config['rate']))
    def request_tags(self,op,data):
        got={};seen=set();frontends=[]
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            value,data=data[3:3+size],data[3+size:]
            if key in seen and key!=71:continue
            seen.add(key)
            if op in (16,24) and (key in (64,66,67,68,69,70) or (key==71 and self.fn==2)):
                if size!={64:1,69:6,71:2}.get(key,4):raise Reject(3)
                if key==71:frontends.append((raw,value))
                else:got[key]=(raw,value)
            elif raw&128:raise Reject(11,bytes((raw,)))
        got[71]=frontends;return got
    def settings(self,tags):
        if not self.roles:raise refusal(6)
        if not all(k in tags for k in (64,66)):raise Reject(3)
        mode=tags[64][1][0];rate=struct.unpack('<I',tags[66][1])[0]
        if rate==0 or any(k in tags and struct.unpack('<I',tags[k][1])[0]==0 for k in (67,68)):raise Reject(3)
        if mode==1 and 67 not in tags:raise Reject(3)
        if mode not in (1,3):raise Reject(11,bytes((tags[64][0],)))
        for key in (67,68,70):
            if key in tags and ((key==67 and mode==3) or key==68 or (key==70 and (69 not in tags or tags[69][1][0]==0))):raise Reject(11,bytes((tags[key][0],)))
        if not 100<=rate<=1000:raise Reject(11,bytes((tags[66][0],)))
        if 69 in tags and tags[69][1][0]!=0:raise Reject(11,bytes((tags[69][0],)))
        frontend={role:0 for role in self.roles};seen=set()
        for raw,(role,fe) in tags[71]:
            if role in seen:raise Reject(3)
            seen.add(role)
            if role not in self.roles:raise refusal(6)
            if fe not in (0,1):raise Reject(11,bytes((raw,)))
            frontend[role]=fe
        return dict(mode=mode,rate=rate,samples=min(16,struct.unpack('<I',tags[67][1])[0]) if mode==1 else 0,frontend=frontend)
    def answer(self,c):
        if self.fn==3:layout=bytes((3,len(self.roles),*range(len(self.roles))))
        else:layout=bytes((16,2,12,len(self.roles),*reversed(self.roles)))
        rows=[tlv(80,struct.pack('<II',c['rate'],1)),tlv(81,layout)]
        if c['mode']==1:rows.append(tlv(82,struct.pack('<I',c['samples'])))
        if self.fn==2:
            rows.append(tlv(89,struct.pack('<BIB',2,1100,0)))
            for role in self.roles:rows.extend((tlv(85,struct.pack('<Bii',role,-17,-1000000 if c['frontend'][role] else 1000000)),tlv(88,bytes((role,c['frontend'][role])))))
        return b''.join(rows)
    def event(self,kind,fixed):
        if self.subscribed:self.out.append(struct.pack('<BHHB',3,self.fn,self.seq,kind)+fixed);self.seq=(self.seq+1)&0xffff
    def begin(self,now,state=3):
        self.generation+=1;self.clear(state);self.origin=now;self.start_ns=now
    def arrive(self):
        self.current.append(wave(self.fn,self.next));self.next+=1
        if len(self.current)>=self.size():self.finish()
    def finish(self,short=False):
        if not self.current:return
        n=len(self.current);row=(self.serial,self.position,n,self.start_ns,UNCERTAINTY,NONE,2 if short else 0,self.generation)
        self.data.extend(self.encode(self.current));self.segments=(self.segments+[row])[-16:]
        self.serial=(self.serial+1)&0xffffffff;self.position+=self.full(n);self.first+=n;self.current=[];self.start_ns=self.grid_ns(self.first)
        if self.config['mode']==1:
            self.state=4;self.event(1,struct.pack('<IQIQIIBI',*row));self.event(2,struct.pack('<BBI',0,0,self.generation))
    def stop(self):
        previous=self.state
        if previous==3:self.finish(True)
        if previous in (2,3,5,6):self.state=1;self.current=[]
        if previous==3:self.flush();self.event(2,struct.pack('<BBI',1,0,self.generation))
    def fail(self,error):
        self.current=[];self.state=6;self.flags|=1;self.error=error;self.flush()
        self.event(2,struct.pack('<BBI',3,error,self.generation))
    def chunks(self):
        """Confirmed, unsent byte ranges, each inside one segment."""
        end=self.write_pos();rows=[(row[1],row[1]+self.full(row[2]),row[2],row[3]) for row in self.segments]
        if self.state==3 and self.current:rows.append((self.position,self.position+self.confirmed(len(self.current)),len(self.current),self.start_ns))
        for start,stop,samples,ns in rows:
            lo,hi=max(start,self.sent),min(stop,end)
            if lo<hi:yield start,lo,hi,samples,ns
    def flush(self):
        if not self.config or self.config['mode']!=3:return
        limit=self.ep.probe.max_frame-15-7-27;view=self.view()
        for start,lo,hi,samples,ns in list(self.chunks()):
            while lo<hi:
                part=view[lo-self.base:min(hi,lo+limit)-self.base]
                if self.subscribed:
                    info=struct.pack('<QIQI',start,samples,ns,UNCERTAINTY)
                    self.out.append(struct.pack('<BHHQH',4,self.fn,self.seq,lo,len(part))+part+tlv(1,struct.pack('<I',self.generation))+tlv(2,info));self.seq=(self.seq+1)&0xffff
                else:self.flags|=1
                lo+=len(part);self.sent=lo
        cut=min(self.sent,self.base+len(self.data))-self.base
        if cut>0:del self.data[:cut];self.base+=cut
    def read(self,payload):
        generation,position,maximum=struct.unpack('<IQH',payload)
        if generation!=self.generation:raise refusal(6)
        end=self.write_pos();low=min(self.sent if self.config and self.config['mode']==3 else self.base,end);gap=0
        if position>=end:return struct.pack('<QBH',end,0,0)
        if position<low:
            if low>=end:return struct.pack('<QBH',end,2,0)
            position=low;gap=2
        count=min(maximum,end-position,self.ep.probe.max_frame-5-11);part=self.view()[position-self.base:position-self.base+count]
        return struct.pack('<QBH',position,gap|int(position+count<end),count)+part
    def dispatch(self,op,payload,tags):
        if op in (16,24):
            if op==16 and self.state in (2,3,5):raise refusal(6)
            config=self.settings(tags);answer=self.answer(config)
            if op==16:self.config=config;self.clear(1)
            return answer
        if op==17:
            if self.config is None or self.state not in (1,4,6):raise refusal(6)
            if self.config['mode']==3 and not self.subscribed:raise refusal(6)
            if self.generation==LAST:raise refusal(2)
            self.begin(self.ep.now_ns())
            if self.fail_start is not None:
                error,self.fail_start=self.fail_start,None;self.fail(error);return Completed(struct.pack('<I',self.generation),1)
            return struct.pack('<I',self.generation)
        if op==18:self.stop();return b''
        if op==20:return struct.pack('<BIQBI',self.state,self.serial,self.write_pos(),self.flags,self.generation)+(tlv(1,bytes((self.error,))) if self.state==6 else b'')
        if op==21:return self.read(payload)
        if op==22:
            first=struct.unpack('<I',payload)[0];rows=self.segments
            index=next((i for i,row in enumerate(rows) if row[0]==first),None)
            rows=[] if first==self.serial else rows[index or 0:]
            limit=(self.ep.probe.max_frame-7)//37;page=rows[:limit]
            return bytes((int(len(rows)>len(page)),len(page)))+b''.join(struct.pack('<IQIQIIBI',*row) for row in page)
        if op==23:
            if struct.unpack_from('<I',payload)[0]!=self.generation:raise refusal(6)
            return b''
        if op==25:return b''
        if op==1:
            if struct.unpack('<HI',payload)!=(0,0):raise refusal(2)
            self.drop();self.subscribed=True;self.seq=0;return b''
        self.drop();self.subscribed=False;return b''
    def drop(self):self.out[:]=[f for f in self.out if struct.unpack_from('<H',f,1)[0]!=self.fn]


class TimelineGroup(Gpio):
    functions=(1,2,3,4)
    def __init__(self,ep):
        super().__init__();self.ep=ep;self.pending=[];self.tracks={fn:Track(fn,ep,self.pending) for fn in (2,3)}
        self.bound=[];self.generation=0;self.start_ns=NO_TIME
    def list_page(self,first,limit):
        names=[b'oep.probe.plan',b'oep.fixture.analog',b'oep.fixture.logic',b'oep.fixture.capture-group']
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in enumerate(names,1)];page=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(fn)
        if fn==4:return (tlv(7,b'\x10\x17'),tlv(64,b'\2\2\0\3\0'))
        ops=bytes((0,6,0,0b11110111,3 if fn==2 else 1))   # subscribe/unsubscribe, 0x10-0x12, 0x14-0x18 (+0x19 ADC)
        rows=[tlv(7,ops),*(tlv(1,bytes((role,0,0,3 if fn==2 else 12))) for role in ROLES[fn]),
              tlv(64,struct.pack('<BII',1,16,0)),tlv(64,struct.pack('<BII',3,STREAM_SAMPLES[fn],0)),
              tlv(65,struct.pack('<II',100,1000)),tlv(68,b'\2'),tlv(69,struct.pack('<II',1,0))]
        if fn==2:rows+=[tlv(70,struct.pack('<Bii',0,0,3300)),tlv(70,struct.pack('<Bii',1,-1000,1000))]
        return tuple(rows)
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        if fn in self.tracks:return self.tracks[fn].request_tags(op,data)
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data)
            if not raw&127 or len(data)<3+size:raise Reject(3)
            if raw&128:raise Reject(11,bytes((raw,)))
            data=data[3+size:]
        return {}
    def release(self):
        self.bound=[];self.release_plans(list(self.plans))
        for t in self.tracks.values():t.subscribed=False
        self.pending.clear()
    def release_plans(self,functions):
        for fn in functions:
            if fn in self.tracks:t=self.tracks[fn];t.roles=[];t.config=None;t.clear(0)
        super().release_plans(functions)
    def plan(self,op,payload,tags):
        if op==17:
            count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:])) or list(self.plans)
            for fn in functions:
                if fn in self.bound:raise refusal(4,fn)
            self.release_plans(functions);return b''
        rows=[(raw,*struct.unpack('<HBH',value)) for raw,value in tags]
        if len(set((fn,role,ch) for _,fn,role,ch in rows))!=len(rows):raise Reject(3)
        for raw,fn,role,ch in rows:
            if fn==0:raise Reject(3)
            if fn not in self.functions:raise Reject(1)
            if fn not in self.tracks or role not in ROLES[fn] or ch not in CHANNELS[fn]:raise Reject(11,bytes((raw,)))
        if len({(r[1],r[2]) for r in rows})!=len(rows) or len({r[3] for r in rows})!=len(rows):raise Reject(3)
        affected={r[1] for r in rows};changed=set()
        for fn in affected:
            selected=sorted((role,ch) for _,owner,role,ch in rows if owner==fn)
            if selected!=sorted(zip(self.tracks[fn].roles,self.plans.get(fn,[]))):
                if fn in self.bound:raise refusal(4,fn)
                if self.tracks[fn].state in (2,3,5):raise refusal(6,fn)
                changed.add(fn)
        self.release_plans(changed)
        for fn in changed:
            selected=sorted((role,ch) for _,owner,role,ch in rows if owner==fn)
            self.tracks[fn].roles=[role for role,ch in selected];self.plans[fn]=[ch for role,ch in selected]
        return b''
    def state(self):
        if not self.bound:return 0
        states=[self.tracks[fn].state for fn in self.bound]
        if 6 in states:return 6
        if self.start_ns!=NO_TIME and all(s==4 for s in states):return 4
        if any(s in (2,3,5) for s in states):return 3
        return 1
    def bind(self,payload):
        count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:]))
        if functions==self.bound:return b''
        if self.state()==3:raise refusal(6)
        if not count:self.bound=[];return b''
        if len(set(functions))!=len(functions):raise Reject(3,tlv(5,struct.pack('<H',functions[0])))
        for fn in functions:
            if fn not in self.tracks:raise Reject(11,b'\0'+tlv(5,struct.pack('<H',fn)))
        for fn in functions:
            t=self.tracks[fn]
            if t.config is None or t.state not in (1,4,6) or t.config['mode']!=self.tracks[functions[0]].config['mode']:raise refusal(6,fn)
        self.bound=functions;return b''
    def start(self):
        if not self.bound:raise refusal(6)
        for fn in self.bound:
            t=self.tracks[fn]
            if t.config is None or t.state not in (1,4,6) or (t.config['mode']==3 and not t.subscribed):raise refusal(6,fn)
        if self.generation==LAST:raise refusal(2)
        for fn in self.bound:
            if self.tracks[fn].generation==LAST:raise refusal(2,fn)
        now=self.ep.now_ns();self.generation+=1;self.start_ns=now;failed=None
        for fn in self.bound:self.tracks[fn].begin(now,1)
        for fn in self.bound:
            t=self.tracks[fn];t.state=3
            if t.fail_start is not None:
                error,t.fail_start=t.fail_start,None;t.fail(error);failed=fn;break
        if failed:
            for fn in self.bound:
                if self.tracks[fn].state==3:self.tracks[fn].stop()
        out=struct.pack('<QIB',now,self.generation,len(self.bound))+b''.join(struct.pack('<HI',fn,self.tracks[fn].generation) for fn in self.bound)
        return Completed(out,1) if failed else out
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if fn in self.tracks:
            if fn in self.bound and op in (16,17,18):raise refusal(4,fn)
            return self.tracks[fn].dispatch(op,payload,tags)
        if op==16:return self.bind(payload)
        if op==17:return self.start()
        if op==18:
            for member in self.bound:self.tracks[member].stop()
            return b''
        return struct.pack('<BQI',self.state(),self.start_ns,self.generation)
    def failure(self,fn,error):
        self.tracks[fn].fail(error)
        if fn in self.bound:
            for other in self.bound:
                if other!=fn:self.tracks[other].stop()


class CaptureTimelineModel(GpioModel):
    def __init__(self,boot_id=None):
        self.absolute_ns=Fraction(0);super().__init__(lambda:int(self.absolute_ns//1000000),boot_id=boot_id);self.ep.probe.max_frame=MAX_FRAME
    def install(self):
        self.ep._clock_ns=lambda:int(self.absolute_ns)
        self.extension=TimelineGroup(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            elif fn==4:e.fixed={16:1+2*n,17:0,18:0,20:0};e.free={20}
            else:e.fixed={1:6,2:0,16:0,17:0,18:0,20:0,21:14,22:4,23:8,24:0,**({25:0} if fn==2 else {})};e.free={20,21,22,24,25}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def timeline_state(self):
        e=self.extension
        return copy.deepcopy(dict(bound=e.bound,generation=e.generation,start_ns=e.start_ns,pending=len(e.pending),
          tracks={fn:dict(state=t.state,generation=t.generation,config=t.config,roles=t.roles,data=bytes(t.data),base=t.base,segments=t.segments,current=t.current,
                         flags=t.flags,error=t.error,subscribed=t.subscribed,sent=t.sent,origin=t.origin,next=t.next) for fn,t in e.tracks.items()},
          plans=e.plans,pins=e.pins,idle=e.idle,now_ns=self.ep.now_ns(),clock_scope='explicit-logical-ns'))
    def timeline_advance_to(self,ns):
        if type(ns)is not int or ns<self.ep.now_ns() or ns-self.ep.now_ns()>1000000000:raise ValueError('forward explicit boot-relative ns within one second required')
        target=self.ep._origin_ns+ns;e=self.extension;events=0
        while True:
            due={fn:self.ep._origin_ns+t.due() for fn,t in e.tracks.items() if t.state==3}
            core=self.ep.current_core
            expiry=max(self.absolute_ns,Fraction(core.deadline*1000000)) if core.holder is not None else None
            if expiry is not None and expiry<=target and (not due or expiry<=min(due.values())):
                self.absolute_ns=expiry;core.tick();continue
            if not due or min(due.values())>target:break
            when=min(due.values());self.absolute_ns=when
            for fn in sorted(fn for fn,time in due.items() if time==when):e.tracks[fn].arrive();events+=1
            if events>4096:raise ValueError('finite explicit timeline budget exceeded')
        self.absolute_ns=Fraction(target);return events
    def timeline_drain(self):
        e=self.extension
        for t in e.tracks.values():t.flush()
        frames=list(e.pending);e.pending.clear();return frames
    def timeline_fail(self,fn,error):
        e=self.extension
        if fn not in e.tracks or type(error)is not int or error not in (1,2,3) or e.tracks[fn].state!=3:raise ValueError('active track and defined error required')
        e.failure(fn,error)
    def timeline_fail_next_start(self,fn,error):
        e=self.extension
        if fn not in e.tracks or type(error)is not int or error not in (1,2,3) or e.tracks[fn].state in (2,3,5):raise ValueError('idle track and defined error required')
        e.tracks[fn].fail_start=error
    def timeline_seed(self,generation,adc,logic):
        e=self.extension
        if self.ep.current_core.holder is not None or e.bound or any(type(v)is not int or not 0<=v<=LAST for v in (generation,adc,logic)):raise ValueError('unowned explicit group/track u32 seeds required')
        e.generation=generation;e.tracks[2].generation=adc;e.tracks[3].generation=logic
