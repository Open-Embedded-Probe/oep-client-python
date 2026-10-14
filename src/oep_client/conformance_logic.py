"""SPEC-first logic track configuration, one-shot/repeat data and lifetime checks."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_registered_ops import RegisteredOpsChecks


class LogicChecks:
    cases=('STATE0','QUERY','CONTRACT','WIDTH','RAW-TAGS','FIRST-WINS','RATE','SAMPLES','TRIGGER','PRETRIGGER','WAIT','ONE-SHOT','READ','READ-GAP','GENERATION','GEN-EXHAUST','PAGES','REPEAT','RELEASE','SHORT-STOP','ERROR','RECONFIGURE','REPLAY-START','REPLAY-RELEASE','PLAN-REPLACE','PLAN-RELEASE','END','EXPIRY','FORCE','SESSION')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    @staticmethod
    def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value
    def req(self,fn,name,sid=0,payload=b''):return self.c.request(self.codes[fn][name],payload,fn=fn,session=sid)
    def ok(self,req):v=self.c.success(req);tlvs(v);return v
    def plan(self,sid,roles=((0,0),(1,1))):self.planned=len(roles);self.ok(self.req(1,'plan_apply',sid,b''.join(self.tlv(self.assignment,struct.pack('<HBH',2,role,ch)) for role,ch in roles)))
    def release_plan(self,sid):self.ok(self.req(1,'plan_release',sid,b'\1\2\0'))
    def settings(self,mode=1,rate=1000,samples=8,segments=None,trigger=None,pre=None,critical=False):
        result=b''
        for name,value in [('mode',bytes((mode,))),('rate',struct.pack('<I',rate)),('samples',None if samples is None else struct.pack('<I',samples)),('segments',None if segments is None else struct.pack('<I',segments)),('trigger',None if trigger is None else struct.pack('<BBI',*trigger)),('pretrigger',None if pre is None else struct.pack('<I',pre))]:
            if value is not None:result+=self.tlv(self.tags[name]|(128 if critical else 0),value)
        return result
    def configure(self,sid,**kwargs):return self.answer(self.c.success(self.req(2,'configure',sid,self.settings(**kwargs))),kwargs.get('mode',1))
    def answer(self,v,mode=1):
        values=dict(reversed(tlvs(v)));tag=self.answers
        require(all(tag[k] in values for k in ('actual_rate','layout')),'configure/query mandatory answer TLVs')
        for name in (('actual_samples',) if mode in (1,2) else ())+(('actual_segments',) if mode==2 else ()):
            require(tag[name] in values and len(values[tag[name]])==4 and int.from_bytes(values[tag[name]],'little')>=1,'configure/query actual capacity required')
        require(len(values[tag['actual_rate']])==8,'capture actual rate shape');num,den=struct.unpack('<II',values[tag['actual_rate']]);require(num>0 and den>0,'capture actual rate ratio');layout=values[tag['layout']];require(len(layout)>=2 and 1<=layout[0]<=128 and len(layout)==2+layout[1] and len(set(layout[2:]))==layout[1] and all(p<layout[0] for p in layout[2:]),'logic layout bounds/count/uniqueness');require(layout[1]==self.planned,'logic layout C differs from planned roles');require(layout==b'\3\2\0\1','explicit fixture layout differs');return values
    def status(self):
        before=self.a.state();v=self.c.success(self.req(2,'status'));require(len(v)>=18,'capture status width');row=struct.unpack_from('<BIQBI',v);tlvs(v[18:]);require(self.a.state()==before,'status changed acquisition');return row,v[18:]
    def start(self,sid):
        req=self.req(2,'start',sid);v=self.c.success(req);require(len(v)>=4,'start fields');generation,=struct.unpack_from('<I',v);tlvs(v[4:]);require(generation>0,'start generation after first start');return generation,req,v
    def read(self,generation,position=0,maximum=65535):
        before=self.a.state();v=self.c.success(self.req(2,'read',payload=struct.pack('<IQH',generation,position,maximum)));require(len(v)>=11,'capture read header');start,flags,count=struct.unpack_from('<QBH',v);require(flags&~3==0 and count<=maximum and len(v)>=11+count,'capture read bounds/flags');tlvs(v[11+count:]);require(self.a.state()==before,'read consumed capture data');return start,flags,v[11:11+count]
    def segments(self,first=0):
        before=self.a.state();rows=[]
        for _ in range(8):
            v=self.c.success(self.req(2,'segments',payload=struct.pack('<I',first)));require(len(v)>=2 and v[0] in (0,1) and len(v)>=2+37*v[1],'segment page shape');page=[struct.unpack_from('<IQIQIIBI',v,2+37*i) for i in range(v[1])];tlvs(v[2+37*v[1]:]);rows+=page
            if not v[0]:break
            require(page,'segment paging progress');first=(page[-1][0]+1)&0xffffffff
        else:raise ValueError('segment paging did not terminate')
        require(self.a.state()==before,'segments changed acquisition');return rows
    def refused(self,req,reason,payload=None):
        before=self.a.state();v=self.c.rejected(req,reason)
        if payload is not None:require(v==payload,'capture refusal raw tag/cause')
        require(self.a.state()==before,'refusal changed capture/plan/pins')
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'explicit new logical boot');self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'boot readback')
    def acquired(self,sid,samples=None):
        self.configure(sid);generation,_,_=self.start(sid);samples=samples or [[i&1,(i>>1)&1] for i in range(8)];require(self.a.feed(samples)==len(samples),'logical sample admission');return generation,samples
    def data(self,generation,samples):
        start,flags,data=self.read(generation);require(start==0 and flags==0 and len(data)==(3*len(samples)+7)//8,'logic data byte count')
        for i,row in enumerate(samples):
            for k,value in enumerate(row):require((data[(3*i+k)//8]>>((3*i+k)%8))&1==value,'logic channel sample packing')
        return data
    def closed(self):
        s=self.a.state();require(s['state']==0 and not s['config'] and not s['data'] and not s['segments'] and not s['current'] and not s['history'] and s['pins']==s['idle'],'plan closure retained capture/config/pins')
    def case(self,kind):
        self.fresh()
        if kind=='GEN-EXHAUST':self.a.seed(0xfffffffe)
        with self.c.holding(1000) as sid:
            pins=self.a.state()['pins']
            if kind=='STATE0':
                for name in ('configure','query'):self.refused(self.req(2,name,sid if name=='configure' else 0,self.settings()),'unavailable',self.tlv(1,b'\6'))
                self.refused(self.req(2,'start',sid),'unavailable',self.tlv(1,b'\6'));self.ok(self.req(2,'stop',sid));require(self.read(0)==(0,0,b''),'state0 empty read');self.plan(sid);require(self.a.state()['pins']==pins,'logic plan changed electrical state');return
            self.plan(sid)
            if kind=='QUERY':
                before=self.a.state();v=self.answer(self.c.success(self.req(2,'query',payload=self.settings())));require(self.a.state()==before,'query applied settings');configured=self.configure(sid);require(v==configured,'query/configure actual values differ')
            elif kind=='CONTRACT':
                payloads=[b'',self.tlv(self.tags['mode'],b'\1'),self.settings(samples=None),self.settings(rate=0),self.settings(samples=0),self.settings(mode=2,segments=0)]
                for p in payloads:self.refused(self.req(2,'configure',sid,p),'malformed')
                for p,tag in [(self.settings(segments=2),self.tags['segments']),(self.settings(trigger=(0,255,0xffffffff),pre=0),self.tags['pretrigger']),(self.settings(pre=0),self.tags['pretrigger']),(self.settings(mode=3,samples=None),self.tags['mode'])]:self.refused(self.req(2,'configure',sid,p),'unsupported',bytes((tag,)))
                self.refused(self.req(2,'query',payload=self.settings(mode=3,samples=None)),'unsupported',bytes((self.tags['mode'],)))
            elif kind=='WIDTH':
                for name in ('mode','rate','samples','segments','trigger','pretrigger'):
                    base=b''.join(self.tlv(tag,value) for tag,value in tlvs(self.settings()) if tag!=self.tags[name])
                    for width in (0,2 if name=='mode' else 1):self.refused(self.req(2,'configure',sid,base+self.tlv(self.tags[name]|128,b'x'*width)),'malformed')
            elif kind=='RAW-TAGS':
                for critical in (False,True):
                    self.configure(sid,critical=critical)
                    for field,kwargs in [('mode',dict(mode=4)),('rate',dict(rate=99)),('trigger',dict(trigger=(3,0,0)))]:self.refused(self.req(2,'configure',sid,self.settings(critical=critical,**kwargs)),'unsupported',bytes((self.tags[field]|(128 if critical else 0),)))
                self.configure(sid);self.refused(self.req(2,'configure',sid,self.settings()+self.tlv(254,b'x')),'unsupported',b'\xfe')
            elif kind=='FIRST-WINS':
                v=self.answer(self.c.success(self.req(2,'configure',sid,self.settings()+self.tlv(self.tags['rate']|128,struct.pack('<I',1)))));require(struct.unpack('<II',v[self.answers['actual_rate']])==(1000,1),'duplicate rate did not retain first')
            elif kind=='RATE':
                for rate in (100,537,1000):require(struct.unpack('<II',self.configure(sid,rate=rate)[self.answers['actual_rate']])==(rate,1),'exact fixture rate')
                for rate in (99,1001):self.refused(self.req(2,'configure',sid,self.settings(rate=rate)),'unsupported',bytes((self.tags['rate'],)))
            elif kind=='SAMPLES':
                v=self.configure(sid,samples=100);require(struct.unpack('<I',v[self.answers['actual_samples']])[0]==16,'samples must clamp capacity');self.configure(sid,mode=2,segments=1);require(self.a.state()['config']['segments']==1,'repeat segment count')
                for segments in (100,None):v=self.configure(sid,mode=2,segments=segments);require(struct.unpack('<I',v[self.answers['actual_segments']])[0]==2==self.a.state()['config']['segments'],'repeat segments must clamp/default to capacity')
            elif kind=='TRIGGER':
                self.configure(sid,trigger=(0,255,0xffffffff));self.refused(self.req(2,'configure',sid,self.settings(trigger=(1,2,0))),'unavailable',self.tlv(1,b'\6'))
                for trigger in ((1,0,2),(2,0,3)):self.refused(self.req(2,'configure',sid,self.settings(trigger=trigger)),'unsupported',bytes((self.tags['trigger'],)))
            elif kind=='PRETRIGGER':
                for pre in (8,9):self.refused(self.req(2,'configure',sid,self.settings(trigger=(1,0,1),pre=pre)),'unsupported',bytes((self.tags['pretrigger'],)))
                self.configure(sid,trigger=(1,0,1),pre=7)
            elif kind=='WAIT':
                self.configure(sid,trigger=(2,0,0),pre=2);generation,_,_=self.start(sid);require(self.status()[0][0]==2,'edge trigger did not wait');self.a.feed([[0,0],[0,1]]);require(self.status()[0][0]==2,'trigger fired without edge');self.a.feed([[1,0],[0,1],[1,1],[0,0],[1,0],[0,1]]);require(self.status()[0][0]==4,'trigger capture did not complete');row=self.segments()[0];require(row[2]==8 and row[5]==2 and row[7]==generation,'trigger/pretrigger segment');self.data(generation,[[0,0],[0,1],[1,0],[0,1],[1,1],[0,0],[1,0],[0,1]])
            elif kind in ('ONE-SHOT','READ','GENERATION','GEN-EXHAUST','RECONFIGURE','REPLAY-START'):
                generation,samples=self.acquired(sid);data=self.data(generation,samples);row=self.segments()[0];require(row[0:3]==(0,0,8) and row[5]==0xffffffff and row[7]==generation and self.status()[0]==(4,1,len(data),0,generation),'one-shot state/segment')
                if kind=='READ':require(self.read(generation,0,2)==(0,1,data[:2]) and self.read(generation,2)==(2,0,data[2:]) ,'read continuation');require(self.read(generation,999)==self.read(generation,len(data))==(len(data),0,b''),'read at/after write_pos must answer write_pos, gap 0, len 0');empty=self.read(generation,0,0);require(empty[0]==0 and not empty[1]&2 and empty[2]==b'','max0 read position/gap')
                elif kind=='GENERATION':
                    new,_,_=self.start(sid);require(new==generation+1,'generation progression');self.refused(self.req(2,'read',payload=struct.pack('<IQH',generation,0,1)),'unavailable',self.tlv(1,b'\6'));self.refused(self.req(2,'release',sid,struct.pack('<II',generation,0)),'unavailable',self.tlv(1,b'\6'));require(self.read(new)==(0,0,b'') and not self.segments(),'start retained old data')
                elif kind=='GEN-EXHAUST':require(generation==0xffffffff,'generation before exhaustion');self.refused(self.req(2,'start',sid),'unavailable',self.tlv(1,b'\2'));self.configure(sid);self.refused(self.req(2,'start',sid),'unavailable',self.tlv(1,b'\2'));require(self.status()[0][-1]==0xffffffff,'exhausted generation reused')
                elif kind=='RECONFIGURE':self.configure(sid);require(self.read(generation)==(0,0,b'') and not self.segments() and self.status()[0][-1]==generation,'configure data reset/generation')
                elif kind=='REPLAY-START':
                    gen,req,v=self.start(sid);self.a.feed(samples);before=self.a.state();require(self.c.success(req)==v and self.a.state()==before and self.status()[0][-1]==gen,'start replay recreated generation/data')
            elif kind=='READ-GAP':
                self.configure(sid,mode=2,samples=8,segments=2);generation,_,_=self.start(sid);self.a.feed([[i&1,(i>>1)&1] for i in range(16)]);end=self.status()[0][2]
                require(self.read(generation,end)==self.read(generation,end+1,0)==(end,0,b''),'read at/after write_pos must answer write_pos, gap 0, len 0')
                self.ok(self.req(2,'release',sid,struct.pack('<II',generation,1)));require(self.status()[0][0]==3 and self.status()[0][2]==end,'release of every segment resume/write_pos')
                for position in (0,end//2,end-1):
                    for maximum in (0,1,65535):require(self.read(generation,position,maximum)==(end,2,b''),'no readable data must answer write_pos, gap 1, len 0')
                require(self.read(generation,end)==(end,0,b''),'read at write_pos after release');require(self.a.feed([[1,0]]*8)==8,'resumed sample admission');new=self.status()[0][2]
                start,flags,data=self.read(generation,0);require(start==end and flags==2 and len(data)==new-end,'reused position moves to next readable data with gap');empty=self.read(generation,0,0);require(empty[0]==end and empty[1]&2 and empty[2]==b'','max0 read follows position/gap rules')
            elif kind in ('PAGES','REPEAT','RELEASE','REPLAY-RELEASE'):
                self.configure(sid,mode=2,samples=8,segments=2);generation,_,_=self.start(sid);self.a.feed([[i&1,(i>>1)&1] for i in range(16)]);rows=self.segments();require(len(rows)==2 and rows[0][0:3]==(0,0,8) and rows[1][0:3]==(1,3,8) and self.status()[0][0]==5,'repeat capacity/positions/paging');require(rows[1][3]-rows[0][3]==8000000,'repeat segment timestamps not continuous');require(self.segments(1)==[rows[1]] and self.segments(2)==[],'segment inclusive/next cursor')
                if kind!='PAGES':
                    req=self.req(2,'release',sid,struct.pack('<II',generation,0));v=self.ok(req);require(self.status()[0][0]==3 and self.read(generation)[0:2]==(3,2),'release resume/gap');self.a.feed([[0,1]]*8);last=self.segments()[-1];require(last[0:3]==(2,6,8) and last[6]&1,'resumed segment gap flag/positions')
                    if kind=='REPLAY-RELEASE':before=self.a.state();require(self.c.success(req)==v and self.a.state()==before,'release replay freed current segment')
            elif kind=='SHORT-STOP':
                self.configure(sid);generation,_,_=self.start(sid);self.a.feed([[1,0]]*3);self.ok(self.req(2,'stop',sid));require(self.status()[0][0]==1 and self.segments()[0][2]==3 and self.segments()[0][6]&2,'stop short segment/state');self.data(generation,[[1,0]]*3)
            elif kind=='ERROR':
                self.configure(sid,mode=2);generation,_,_=self.start(sid);self.a.feed([[1,0]]*9);before=self.read(generation)[2];self.a.fail(2);status,tail=self.status();require(status==(6,1,len(before),1,generation) and dict(tlvs(tail)).get(1)==b'\2','error status/write_pos');require(self.read(generation)[2]==before and len(self.segments())==1,'error canceled committed segment/kept incomplete');new,_,_=self.start(sid);require(new==generation+1 and self.status()[0][3]==0,'error restart flags/generation')
            elif kind in ('PLAN-REPLACE','PLAN-RELEASE','END','EXPIRY','FORCE'):
                self.configure(sid,mode=2);generation,_,_=self.start(sid);self.a.feed([[1,0]]*9)
                if kind=='PLAN-REPLACE':self.plan(sid)
                elif kind=='PLAN-RELEASE':self.release_plan(sid)
                elif kind=='END':self.ok(self.c.request(self.c.core['end'],session=sid));self.c.session=None
                elif kind=='EXPIRY':self.a.advance(1000);require(struct.unpack_from('<BI',self.c.success(self.c.request(self.c.core['lock_state'])))==(0,0),'capture expiry');self.c.session=None
                else:
                    old=self.req(2,'stop',sid);replacement=secrets.randbelow(0xffffffff)+1
                    while replacement==sid:replacement=secrets.randbelow(0xffffffff)+1
                    self.c.corr=0;v=self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=replacement));require(struct.unpack_from('<II',v)==(1000,self.c.boot),'force reply');tlvs(v[8:]);self.c.session=replacement
                self.closed();require(self.read(generation)==(0,0,b'') and not self.segments() and self.status()[0][-1]==generation,'plan release data/generation')
                if kind=='FORCE':self.plan(replacement);self.configure(replacement);self.refused(old,'locked')
            elif kind=='SESSION':
                for name,payload in [('configure',self.settings()),('start',b''),('stop',b''),('release',b'\0'*8)]:self.refused(self.req(2,name,0,payload),'session_required')
                self.status();self.read(0);self.segments();self.answer(self.c.success(self.req(2,'query',payload=self.settings())))
            require(self.a.state()['pins']==pins,'logic capture changed pin electrical state')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.selection=={1:'oep.probe.plan',2:'oep.fixture.logic'},'explicit logic track selection');base=RegisteredOpsChecks(self.c,self.a.selection);require(base.run()['status']=='passed','registered capture ops');self.codes={fn:{row['name']:row['code'] for row in base.specs[name]['op']} for fn,name in self.a.selection.items()};spec=base.specs['oep.fixture.logic'];self.tags=spec['tlv']['configure'];self.answers=spec['tlv']['configure_answer'];self.assignment=base.specs['oep.probe.plan']['tlv']['plan_apply']['role_assignment'];require(self.a.state()['clock_scope']=='explicit-logical-ms','logical fixture clock')
        self.c.check('OEP-LOGIC-CONTEXT','capture §1–3; plan §2',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for kind in self.cases:
            first=len(self.a.trace);self.c.check('OEP-LOGIC-'+kind,'capture §1–3; plan §2; core §6/8/9',lambda kind=kind:self.case(kind));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(row['status']=='passed' for row in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,scope='explicit logic track one-shot/repeat samples, configure contract/answer rows, generations and exhaustion, read/segments and plan lifecycle',unchecked=['analog/calibration, capture-group, streaming/notifications','query while acquiring and optional force/subscription behavior','early trigger prehistory and other trigger/rate/packing geometries','serial wrap and retained segment metadata independent of data lifetime','physical sampling/timing/uncertainty, persisted config and concurrency','production Host integration'])
