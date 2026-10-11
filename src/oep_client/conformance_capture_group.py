"""Independent selected-SPEC group wire/lifecycle checker for paired sample fixtures."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_registered_ops import RegisteredOpsChecks
from .conformance_analog_payload import AnalogPayloadChecks

NO_TIME=(1<<64)-1


class CaptureGroupChecks:
    cases=('EMPTY','BIND','REVERSE','BIND-DUP','BIND-UNKNOWN','BIND-UNCONFIGURED','BIND-MODE','BIND-TRIGGER','BIND-PRE','BIND-RATE','BIND-WIDTH','BIND-FIRST','BIND-CRITICAL','UNBIND','UNBIND-ACTIVE','TRACK-LOCK','PLAN-LOCK','QUERY','START','GENERATION','GEN-WRAP','REBIND-GEN','START-ATOMIC','REPLAY-START','REPLAY-BIND','STOP','TRIGGER','FORCE','REPEAT','RELEASE','ERROR-ADC','ERROR-LOGIC','REPLAY-STOP','END','EXPIRY','SESSION-FORCE','SESSION')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    @staticmethod
    def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value
    def req(self,fn,name,sid=0,payload=b''):return self.c.request(self.codes[fn][name],payload,fn=fn,session=sid)
    def ok(self,request):v=self.c.success(request);tlvs(v);return v
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'explicit new group boot')
        self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'group boot readback')
    def plan(self,sid):
        rows=[(2,1,0),(2,3,1),(3,0,2),(3,1,3)]
        return self.ok(self.req(1,'plan_apply',sid,b''.join(self.tlv(self.assignment,struct.pack('<HBH',*r)) for r in rows)))
    def settings(self,fn,mode=1,samples=8,rate=1000,trigger=None,pre=None,segments=None):
        rows=[('mode',bytes((mode,))),('rate',struct.pack('<I',rate)),('samples',struct.pack('<I',samples))]
        if trigger is not None:rows.append(('trigger',struct.pack('<BBI',*trigger)))
        if pre is not None:rows.append(('pretrigger',struct.pack('<I',pre)))
        if segments is not None:rows.append(('segments',struct.pack('<I',segments)))
        return b''.join(self.tlv(self.tags[fn][name],value) for name,value in rows)
    def configure(self,sid,mode=1,samples=8,trigger=False,pre=2,segments=None):
        self.config={}
        for fn in (2,3):
            p=self.c.success(self.req(fn,'configure',sid,self.settings(fn,mode,samples,trigger=(3,3,1500) if fn==2 and trigger else None,pre=pre if fn==2 and trigger else None,segments=segments)))
            if fn==2:self.config[2]=self.payload.configure(p,roles=(1,3),frontends={0,1},mode=mode,max_op_ms=self.maximum)
            else:
                values=dict(reversed(tlvs(p)));require(values[self.answers[3]['layout']]==b'\3\2\0\1','explicit group logic layout')
                require(values[self.answers[3]['actual_rate']]==struct.pack('<II',1000,1),'explicit group logic rate')
    def binding(self,members=(2,3),source=0,critical=False):
        return bytes((len(members),))+b''.join(struct.pack('<H',fn) for fn in members)+(self.tlv(self.trigger_tag|(128 if critical else 0),struct.pack('<H',source)) if source else b'')
    def bind(self,sid,members=(2,3),source=0):
        request=self.req(4,'bind',sid,self.binding(members,source));return request,self.ok(request)
    def refused(self,request,reason,cause=None,fn=None):
        before=self.a.state();payload=self.c.rejected(request,reason)
        if cause is not None:require(dict(tlvs(payload)).get(self.cause_tag)==bytes((cause,)),'group refusal cause')
        if fn is not None:
            values=dict(tlvs(payload[1:] if reason=='unsupported' else payload))
            require(values.get(self.fn_tag)==struct.pack('<H',fn),'group refusal affected fn')
        require(self.a.state()==before,'group refusal changed tracks/bind/generations/pins')
    def status(self,fn=4):
        before=self.a.state();require(before['pins']==before['idle'],'active group changed electrical state');payload=self.c.success(self.req(fn,'status'))
        size=23 if fn==4 else 18;require(len(payload)>=size,'group/track status fixed header');tlvs(payload[size:])
        require(self.a.state()==before,'group/track status changed acquisition')
        return struct.unpack_from('<BQQHI' if fn==4 else '<BIQBI',payload),payload[size:]
    def start(self,sid,members=(2,3)):
        before=self.a.state();request=self.req(4,'start',sid);p=self.c.success(request)
        require(len(p)>=17,'group start header');blocking,ns,generation,count=struct.unpack_from('<IQIB',p)
        require(blocking<=self.maximum and len(p)>=17+6*count,'group start blocking/shape')
        rows=[struct.unpack_from('<HI',p,17+6*i) for i in range(count)];tlvs(p[17+6*count:])
        require([fn for fn,gen in rows]==list(members),'group start membership/bind order')
        require(generation==((before['generation']+1)&0xffffffff or 1),'group generation progression')
        require(ns==before['now_ns'],'fixture common start instant')
        for fn,gen in rows:
            require(gen==((before['tracks'][fn]['generation']+1)&0xffffffff or 1),'track generation progression')
            require(self.status(fn)[0][-1]==gen,'start track generation readback')
        require(self.status()[0][1]==ns and self.status()[0][-1]==generation,'group start status readback')
        return generation,dict(rows),request,p
    def segments(self,fn):
        before=self.a.state();p=self.c.success(self.req(fn,'segments',payload=b'\0'*4));require(len(p)>=2 and p[0]==0 and len(p)>=2+37*p[1],'fixture complete segment page')
        rows=[struct.unpack_from('<IQIQIIBI',p,2+37*i) for i in range(p[1])];tlvs(p[2+37*p[1]:]);require(self.a.state()==before,'group track segments consumed data');return rows
    def read(self,fn,generation,position=0,maximum=65535):
        before=self.a.state();p=self.c.success(self.req(fn,'read',payload=struct.pack('<IQI',generation,position,maximum)))
        require(len(p)>=13,'track read header');start,flags,count=struct.unpack_from('<QBI',p)
        require(flags&~3==0 and count<=maximum and len(p)>=13+count,'track read bounds');tlvs(p[13+count:]);require(self.a.state()==before,'track read consumed group data')
        return start,flags,p[13:13+count]
    @staticmethod
    def rows(count=8):return [{2:[i,4095-i],3:[i&1,(i>>1)&1]} for i in range(count)]
    def data(self,generations,rows):
        for fn in (2,3):
            start,flags,data=self.read(fn,generations[fn]);require(start==0 and flags==0,'initial grouped read position/flags')
            if fn==2:self.payload.data(data,self.config[2],[r[2] for r in rows])
            else:
                require(len(data)==(3*len(rows)+7)//8,'group logic byte count')
                for i,row in enumerate(rows):
                    for k,bit in enumerate(row[3]):require((data[(3*i+k)//8]>>((3*i+k)%8))&1==bit,'group logic raw sample')
    def case(self,kind):
        self.fresh()
        if kind=='GEN-WRAP':self.a.seed(0xffffffff,0xffffffff,0xffffffff)
        if kind=='GENERATION':self.a.seed(4,7,12)
        with self.c.holding(1000) as sid:
            if kind=='EMPTY':
                require(self.status()[0]==(0,NO_TIME,NO_TIME,0,0),'unbound initial group status')
                self.refused(self.req(4,'start',sid),'unavailable',6);self.ok(self.req(4,'stop',sid));self.ok(self.req(4,'force',sid));self.bind(sid,());return
            self.plan(sid);self.configure(sid)
            if kind in ('BIND-DUP','BIND-UNKNOWN'):
                members=(2,2) if kind=='BIND-DUP' else (2,7)
                self.refused(self.req(4,'bind',sid,self.binding(members)),'malformed' if kind=='BIND-DUP' else 'unsupported',fn=None if kind=='BIND-DUP' else 7);return
            if kind=='BIND-UNCONFIGURED':self.a.invalidate(3);self.refused(self.req(4,'bind',sid,self.binding()),'unavailable',6,3);return
            if kind in ('BIND-MODE','BIND-TRIGGER','BIND-PRE','BIND-RATE'):
                kwargs={'mode':2} if kind=='BIND-MODE' else {'trigger':(2,0,0)} if kind=='BIND-TRIGGER' else {'samples':2} if kind=='BIND-PRE' else {'rate':537}
                self.c.success(self.req(3,'configure',sid,self.settings(3,**kwargs)))
                if kind=='BIND-PRE':self.c.success(self.req(2,'configure',sid,self.settings(2,trigger=(3,3,1500),pre=3)))
                self.refused(self.req(4,'bind',sid,self.binding(source=2 if kind=='BIND-PRE' else 0)),'unavailable',2 if kind=='BIND-RATE' else 6,3);return
            if kind=='BIND-WIDTH':
                for value in (b'',b'x',b'xxx'):
                    self.refused(self.req(4,'bind',sid,self.binding()+self.tlv(self.trigger_tag|128,value)),'malformed')
                self.refused(self.req(4,'bind',sid,b'\2\2\0'),'malformed');return
            if kind in ('BIND-FIRST','BIND-CRITICAL'):
                p=self.binding(source=2,critical=kind=='BIND-CRITICAL')
                if kind=='BIND-FIRST':p+=self.tlv(self.trigger_tag|128,b'\xff\xff')
                self.ok(self.req(4,'bind',sid,p));require(self.a.state()['source']==2,'bind trigger tag critical/first-wins');return
            triggered=kind in ('TRIGGER','FORCE');repeat=kind in ('REPEAT','RELEASE','ERROR-ADC','ERROR-LOGIC')
            if triggered:self.configure(sid,trigger=True)
            if repeat:self.configure(sid,mode=2,samples=4,segments=1 if kind in ('REPEAT','RELEASE') else 2)
            members=(3,2) if kind=='REVERSE' else (2,3)
            bind_request,bind_reply=self.bind(sid,members,2 if triggered else 0)
            if kind in ('BIND','REPLAY-BIND'):
                require(self.status()[0][0]==1 and self.a.state()['bound']==list(members),'configured group bind')
                if kind=='REPLAY-BIND':before=self.a.state();require(self.c.success(bind_request)==bind_reply and self.a.state()==before,'bind replay changed resources')
                return
            if kind=='UNBIND':self.bind(sid,());require(self.status()[0][0]==0,'unbind state');self.c.success(self.req(2,'configure',sid,self.settings(2)));return
            if kind in ('TRACK-LOCK','PLAN-LOCK','QUERY','SESSION'):
                if kind=='TRACK-LOCK':
                    for fn in members:
                        for op in ('configure','start','stop','force'):self.refused(self.req(fn,op,sid,self.settings(fn) if op=='configure' else b''),'unavailable',4)
                elif kind=='PLAN-LOCK':
                    for fn in members:
                        self.refused(self.req(1,'plan_release',sid,b'\1'+struct.pack('<H',fn)),'unavailable',4)
                        role,ch=(1,0) if fn==2 else (0,2)
                        self.refused(self.req(1,'plan_apply',sid,self.tlv(self.assignment,struct.pack('<HBH',fn,role,ch))),'unavailable',4)
                elif kind=='QUERY':
                    for active in (False,True):
                        if active:self.start(sid)
                        for fn in members:
                            before=self.a.state();self.c.success(self.req(fn,'query',payload=self.settings(fn,samples=5,rate=537)));require(self.a.state()==before,'bound query mutated group/track configuration')
                else:
                    for op,p in [('bind',self.binding()),('start',b''),('stop',b''),('force',b'')]:self.refused(self.req(4,op,payload=p),'session_required')
                    self.status()
                return
            if kind=='START-ATOMIC':
                self.a.invalidate(3);self.refused(self.req(4,'start',sid),'unavailable',6,3);return
            generation,gens,start_request,start_reply=self.start(sid,members)
            if kind in ('START','GENERATION','GEN-WRAP','REVERSE'):
                require(self.status()[0][0]==3,'group start state');rows=self.rows();require(self.a.feed(rows)==8,'paired admission');self.data(gens,rows)
                require(self.status()[0][0]==4,'group completion state')
                for fn in members:require(self.segments(fn)[0][3]==self.status()[0][1],'fixture simultaneous track start')
                return
            if kind=='UNBIND-ACTIVE':self.refused(self.req(4,'bind',sid,self.binding(())),'unavailable',6);return
            if kind in ('TRIGGER','FORCE'):
                before=self.a.state()['now_ns'];prefix=[{2:[0,0],3:[0,1]},{2:[1,0],3:[1,0]}]
                require(self.a.feed(prefix)==2 and self.status()[0][0]==2 and self.status()[0][2]==NO_TIME,'group triggered before condition')
                if kind=='FORCE':self.ok(self.req(4,'force',sid))
                tail=[{2:[i,3000],3:[i&1,1]} for i in range(6)]
                require(self.a.feed(tail)==6,'group post-trigger admission')
                status=self.status()[0];require(status[0]==4 and status[2]==before+2000000 and status[3]==(0 if kind=='FORCE' else 2),'common trigger instant/source')
                self.data(gens,prefix+tail)
                for fn in members:
                    row=self.segments(fn)[0];require(row[5]==2 and row[7]==gens[fn],'group pretrigger index/generation')
                    require(row[3]+row[5]*1000000==status[2],'nearest common trigger index')
                return
            if kind in ('REPEAT','RELEASE'):
                self.a.feed(self.rows(4));require(self.status()[0][0]==3 and all(self.status(fn)[0][0]==5 for fn in members),'group aggregates paused tracks as state3')
                retained=self.segments(3);retained_data=self.read(3,gens[3])
                self.ok(self.req(2,'release',sid,struct.pack('<II',gens[2],0)));require(self.status(2)[0][0]==3 and self.status(3)[0][0]==5,'release must be per track')
                self.a.feed(self.rows(4));require(self.segments(2)[0][0]==1 and self.segments(2)[0][6]&1,'resumed track gap')
                require(self.segments(3)==retained and self.read(3,gens[3])==retained_data,'peer release changed other track');return
            if kind in ('ERROR-ADC','ERROR-LOGIC'):
                self.a.feed(self.rows(5));fn=2 if kind=='ERROR-ADC' else 3;other=5-fn
                complete=self.segments(fn);self.a.fail(fn,2);require(self.status()[0][0]==6 and self.status(fn)[0][0]==6 and self.status(other)[0][0]==1,'group error failed to stop peer')
                require(self.segments(fn)==complete and self.segments(other)[-1][2]==1 and self.segments(other)[-1][6]&2,'group error discarded complete/failed short peer')
                self.start(sid);require(self.status()[0][0]==3,'group error restart');return
            if kind in ('END','EXPIRY','SESSION-FORCE'):
                self.a.feed(self.rows(2))
                if kind=='END':self.ok(self.c.request(self.c.core['end'],session=sid));self.c.session=None
                elif kind=='EXPIRY':self.a.advance(1000);self.c.success(self.c.request(self.c.core['lock_state']));self.c.session=None
                else:
                    new=secrets.randbelow(0xffffffff)+1
                    while new==sid:new=secrets.randbelow(0xffffffff)+1
                    self.c.corr=0;self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=new));self.c.session=new
                state=self.a.state();require(not state['bound'] and not state['plans'] and state['pins']==state['idle'],'session closure retained group/plan/pins')
                require(state['generation']==generation and all(t['state']==0 and not t['config'] and not t['data'] and not t['current'] and not t['segments'] and t['generation']==gens[fn] for fn,t in state['tracks'].items()),'session closure retained capture or reset generation');return
            self.a.feed(self.rows(3))
            if kind=='REPLAY-START':
                before=self.a.state();require(self.c.success(start_request)==start_reply and self.a.state()==before,'group start replay applied twice');return
            stop_request=self.req(4,'stop',sid);stop_reply=self.ok(stop_request)
            require(self.status()[0][0]==1 and all(self.status(fn)[0][0]==1 for fn in members),'group stop failed to stop every track')
            for fn in members:require(self.segments(fn)[0][2]==3 and self.segments(fn)[0][6]&2,'group stop short segment')
            if kind=='REPLAY-STOP':before=self.a.state();require(self.c.success(stop_request)==stop_reply and self.a.state()==before,'group stop replay changed data')
            if kind=='REBIND-GEN':
                self.bind(sid,());self.bind(sid,(3,2));new,newgens,_,_=self.start(sid,(3,2));require(new==generation+1 and all(newgens[fn]==gens[fn]+1 for fn in members),'rebind reset generations')
            require(self.a.state()['pins']==self.a.state()['idle'],'group operations changed electrical pin state')
    def run(self):
        start=len(self.c.results)
        def context():
            expected={1:'oep.probe.plan',2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'}
            require(self.a.reset_scope=='logical-model' and self.a.selection==expected,'explicit group fixture selection')
            base=RegisteredOpsChecks(self.c,expected);require(base.run()['status']=='passed','registered group/track ops')
            self.codes={fn:{r['name']:r['code'] for r in base.specs[name]['op']} for fn,name in expected.items()}
            self.tags={fn:base.specs[expected[fn]]['tlv']['configure'] for fn in (2,3)};self.answers={fn:base.specs[expected[fn]]['tlv']['configure_answer'] for fn in (2,3)}
            self.trigger_tag=base.specs[expected[4]]['tlv']['bind']['trigger_track'];self.assignment=base.specs[expected[1]]['tlv']['plan_apply']['role_assignment']
            self.cause_tag=self.c.reg['core']['tlv']['unavailable_payload']['cause'];self.fn_tag=self.c.reg['core']['tlv']['unavailable_payload']['fn']
            self.maximum=int.from_bytes(dict(reversed(self.c.describe(0)))[self.c.reg['core']['tlv']['describe']['max_op_ms']],'little')
            self.payload=AnalogPayloadChecks(self.c);require(self.a.state()['clock_scope']=='explicit-logical-ms','explicit group fixture clock')
        self.c.check('OEP-GROUP-CONTEXT','capture §4; core §7',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        def execute(kind):
            self.case(kind);state=self.a.state();require(state['pins']==state['idle'],'group actions changed electrical state')
        for kind in self.cases:
            first=len(self.a.trace);self.c.check('OEP-GROUP-'+kind,'capture §4.1; plan §2; core §6/8/9',lambda kind=kind:execute(kind));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,
                    scope='explicit equal-rate ADC/logic group binding/start/trigger/stop/error/generation/resource lifetime',
                    unchecked=['multirate pretrigger conversion and nearest-sample rounding across rates','streaming/subscriptions/notifications, other group resources/topologies','early trigger history shortage, serial retention bounds/concurrency/persisted config','physical synchronization/skew/accuracy and production Host'])
