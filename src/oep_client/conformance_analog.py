"""Selected-SPEC ADC operations checked independently of model and Host decoder."""
import struct
from .conformance import require, tlvs
from .conformance_logic import LogicChecks
from .conformance_analog_payload import AnalogPayloadChecks
from .conformance_registered_ops import RegisteredOpsChecks


class AnalogChecks(LogicChecks):
    cases=LogicChecks.cases+('RAW','FRONTEND','CALIBRATION','CALIBRATION-FIT','DOWN',*(f'QUERY-STATE-{state}' for state in range(7)))
    def plan(self,sid,roles=((1,0),(3,1))):return super().plan(sid,roles)
    def answer(self,payload,mode=1):
        self.config=self.payload.configure(payload,roles=(1,3),frontends={0,1},mode=mode)
        require(self.config['slot']==16 and self.config['offset']==2 and self.config['bits']==12 and self.config['order']==[3,1],'explicit ADC layout differs')
        return dict(reversed(tlvs(payload)))
    def data(self,generation,samples):
        start,flags,data=self.read(generation)
        require(start==0 and flags==0,'ADC initial read position/flags')
        self.payload.data(data,self.config,samples)
        return data
    def case(self,kind):
        special={'RAW-TAGS','TRIGGER','PRETRIGGER','WAIT','DOWN','PAGES','REPEAT','RELEASE','REPLAY-RELEASE','RAW','FRONTEND','CALIBRATION','CALIBRATION-FIT'}
        if kind not in special and not kind.startswith('QUERY-STATE-'):return super().case(kind)
        self.fresh()
        with self.c.holding(1000) as sid:
            pins=self.a.state()['pins'];self.plan(sid)
            if kind=='RAW-TAGS':
                for critical in (False,True):
                    self.configure(sid,critical=critical)
                    for field,kwargs in [('mode',dict(mode=4)),('mode',dict(mode=3,samples=None)),('rate',dict(rate=99)),('trigger',dict(trigger=(1,1,0)))]:
                        self.refused(self.req(2,'configure',sid,self.settings(critical=critical,**kwargs)),'unsupported',bytes((self.tags[field]|(128 if critical else 0),)))
                self.refused(self.req(2,'configure',sid,self.settings()+self.tlv(254,b'x')),'unsupported',b'\xfe')
            elif kind=='TRIGGER':
                self.configure(sid,trigger=(0,255,0xffffffff))
                self.refused(self.req(2,'configure',sid,self.settings(trigger=(3,0,1500))),'unavailable',self.tlv(1,b'\6'))
                for trigger in [(1,1,0),(2,1,0),(3,1,4096),(4,3,4096)]:
                    self.refused(self.req(2,'configure',sid,self.settings(trigger=trigger)),'unsupported',bytes((self.tags['trigger'],)))
            elif kind=='PRETRIGGER':
                for pre in (8,9):self.refused(self.req(2,'configure',sid,self.settings(trigger=(3,1,1500),pre=pre)),'unsupported',bytes((self.tags['pretrigger'],)))
                self.configure(sid,trigger=(3,1,1500),pre=7)
            elif kind in ('WAIT','DOWN'):
                down=kind=='DOWN';before=3000 if down else 0;after=0 if down else 3000
                self.configure(sid,trigger=(4 if down else 3,3,1500),pre=2)
                generation,_,_=self.start(sid)
                prefix=[[0,before],[4095,before]];tail=[[i,after] for i in range(6)]
                require(self.a.feed(prefix)==2 and self.status()[0][0]==2,'ADC threshold fired without crossing')
                require(self.a.feed(tail)==6 and self.status()[0][0]==4,'ADC crossing capture did not finish')
                row=self.segments()[0]
                require(row[2]==8 and row[5]==2 and row[7]==generation,'ADC pretrigger segment')
                self.data(generation,prefix+tail)
            elif kind in ('PAGES','REPEAT','RELEASE','REPLAY-RELEASE'):
                self.configure(sid,mode=2,samples=8,segments=2);generation,_,_=self.start(sid)
                require(self.a.feed([[i,4095-i] for i in range(16)])==16,'ADC repeat sample admission')
                rows=self.segments()
                require(len(rows)==2 and rows[0][:3]==(0,0,8) and rows[1][:3]==(1,32,8) and self.status()[0][0]==5,'ADC repeat capacity/positions')
                require(rows[1][3]-rows[0][3]==8000000,'ADC repeat timestamp continuity')
                require(self.segments(1)==[rows[1]] and self.segments(2)==[],'ADC segment paging')
                if kind!='PAGES':
                    retained=self.read(generation,32)[2]
                    request=self.req(2,'release',sid,struct.pack('<II',generation,0));reply=self.ok(request)
                    require(self.status()[0][0]==3 and self.read(generation)[:2]==(32,2),'ADC release resume/read gap')
                    require(self.a.feed([[1500,2500]]*8)==8,'ADC resumed sample admission')
                    new_rows=self.segments()
                    require(new_rows[0]==rows[1] and self.read(generation,32,32)[2]==retained,'ADC release changed retained rows/data')
                    require(new_rows[1][:3]==(2,64,8) and new_rows[1][6]&1,'ADC resumed segment gap')
                    if kind=='REPLAY-RELEASE':
                        before=self.a.state();require(self.c.success(request)==reply and self.a.state()==before,'ADC replay release applied twice')
            elif kind in ('RAW','FRONTEND'):
                p=self.settings()+self.tlv(self.tags['frontend'],b'\3\1')
                self.answer(self.c.success(self.req(2,'configure',sid,p)))
                require(all({1:(0,),3:(1,)}[role]==row for role,row in self.config['frontend_used'].items()) and self.config['scale'][1][1]>0>self.config['scale'][3][1],'ADC selected frontend/negative scale')
                if kind=='RAW':
                    samples=[[0,4095],[1,4094],[17,30000%4096],[2048,1500],[4095,0],[3000,1000],[123,321],[4040,40]]
                    generation,_,_=self.start(sid);require(self.a.feed(samples)==8,'ADC raw admission');self.data(generation,samples)
                else:
                    for value,reason in [(b'\3\1'+b'\0','malformed'),(b'\3\2','unsupported'),(b'\0\1','unavailable')]:
                        self.refused(self.req(2,'configure',sid,self.settings()+self.tlv(self.tags['frontend']|128,value)),reason)
                    self.refused(self.req(2,'configure',sid,p+self.tlv(self.tags['frontend'],b'\3\0')),'malformed')
            elif kind=='CALIBRATION':
                before=self.a.state();initial=self.c.success(self.req(2,'calibration'));self.payload.calibration(initial,frontends={0,1})
                require(self.a.state()==before and self.calibration_tags['vrefint'] not in dict(tlvs(initial)),'calibration before first start changed acquisition/measured value')
                self.configure(sid,trigger=(3,3,1500))
                for _ in range(2):
                    self.a.advance(3);self.start(sid);before=self.a.state()
                    p=self.c.success(self.req(2,'calibration'));self.payload.calibration(p,frontends={0,1})
                    require(self.a.state()==before,'calibration changed acquisition')
                    values=dict(tlvs(p));require(values[self.calibration_tags['factory']]==dict(tlvs(initial))[self.calibration_tags['factory']],'factory calibration changed at start')
                    raw,ns,mv=struct.unpack('<IQI',values[self.calibration_tags['vrefint']])
                    require(raw==1365 and ns==before['now_ns'] and mv==1100,'fixture last-start reference calibration')
                    self.ok(self.req(2,'stop',sid))
            elif kind=='CALIBRATION-FIT':
                def factory():
                    p=self.c.success(self.req(2,'calibration'));return p,{fe:raw for fe,_,raw in self.payload.calibration(p,frontends={0,1})}
                raw=self.a.factory(64);p,rows=factory();require(rows.get(255)==raw,'factory raw returned as held')
                size=64+self.c.max_frame-5-len(p);raw=self.a.factory(size);p,rows=factory();require(5+len(p)==self.c.max_frame and rows.get(255)==raw,'exactly fitting calibration must succeed whole')
                self.a.factory(size+1);self.refused(self.req(2,'calibration'),'unsupported',b'\0')
                self.a.factory(size);self.configure(sid,trigger=(3,3,1500));self.start(sid);self.refused(self.req(2,'calibration'),'unsupported',b'\0')
                raw=self.a.factory(0);p,rows=factory();require(rows.get(255)==raw and self.calibration_tags['vrefint'] in dict(tlvs(p)),'calibration after shrinking held information')
            else:
                state=int(kind.rsplit('-',1)[1])
                if state:
                    self.configure(sid,mode=2 if state in (3,5,6) else 1,samples=8,segments=1 if state==5 else None,
                                   trigger=(3,3,1500) if state==2 else None)
                if state in (2,3,4,5,6):self.start(sid)
                if state in (4,5):self.a.feed([[100,200]]*8)
                if state==6:self.a.feed([[100,200]]*9);self.a.fail(2)
                require(self.status()[0][0]==state,'ADC query fixture state')
                before=self.a.state();self.answer(self.c.success(self.req(2,'query',payload=self.settings(rate=537,samples=5))))
                require(self.a.state()==before,'active ADC query applied settings/data')
            require(self.a.state()['pins']==pins,'ADC acquisition changed electrical pin state')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.selection=={1:'oep.probe.plan',2:'oep.fixture.analog'},'explicit ADC selection')
            base=RegisteredOpsChecks(self.c,self.a.selection);require(base.run()['status']=='passed','registered ADC ops')
            self.codes={fn:{r['name']:r['code'] for r in base.specs[name]['op']} for fn,name in self.a.selection.items()}
            spec=base.specs['oep.fixture.analog'];self.tags=spec['tlv']['configure'];self.answers=spec['tlv']['configure_answer'];self.calibration_tags=spec['tlv']['calibration_answer']
            self.assignment=base.specs['oep.probe.plan']['tlv']['plan_apply']['role_assignment']
            self.payload=AnalogPayloadChecks(self.c)
            require(self.a.state()['clock_scope']=='explicit-logical-ms','explicit ADC model clock')
        self.c.check('OEP-ANALOG-CONTEXT','capture §1.2/3; plan §2',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for kind in self.cases:
            first=len(self.a.trace);self.c.check('OEP-ANALOG-'+kind,'capture §1.2/2/3/3.7; plan §2; core §4.4/6/8/9',lambda kind=kind:self.case(kind))
            self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,
                    scope='explicit ADC u12 rows, sparse roles/reverse layout, configure contract/answer rows, one-shot/repeat, read rules, threshold, generation exhaustion, calibration fit and lifetime',
                    unchecked=['capture-group, streaming, notifications and optional force','other ADC layouts/rates/trigger boundaries and reference acquisition policies','serial wrap and bounded-retention/concurrency/persisted config','physical ADC accuracy/skew/clock and production Host integration'])
