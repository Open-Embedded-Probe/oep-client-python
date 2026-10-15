"""Independent mandatory riscv-dm payload/progress and console arbitration checks."""
import struct
from .conformance import require, tlvs, ops
from .conformance_dmseq import DmseqChecks, target_frame, reply_word


class DebugChecks(DmseqChecks):
    def dmi_request(self,sid,parent,steps,extra=b''):
        return self.request(5,'dmi',sid,struct.pack('<IH',parent,len(steps))+b''.join(steps)+extra)
    def dmi(self,sid,parent,steps,done,status,values):
        req=self.dmi_request(sid,parent,steps);frame=self.c.exchange(req)
        require(len(frame)>=10 and frame[3:5]==bytes((1,0 if status==0 else (2 if done else 1))),'dmi outcome/header')
        actual_done,actual_status,count=struct.unpack_from('<HBH',frame,5)
        require((actual_done,actual_status,count)==(done,status,len(values)) and len(frame)>=10+4*count,'dmi done/status/nvals')
        require([struct.unpack_from('<I',frame,10+4*i)[0] for i in range(count)]==values,'dmi values/order/failed-step value');tlvs(frame[10+4*count:])
        return req,frame
    @staticmethod
    def read(address):return bytes((2,address))
    @staticmethod
    def write_reg(address,value):return bytes((1,address))+struct.pack('<I',value)
    @staticmethod
    def poll_step(kind,address,mask,value,limit):return bytes((kind,address))+struct.pack('<II',mask,value)+struct.pack('<H' if kind==3 else '<I',limit)
    def simple(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid)
            if kind=='EMPTY':self.dmi(sid,parent,[],0,0,[])
            elif kind=='MIXED':
                self.dmi(sid,parent,[self.write_reg(4,0x12345678),self.read(4),self.write_reg(5,0xabcdef01),self.read(5),b'\x04'+struct.pack('<I',0)],5,0,[0x12345678,0xabcdef01])
                require(self.a.state()['targets'][parent]['registers'][4]==0x12345678,'raw DMI host write was restored or lost')
            else:
                steps=[self.write_reg(4,0x55),self.read(4),self.write_reg(5,0xaa),self.read(5)]
                for index in range(4):
                    self.a.failure(index,2);before=self.a.state();self.dmi(sid,parent,steps,index,2,[0x55] if index>=2 else [])
                    delta=self.a.state()['access'][len(before['access']):]
                    require(len([row for row in delta if row[0] in ('dmi-read','dmi-write')])==index,'executed failing step or suffix')
                self.a.failure(None)
    def polling(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.registers(parent,{4:0xa5})
            for limit in (0,1,3):
                code=3 if kind=='READS' else 5
                self.dmi(sid,parent,[self.poll_step(code,4,0xf,5,limit)],1,0,[0xa5])
                before=self.a.state();self.dmi(sid,parent,[self.poll_step(code,4,0xf,6,limit)],0,self.c.reg['status']['timeout'],[0xa5])
                count=sum(row[0]=='dmi-read' for row in self.a.state()['access'][len(before['access']):])
                require(count==max(1,limit) if code==3 else (count==1 if limit==0 else count>=1),'poll zero limit did not read once')
    def rejection(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);reason='malformed';steps=[self.write_reg(4,99)];extra=b''
            if kind=='UNKNOWN-LATE':steps.append(b'\xff');reason='unsupported'      # an unknown kind: 0x00 (debug §4.1)
            elif kind=='TRUNCATED':steps.append(b'\x01\x05x')
            elif kind=='TIME-SUM':steps+=[b'\x04'+struct.pack('<I',600),self.poll_step(5,4,0,0,401)];reason='unsupported'
            elif kind=='CRITICAL':extra=self.tlv(254,b'x');reason='unsupported'
            else:extra=b'\x7f\x05\0x'
            self.refused(self.dmi_request(sid,parent,steps,extra),reason,b'\xfe' if kind=='CRITICAL' else (b'\0' if reason=='unsupported' else None))
    def resources(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);stream,_=self.console(sid,parent,2)
            self.refused(self.dmi_request(sid,65535,[]),'no_resource')
            self.refused(self.dmi_request(sid,stream,[]),'unavailable',cause=6)
            arm,_=self.attach(sid,fn=3);self.refused(self.dmi_request(sid,arm,[]),'unavailable',cause=6)
    def session(self):
        self.fresh();self.refused(self.dmi_request(0,65535,[b'\xff']),'session_required')
        with self.c.holding(3000) as sid:self.refused(self.dmi_request(sid%0xffffffff+1,65535,[b'\xff']),'locked')
    def arbitration(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);rid,_=self.console(sid,parent,2);self.a.frame(rid,*target_frame(syn=True,data=b'x'))
            before=self.a.state();steps=[self.read(4),self.write_reg(5,0x12345678),self.read(5)]
            self.dmi(sid,parent,steps,3,0,[target_frame(syn=True,data=b'x')[0],0x12345678]);after=self.a.state()
            polls=after['arbitration'][len(before['arbitration']):]
            require(len(polls)==3 and all(row['events']==[] and row['connection']==parent for row in polls),'console interleaved inside standard dmi request')
            require(after['streams'][rid]['data']==b'' and after['protocols'][rid]==before['protocols'][rid],'dmi request consumed console mailbox')
            require(after['mailboxes'][rid]['data1']==0x12345678,'raw host DATA1 write did not remain')
            self.a.poll(rid);after=self.a.state();require(after['mailboxes'][rid]['events'][:2]==[('read',0x11,0xc00),('read',4,target_frame(syn=True,data=b'x')[0])],'console did not re-read DMSTATUS after dmi response')
            require(after['streams'][rid]['data']==b'x','console did not resume after dmi response')
    def halt_resume(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);rid,_=self.console(sid,parent,2);self.a.frame(rid,*target_frame(syn=True,data=b'x'))
            for name in ('halt','halt','resume'):
                value=self.c.success(self.request(5,name,sid,struct.pack('<I',parent)));require(value[:1]==b'\0','halt/resume status');tlvs(value[1:])
                require(not self.a.state()['targets'][parent]['haltreq'],'halt/resume left haltreq asserted')
                before=self.a.state();self.a.poll(rid);after=self.a.state()
                if name=='halt':require(after['mailboxes'][rid]['events']==[('read',0x11,0x300)] and after['streams'][rid]==before['streams'][rid],'console touched mailbox while halted')
            after=self.a.state();require(after['streams'][rid]['data']==b'x' and not after['targets'][parent]['haltreq'],'resume failed to clear haltreq/restart console')
            require(sum(row[0]=='resume' for row in after['access'])==1,'resume resumereq repeated')
    def replay(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);req,result=self.dmi(sid,parent,[self.write_reg(4,123),self.read(4)],2,0,[123])
            self.a.registers(parent,{4:456});before=self.a.state();require(self.c.exchange(req)==result and self.a.state()==before,'dmi replay repeated target access')
    def overall_timeout(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.registers(parent,{4:0xa5})
            self.dmi(sid,parent,[self.poll_step(3,4,15,6,200)],0,4,[0xa5])
            require(sum(row[0]=='dmi-read' for row in self.a.state()['access'])==100,'logical max_op_ms failed to stop read-count poll')
    def poll_failure(self,status):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.registers(parent,{4:123});self.a.failure(0,status)
            self.dmi(sid,parent,[self.poll_step(3,4,255,124,3)],0,status,[123] if status==4 else [])
    def hart_failed(self,resume):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid)
            if resume:self.c.success(self.request(5,'halt',sid,struct.pack('<I',parent)))
            self.a.hart_failure(True);name='resume' if resume else 'halt';before=self.a.state()
            frame=self.c.exchange(self.request(5,name,sid,struct.pack('<I',parent)))
            require(frame[3:6]==bytes((1,1,5 if resume else 4)),'halt/resume failure status/outcome');tlvs(frame[6:])
            after=self.a.state();require(not after['targets'][parent]['haltreq'],'failed halt/resume left haltreq set')
            require({k:v for k,v in after['targets'][parent]['registers'].items() if k!=0x10}=={k:v for k,v in before['targets'][parent]['registers'].items() if k!=0x10},'failed halt/resume changed registers other than DMCONTROL')
            require(not after['targets'][parent]['registers'].get(0x10,0)&(0x80000000|(0x3ff<<6)|(0x3ff<<16)),'failed halt/resume left haltreq/hartsel in DMCONTROL')
            if resume:require(sum(row[0]=='resume' for row in after['access'])==1,'failed resume repeated resumereq')
    def hart_selection(self,resume=False,failed=False,raw=False,already=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);other,_=self.attach(sid,pair=self.a.pairs[1][1])
            mask=(0x3ff<<6)|(0x3ff<<16);control=mask|0x80000001
            if resume or already:self.c.success(self.request(5,'halt',sid,struct.pack('<I',parent)))
            self.a.registers(parent,{0x10:control});self.a.registers(other,{0x10:control});before=self.a.state()
            if raw:
                self.dmi(sid,parent,[self.write_reg(0x10,control)],1,0,[])
                require(self.a.state()['targets'][parent]['registers'][0x10]==control,'raw host DMI hartsel was silently cleared')
            else:
                self.a.hart_failure(failed);frame=self.c.exchange(self.request(5,'resume' if resume else 'halt',sid,struct.pack('<I',parent)))
                require(len(frame)>=6 and frame[3:5]==bytes((1,1 if failed else 0)),'high-level hart operation resolution/outcome')
                value=frame[5:];require(value[0]==((5 if resume else 4) if failed else 0),'high-level hart operation status');tlvs(value[1:])
                state=self.a.state();actual=state['targets'][parent]['registers'][0x10]
                require(not actual&mask,'high-level op returned with nonzero hartsel')
                if already:require(state['targets'][parent]['halted'] and state['access']==before['access'],'already halted hart was halted again')
                # every halt lowers haltreq too: after allhalted, when already halted, or when the wait gives up (debug §4, §4.2)
                require(not actual&0x80000000 and not state['targets'][parent]['haltreq'],'halt/resume left haltreq asserted')
            require(self.a.state()['targets'][other]==before['targets'][other],'high-level hart selection affected another connection')
    def output_bound(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);count=(self.c.max_frame-10)//4
            require(20+2*(count+1)<=self.c.max_frame and (count+1)*10<1000,'selected frame does not fit logical boundary fixture')
            # Include a write before too many reads; refusal must precede it.
            self.refused(self.dmi_request(sid,parent,[self.write_reg(4,123)]+[self.read(4)]*(count+1)),'unsupported',b'\0')
            self.a.registers(parent,{4:17});self.dmi(sid,parent,[self.read(4)]*count,count,0,[17]*count)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical standard debug fixture')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            require(self.specs[5]['enum']['dmi_step']==dict(write=1,read=2,poll_reads=3,wait_us=4,poll_us=5),'selected dmi step grammar differs')
            require(self.c.reg['status']==dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5),'selected standard status meanings differ')
            self.c.confirm();self.c.identity();self.c.declarations();self.offered=ops(dict(reversed(self.c.describe(5)))[7])
            require(16 in self.offered,'mandatory dmi missing')   # debug §4: dmi is the one mandatory op; halt / resume are optional
            require(self.a.state()['max_op_ms']==1 and self.a.state()['read_cost_us']==10,'selected logical budget differs from fixture')
        self.c.check('OEP-DEBUG-CONTEXT','debug §4/4.1/4.2; console §3',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[(kind,lambda kind=kind:self.simple(kind)) for kind in ('EMPTY','MIXED','PROGRESS')]
        cases += [('POLL-'+kind,lambda kind=kind:self.polling(kind)) for kind in ('READS','US')]
        cases += [(kind,lambda kind=kind:self.rejection(kind)) for kind in ('UNKNOWN-LATE','TRUNCATED','TIME-SUM','CRITICAL','TLV-STRUCTURE')]
        cases += [('RESOURCES',self.resources),('SESSION',self.session),('ARBITRATION',self.arbitration),('HALT-RESUME',self.halt_resume),('REPLAY',self.replay)]
        cases += [('HARTSEL-HALT',self.hart_selection),('HARTSEL-HALT-ALREADY',lambda:self.hart_selection(already=True)),('HARTSEL-RESUME',lambda:self.hart_selection(True)),('HARTSEL-HALT-FAIL',lambda:self.hart_selection(failed=True)),('HARTSEL-RESUME-FAIL',lambda:self.hart_selection(True,True)),('HARTSEL-RAW',lambda:self.hart_selection(raw=True))]
        cases += [('OUTPUT-BOUND',self.output_bound),('OVERALL-TIMEOUT',self.overall_timeout),('HALT-FAILURE',lambda:self.hart_failed(False)),('RESUME-FAILURE',lambda:self.hart_failed(True))]
        cases += [('POLL-LINE',lambda:self.poll_failure(2)),('POLL-TIMEOUT',lambda:self.poll_failure(4))]
        # the cases that send halt / resume run only when the fixture offers them (debug §4: optional, declared in ops)
        needs={'HALT-RESUME':{17,18},'HARTSEL-HALT':{17},'HARTSEL-HALT-ALREADY':{17},'HARTSEL-RESUME':{17,18},'HARTSEL-HALT-FAIL':{17},
               'HARTSEL-RESUME-FAIL':{17,18},'HALT-FAILURE':{17},'RESUME-FAILURE':{17,18}}
        offered=getattr(self,'offered',None)
        if offered is not None:cases=[(name,case) for name,case in cases if needs.get(name,set())<=offered]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-DEBUG-'+name,'debug §4.1/4.2; console §3; core §5/9',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(r['status']=='passed' for r in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical mandatory dmi/halt/resume and same-connection console arbitration','unchecked':['optional reset/step/run/block operations and state restoration','hartsel of optional operations and physical hart selection','physical DMI/timeouts/line retry','other-connection scheduling','actual response writer ordering','production Host integration']}
