"""Selected-SPEC cross-rate immediate group acquisition checks with an independent waveform oracle.

Self-contained for capture a09fa45: no imports of probe encoders, Host decoders,
the bench endpoint or the other capture checkers. Layouts are decoded from the
configure answers; the waveform and clock are the documented fixture's.
"""
import struct
from fractions import Fraction
from .conformance import require,tlvs
from .conformance_registered_ops import RegisteredOpsChecks

NO_TIME=(1<<64)-1
NONE=0xffffffff
EVENT,DATA=3,4   # notification roles (core §10.2)


def wave(fn,index):
    """Documented fixture waveform for sample index, in increasing plan role order."""
    return [(17*index+3)&4095,(4095-29*index)%4096] if fn==2 else [index&1,(index//3)&1]


def arrived(rate,elapsed,limit=None):
    """Samples on a grid starting at elapsed 0 (sample 0 at the start instant)."""
    count=0 if elapsed<0 else int(Fraction(elapsed)*rate//1000000000)+1
    return count if limit is None else min(limit,count)


class CaptureTimelineChecks:
    scenarios=tuple((mode,members,rates,offset) for mode in (1,3) for members in ((2,3),(3,2)) for rates in ((1000,333),(333,1000),(1000,500)) for offset in (0,1234567))
    steps=(2,9,23,41,58)   # streaming drain instants, ms after the group start
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    def context(self):
        expected={1:'oep.probe.plan',2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'}
        require(self.a.reset_scope=='logical-model' and self.a.selection==expected,'explicit timeline selection')
        base=RegisteredOpsChecks(self.c,expected);require(base.run()['status']=='passed','registered timeline group/track ops')
        self.codes={fn:{r['name']:r['code'] for r in base.specs[name]['op']} for fn,name in expected.items()}
        spec={fn:base.specs[expected[fn]] for fn in (2,3)}
        self.tags={fn:spec[fn]['tlv']['configure'] for fn in (2,3)};self.answers={fn:spec[fn]['tlv']['configure_answer'] for fn in (2,3)}
        self.error_tag={fn:spec[fn]['tlv']['status_answer']['error'] for fn in (2,3)};self.data_tags={fn:spec[fn]['tlv']['data'] for fn in (2,3)}
        self.events={fn:spec[fn]['event'] for fn in (2,3)};self.assignment=base.specs[expected[1]]['tlv']['plan_apply']['role_assignment']
        self.cause_tag=self.c.reg['core']['tlv']['unavailable_payload']['cause'];self.fn_tag=self.c.reg['core']['tlv']['unavailable_payload']['fn']
        require(self.a.state()['clock_scope']=='explicit-logical-ns','explicit timeline ns clock')
    @staticmethod
    def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value
    def req(self,fn,name,sid=0,payload=b''):return self.c.request(self.codes[fn][name],payload,fn=fn,session=sid)
    def ok(self,request):v=self.c.success(request);tlvs(v);return v
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'explicit new timeline boot')
        self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'timeline boot readback');self.layouts={}
    def plan(self,sid):
        rows=[(2,1,0),(2,3,1),(3,0,2),(3,1,3)]
        return self.ok(self.req(1,'plan_apply',sid,b''.join(self.tlv(self.assignment,struct.pack('<HBH',*r)) for r in rows)))
    def settings(self,fn,mode=1,rate=1000,samples=16):
        rows=[('mode',bytes((mode,))),('rate',struct.pack('<I',rate))]+([('samples',struct.pack('<I',samples))] if mode==1 else [])
        return b''.join(self.tlv(self.tags[fn][name],value) for name,value in rows)
    def answer(self,fn,payload,mode,rate,samples=16):
        rows=tlvs(payload);values={}
        for tag,value in rows:values.setdefault(tag,value)
        a=self.answers[fn];actual=values.get(a['actual_rate'],b'');layout=values.get(a['layout'],b'')
        require(len(actual)==8 and all(struct.unpack('<II',actual)) and Fraction(*struct.unpack('<II',actual))==rate,'explicit exact timeline rate')
        if mode==1:require(values.get(a['actual_samples'])==struct.pack('<I',samples),'explicit one-shot actual samples')
        roles=(1,3) if fn==2 else (0,1)
        if fn==3:
            require(len(layout)>=2 and layout[1]==2 and len(layout)==4,'logic layout shape for two roles')
            w,pos=layout[0],list(layout[2:]);require(1<=w<=128 and len(set(pos))==2 and all(p<w for p in pos),'logic layout w/pos bounds')
            return dict(bits=w,w=w,pos=pos,roles=roles)
        require(len(layout)==6 and layout[3]==2,'ADC layout shape for two roles')
        s,o,b,_=layout[:4];order=list(layout[4:])
        require(s in (8,16,32) and 1<=b<=31 and o+b<=s and sorted(order)==list(roles),'ADC layout s/o/b/order bounds')
        scales={value[0] for tag,value in rows if tag==a['scale'] and len(value)==9}
        require(scales==set(roles),'ADC scale for every planned role')
        return dict(bits=2*s,s=s,o=o,b=b,order=order,roles=roles)
    def configure(self,sid,fn,mode,rate,samples=16):
        p=self.c.success(self.req(fn,'configure',sid,self.settings(fn,mode,rate,samples)));self.layouts[fn]=self.answer(fn,p,mode,rate,samples);return p
    def confirmed(self,fn,n):return self.layouts[fn]['bits']*n//8
    def full(self,fn,n):return (self.layouts[fn]['bits']*n+7)//8
    def decode(self,fn,data):
        """Every sample whose defined bits lie inside data, from the segment's first byte."""
        layout=self.layouts[fn];rows=[]
        for i in range(len(data)*8//layout['bits']):
            if fn==3:rows.append([(data[(i*layout['w']+p)//8]>>((i*layout['w']+p)%8))&1 for p in layout['pos']])
            else:
                row=[0,0];size=layout['s']//8
                for m,role in enumerate(layout['order']):
                    frame=int.from_bytes(data[(2*i+m)*size:(2*i+m+1)*size],'little');row[layout['roles'].index(role)]=(frame>>layout['o'])&((1<<layout['b'])-1)
                rows.append(row)
        return rows
    def expect(self,fn,data,first,count,message):
        require(self.decode(fn,data)[:count]==[wave(fn,first+i) for i in range(count)] and len(self.decode(fn,data))>=count,message)
    def refused(self,request,reason,cause=None,fn=None):
        before=self.a.state();payload=self.c.rejected(request,reason)
        if cause is not None:require(dict(reversed(tlvs(payload))).get(self.cause_tag)==bytes((cause,)),'timeline refusal cause')
        if fn is not None:require(dict(reversed(tlvs(payload))).get(self.fn_tag,struct.pack('<H',fn))==struct.pack('<H',fn),'timeline refusal affected fn')
        require(self.a.state()==before,'timeline refusal changed tracks/bind/generations/data')
    def idle_pads(self,state):
        """capture §1.2: every pin keeps its free (idle) state, except an analog track's channels while it waits or
        captures - their pad leaves the digital function at start and comes back when acquisition stops."""
        analog=[fn for fn,name in self.a.selection.items() if name=='oep.fixture.analog']
        skip={ch for fn in analog if state['tracks'].get(fn,{}).get('state') in (2,3) for ch in state['plans'].get(fn,())}
        return {ch:v for ch,v in state['pins'].items() if ch not in skip}=={ch:v for ch,v in state['idle'].items() if ch not in skip}
    def status(self,fn=4):
        before=self.a.state();require(self.idle_pads(before),'capture changed electrical state');payload=self.c.success(self.req(fn,'status'))
        size=13 if fn==4 else 18;require(len(payload)>=size,'group/track status fixed header');tail=tlvs(payload[size:])
        require(self.a.state()==before,'group/track status changed acquisition')
        if fn==4:return struct.unpack_from('<BQI',payload)
        row=struct.unpack_from('<BIQBI',payload);values=dict(reversed(tail));error=values.get(self.error_tag[fn])
        require((row[0]==6)==(error is not None) and (error is None or len(error)==1),'track error TLV exactly in state 6')
        return row+(error,)
    def bind(self,sid,members):
        request=self.req(4,'bind',sid,bytes((len(members),))+b''.join(struct.pack('<H',fn) for fn in members));return request,self.ok(request)
    def start(self,sid,members,failed=False):
        before=self.a.state();request=self.req(4,'start',sid)
        if failed:
            reply=self.c.exchange(request);r=self.c.reg
            require(reply[3:5]==bytes((r['resolutions']['completed'],r['outcomes']['failed'])),'failed initialisation must complete failed');p=reply[5:]
        else:p=self.c.success(request)
        require(len(p)>=13,'group start fixed header');ns,generation,count=struct.unpack_from('<QIB',p)
        require(len(p)>=13+6*count,'group start track array');rows=[struct.unpack_from('<HI',p,13+6*i) for i in range(count)];tlvs(p[13+6*count:])
        require([fn for fn,_ in rows]==list(members),'group start membership/bind order')
        require(generation==before['generation']+1,'group generation progression without wrap')
        require(ns==before['now_ns'],'fixture common start instant')
        for fn,gen in rows:require(gen==before['tracks'][fn]['generation']+1,'track generation progression without wrap')
        group=self.status();require(group[1]==ns and group[2]==generation,'group start status readback')
        for fn,gen in rows:require(self.status(fn)[4]==gen,'start track generation readback')
        return generation,dict(rows),request,p
    def segments(self,fn):
        before=self.a.state();rows=[];first=0
        for _ in range(64):
            p=self.c.success(self.req(fn,'segments',payload=struct.pack('<I',first)))
            require(len(p)>=2 and p[0] in (0,1) and len(p)>=2+37*p[1],'segment page shape')
            page=[struct.unpack_from('<IQIQIIBI',p,2+37*i) for i in range(p[1])];tlvs(p[2+37*p[1]:])
            require(page or not p[0],'segment page progress');rows+=page
            if not p[0]:break
            first=(page[-1][0]+1)&0xffffffff
        require(self.a.state()==before,'segments consumed data');return rows
    def read(self,fn,generation,position=0,maximum=65535):
        """Up to `maximum` bytes from `position`: the probe decides how much one read returns (max or less, capture
        §3.2), so while it sets more the rest is read on from where the answer ended; (first start, first gap | last
        more, the data)."""
        before=self.a.state();first=None;data=b''
        while True:
            p=self.c.success(self.req(fn,'read',payload=struct.pack('<IQH',generation,position,maximum-len(data))))
            require(len(p)>=11,'track read header');start,flags,count=struct.unpack_from('<QBH',p)
            require(flags&~3==0 and count<=maximum-len(data) and len(p)>=11+count,'track read bounds');tlvs(p[11+count:])
            if first is None:first=(start,flags&2)
            elif start!=position or flags&2:break
            data+=p[11:11+count];position=start+count;more=flags&1
            if not more or not count or len(data)>=maximum:break
        require(self.a.state()==before,'track read consumed data')
        return first[0],first[1]|more,data
    def read_rules(self,fn,generation,write_pos,stream=False):
        """capture §2.1/§3.2 read positions; returns the read from position 0."""
        for position in (write_pos,write_pos+1,write_pos+4096):
            require(self.read(fn,generation,position)==(write_pos,0,b''),'read at or past write_pos must answer write_pos, gap 0, len 0')
        full=self.read(fn,generation,0);empty=self.read(fn,generation,0,0)
        require(empty[2]==b'' and empty[0]==full[0] and empty[1]&2==full[1]&2,'max 0 read must follow the same position/gap rules')
        start,flags,data=full
        if flags&2:
            require(stream,'retained one-shot data must be readable from position 0 without gap');require(start>0,'read gap must advance to a readable position or write_pos')
            if not data:require(start==write_pos,'nothing readable after a re-used position must answer write_pos, gap 1, len 0')
        else:require(start==0 and (data or not write_pos),'read from position 0 without gap must return retained data')
        require(bool(flags&1)==(start+len(data)<write_pos) and start+len(data)<=write_pos,'read more flag and write_pos bound')
        self.refused(self.req(fn,'read',payload=struct.pack('<IQH',(generation-1)&0xffffffff,0,1)),'unavailable',6)
        return full
    def released(self,generation,gens):
        state=self.a.state();require(not state['bound'] and not state['plans'] and self.idle_pads(state) and not state['pending'],'session closure retained group/plans/pins/notifications')
        require(state['generation']==generation and all(t['state']==0 and not t['config'] and not t['data'] and not t['current'] and not t['segments'] and not t['subscribed'] and t['generation']==gens[fn] for fn,t in state['tracks'].items()),'session closure retained capture/subscription or reset generation')
    def one_shot(self,sid,members,rates,offset):
        for fn,rate in zip(members,rates):self.configure(sid,fn,1,rate)
        self.bind(sid,members);require(self.status()[0]==1,'configured group state')
        self.a.advance_to(offset);generation,gens,_,_=self.start(sid,members);origin=self.status()[1]
        require(self.status()[0]==3 and all(self.status(fn)[:3]==(3,0,0) for fn in members),'started tracks before the first sample instant')
        self.a.advance_to(origin+7000000)
        for fn,rate in zip(members,rates):
            n=arrived(rate,7000000,16);row=self.status(fn);require(row[:3]==(3,0,self.confirmed(fn,n)),'write_pos counts confirmed bytes of arrived samples only')
            start,flags,data=self.read_rules(fn,gens[fn],row[2]);self.expect(fn,data,0,len(self.decode(fn,data)),'partial read raw values')
            require(len(self.decode(fn,data))==self.confirmed(fn,n)*8//self.layouts[fn]['bits'],'partial read sample count')
        self.a.advance_to(origin+100000000);require(self.status()==(4,origin,generation),'cross-rate one-shot completion')
        for fn,rate in zip(members,rates):
            row=self.status(fn);size=self.full(fn,16);require(row[:3]==(4,1,size),'completed track serial_done/write_pos')
            require(self.segments(fn)==[(0,0,16,origin,self.segments(fn)[0][4],NONE,0,gens[fn])],'one-shot segment geometry/start_ns/generation')
            start,flags,data=self.read_rules(fn,gens[fn],size);require(len(data)==size,'complete read byte count');self.expect(fn,data,0,16,'one-shot raw values')
        return generation,gens
    def subscribe(self,sid,members):
        for fn in members:self.ok(self.req(fn,'subscribe',sid,struct.pack('<HI',0,0)))
    def collect(self,store,at):
        for frame in self.a.drain():
            require(type(frame)is bytes and 6<=len(frame)<=self.c.max_frame and frame[0] in (EVENT,DATA),'notification role and max_frame')
            fn,seq=struct.unpack_from('<HH',frame,1);require(fn in store,'notification from a subscribed track')
            if frame[0]==DATA:
                require(len(frame)>=15,'data frame header');position,size=struct.unpack_from('<QH',frame,5);require(len(frame)>=15+size,'data frame length')
                values={}
                for tag,value in tlvs(frame[15+size:]):values.setdefault(tag,value)
                gen=values.get(self.data_tags[fn]['generation']);info=values.get(self.data_tags[fn]['segment_info'])
                require(gen is not None and len(gen)==4,'streaming data generation TLV');require(info is not None and len(info)==24,'streaming data segment_info TLV')
                store[fn].append(dict(kind='data',seq=seq,at=at,position=position,data=frame[15:15+size],generation=struct.unpack('<I',gen)[0],info=struct.unpack('<QIQI',info)))
            else:store[fn].append(dict(kind='event',seq=seq,at=at,event=frame[5],fixed=frame[6:]))
    def stream(self,fn,items,generation,origin,rate,write_pos,rows,stopped,exact=True):
        """capture §2.1/§2.2/§3.4 streaming frames of one track against its segments."""
        require([i['seq'] for i in items]==list(range(len(items))),'fixture notification seq from subscribe without loss')
        data=[i for i in items if i['kind']=='data'];events=[i for i in items if i['kind']=='event']
        require(not any(e['event']==self.events[fn]['segment'] for e in events),'streaming must not send segment events')
        ends=[e for e in events if e['event']==self.events[fn]['stopped']]
        require(len(ends)==1 and len(ends[0]['fixed'])>=6 and struct.unpack_from('<BBI',ends[0]['fixed'])==stopped+(generation,),'stopped event reason/error/generation');tlvs(ends[0]['fixed'][6:])
        require(all(i['generation']==generation for i in data),'data generation TLV')
        cursor=0
        for i in data:require(i['position']==cursor,'fixture stream positions contiguous from 0');cursor+=len(i['data'])
        require(cursor==write_pos if exact else cursor>=write_pos,'received stream end and write_pos')
        first=0;by={};buffers={}
        for k,row in enumerate(rows):
            require(row[0]==k and row[7]==generation and row[5]==NONE and row[6]&~2==0 and (not row[6]&2 or k==len(rows)-1),'streaming segment serial/generation/trigger/flags')
            require(row[1]==(rows[k-1][1]+self.full(fn,rows[k-1][2]) if k else 0),'streaming segments continuous without gap')
            require(row[3]==int(origin+Fraction(first*1000000000)/rate),'streaming segment start_ns on the track grid');by[row[1]]=(row,first);first+=row[2]
        require(rows and rows[-1][1]+self.full(fn,rows[-1][2])==write_pos,'write_pos at the end of the last published segment')
        seen={}
        for i in data:
            position,samples,ns,uncertainty=i['info'];end=i['position']+len(i['data'])
            require(samples>=1 and position<=i['position'] and len(i['data'])>=1,'segment_info origin and sample count')
            require(samples>=seen.get(position,0),'segment_info samples never decrease');seen[position]=samples
            if position in by:
                row,base=by[position];require(samples<=row[2] and ns==row[3] and uncertainty==row[4],'segment_info matches the published segment')
            else:
                require(not exact and position==write_pos,'data outside the published segments');base=first
                require(ns==int(origin+Fraction(base*1000000000)/rate),'failed segment_info start_ns on the track grid')
            require(base+samples<=arrived(rate,i['at']-origin),'segment_info counted a future sample')
            complete=position in by and samples==by[position][0][2]
            require(end<=position+(self.full(fn,samples) if complete else self.confirmed(fn,samples)),'frame crossed its segment or sent an unconfirmed byte')
            buffers.setdefault(position,bytearray()).extend(i['data'])
        for position,(row,base) in by.items():
            require(len(buffers.get(position,b''))==self.full(fn,row[2]),'segment bytes received');self.expect(fn,bytes(buffers[position]),base,row[2],'streaming raw values')
        return first
    def streaming(self,sid,members,rates,offset):
        for fn,rate in zip(members,rates):self.configure(sid,fn,3,rate)
        self.bind(sid,members);self.refused(self.req(4,'start',sid),'unavailable',6)
        self.subscribe(sid,members);self.a.advance_to(offset);generation,gens,_,_=self.start(sid,members);origin=self.status()[1];store={fn:[] for fn in members}
        for ms in self.steps:
            self.a.advance_to(origin+ms*1000000);self.collect(store,origin+ms*1000000)
            for fn in members:
                row=self.status(fn);require(row[0]==3,'streaming track state');self.read_rules(fn,gens[fn],row[2],stream=True)
        stop=origin+self.steps[-1]*1000000;self.ok(self.req(4,'stop',sid));self.collect(store,stop)
        require(self.status()[0]==1,'stopped streaming group')
        for fn,rate in zip(members,rates):
            row=self.status(fn);require(row[0]==1,'stopped streaming track');self.read_rules(fn,gens[fn],row[2],stream=True)
            total=self.stream(fn,store[fn],gens[fn],origin,rate,row[2],self.segments(fn),(1,0))
            require(total==arrived(rate,stop-origin),'streaming sample count until stop')
        return generation,gens
    def scenario(self,mode,members,rates,offset):
        self.fresh()
        with self.c.holding(1000) as sid:
            self.plan(sid);generation,gens=(self.one_shot if mode==1 else self.streaming)(sid,members,rates,offset)
            self.ok(self.c.request(self.c.core['end'],session=sid));self.c.session=None;self.released(generation,gens)
    def run(self):
        start=len(self.c.results);self.c.check('OEP-TIMELINE-CONTEXT','capture §4; core §7',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for at,args in enumerate(self.scenarios):
            first=len(self.a.trace);self.c.check('OEP-TIMELINE-'+str(at),'capture §2/3.2/3.4/4.1; core §9/10',lambda args=args:self.scenario(*args));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,
                    scope='independent cross-rate immediate ADC/logic group one-shot/streaming acquisition, read positions, segment_info and known raw values',
                    scenarios=[dict(mode=mode,members=list(members),rates=list(rates),offset_ns=offset) for mode,members,rates,offset in self.scenarios],
                    unchecked=['repeat/release resumption, streaming drops and subscription loss','other rates/ADC layouts/skew policies, serial retention bounds','error/replay/resource races on asynchronous schedule','physical timing/skew/accuracy and production Host'])
