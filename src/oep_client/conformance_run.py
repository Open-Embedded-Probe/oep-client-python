"""Independent optional run admission, stopped results and loader-state checks."""
import struct
from .conformance import require, tlvs, ops
from .conformance_reset import ResetChecks
from .conformance_blocks import HARTSEL
from .conformance_dmseq import target_frame


class RunChecks(ResetChecks):
    inputs=((0x100a,0x12345678),(0x100b,0xabcdef01))
    outputs=(0x100b,0x100a,0x100b,0x100c)
    def run_request(self,sid,parent,pc=0x2000,timeout=1,inputs=None,outputs=None,extra=b''):
        inputs=self.inputs if inputs is None else inputs;outputs=self.outputs if outputs is None else outputs
        return self.request(5,'run',sid,struct.pack('<HIIB',parent,pc,timeout,len(inputs))+b''.join(struct.pack('<HI',*row) for row in inputs)+bytes((len(outputs),))+b''.join(struct.pack('<H',reg) for reg in outputs)+extra)
    def loader(self,sid,parent,mode='stopped',pc=0x2000,after_pc=0x2004,timeout=1,inputs=None,outputs=None):
        inputs=self.inputs if inputs is None else inputs;outputs=self.outputs if outputs is None else outputs
        self.a.run_behavior(mode,after_pc);before=self.a.state();request=self.run_request(sid,parent,pc,timeout,inputs,outputs);frame=self.c.exchange(request)
        stopped={'stopped':1,'timeout-halted':0,'not-halted':2,'prepare-line':3,'prepare-fault':3}[mode]
        status={'stopped':0,'timeout-halted':4,'not-halted':4,'prepare-line':2,'prepare-fault':3}[mode];valid=stopped in (0,1)
        require(len(frame)>=16 and frame[3:5]==bytes((1,int(bool(status)))),'run completed outcome')
        got_status,got_stopped,dpc,elapsed,nvals=struct.unpack_from('<BBIIB',frame,5)
        require((got_status,got_stopped,dpc,nvals)==(status,stopped,after_pc if valid else 0,len(outputs) if valid else 0),'run stopped/status/dpc/nvals validity')
        size=16+4*nvals;require(len(frame)>=size,'run output truncated');tlvs(frame[size:])
        expected=dict(before['targets'][parent]['gprs']);expected.update({reg-0x1000:value for reg,value in inputs});expected.update(before['programs'][parent]['gprs'])
        values=[struct.unpack_from('<I',frame,16+4*i)[0] for i in range(nvals)]
        require(values==([expected.get(reg-0x1000,0) for reg in outputs] if valid else []),'run output order/duplicate values')
        after=self.a.state();target=after['targets'][parent];access=after['access'][len(before['access']):]
        resumes=[row for row in access if row[0]=='run-resume'];finishes=[row for row in access if row[0]=='run-finish']
        require(len(resumes)==int(stopped!=3) and len(finishes)==int(stopped!=3),'run issued resume twice/before preparation')
        rehalts=[row for row in access if row[0]=='run-rehalt']
        require(len(rehalts)==int(stopped in (0,2)),'missing/unexpected run re-halt')
        if stopped==3:require(elapsed==0 and target['halted'],'preparation failure elapsed/hart state')
        else:
            require(elapsed==finishes[0][2]-resumes[0][2],'elapsed_us counted preparation or omitted halt wait')
            require(elapsed==(100 if stopped==1 else timeout*1000+(200 if stopped==0 else 300)),'selected logical run duration differs')
            require(target['pc']==after_pc and target['gprs']==expected,'run reverted host/program PC/GPR changes')
            require(target['dcsr']&0x8003==0x8003,'run reverted ebreakm/prv M')
            require(target['halted']==(stopped!=2),'run final halted state')
        require(not target['haltreq'] and not target['registers'][0x10]&(HARTSEL|0x80000000),'run left haltreq/hartsel')
        for reg in (4,5,0x18):require(target['registers'].get(reg,0)==before['targets'][parent]['registers'].get(reg,0),'run DATA/abstractauto restoration')
        require([row[2] for row in after['restore'][len(before['restore']):] if row[2] in (4,5)]==[5,4],'run DATA1/DATA0 restore order')
        return request,frame
    def outcome_run(self,mode,pc=0x2000,after=0x2004,timeout=1,empty=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.seed_step(parent,0x1000,0x40000000);self.loader(sid,parent,mode,pc,after,timeout,inputs=() if empty else None,outputs=() if empty else None)
    def frame_run(self,over=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);count=(self.c.max_frame-16)//4
            require(22+2*(count+1)<=self.c.max_frame,'explicit frame capacity premise')
            if over:self.refused(self.run_request(sid,parent,inputs=(),outputs=(0x100a,)*(count+1)),'unsupported',b'\0')
            else:self.loader(sid,parent,inputs=(),outputs=(0x100a,)*count)
    def program_run(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.program(parent,{self.base:0xbad00001,self.base+4:0xbad00002},{10:0xfeed0001,11:0xfeed0002})
            self.loader(sid,parent);self.block(sid,parent,values=[0xbad00001,0xbad00002])
    def console_run(self,mode):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);rid,_=self.console(sid,parent,2);data=target_frame(syn=True,data=b'wire');self.a.frame(rid,*data)
            other,_=self.attach(sid,pair=(2,3));other_rid,_=self.console(sid,other,2);before=self.a.state();self.loader(sid,parent,mode);after=self.a.state()
            require(after['mailboxes'][rid]['data0']==data[0] and after['mailboxes'][rid]['data1']==data[1] and after['protocols'][rid]==before['protocols'][rid],'run damaged mailbox/pending')
            require(after['streams'][rid]==before['streams'][rid],'run damaged console buffer/marks/queue')
            for field in ('targets','streams','mailboxes','protocols'):
                key=other if field=='targets' else other_rid;require(after[field][key]==before[field][key],'run changed other connection')
            polls=after['arbitration'][len(before['arbitration']):];require(polls and all(not row['events'] for row in polls),'console accessed run scratch')
            if mode!='not-halted':self.c.success(self.request(5,'resume',sid,struct.pack('<H',parent)))
            self.a.poll(rid);require(self.a.state()['streams'][rid]['data']==b'wire','console did not resume after run')
    def replay_run(self,mode):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);req,frame=self.loader(sid,parent,mode);self.a.run_behavior('stopped',0x3000);before=self.a.state()
            require(self.c.exchange(req)==frame and self.a.state()==before,'run replay executed/recomputed loader')
    def refusal_run(self,kind):
        self.fresh()
        if kind=='SESSION':self.refused(self.request(5,'run',0,b'\xfe'),'session_required');return
        with self.c.holding(3000) as sid:
            parent=self.parent(sid)
            if kind=='TIMEOUT':self.refused(self.run_request(sid,parent,timeout=2),'unsupported',b'\0')
            elif kind=='CRITICAL':self.refused(self.run_request(sid,parent,extra=self.tlv(254,b'x')),'unsupported',b'\xfe')
            elif kind=='TLV':self.refused(self.run_request(sid,parent,extra=b'\x7f\x05\0x'),'malformed')
            elif kind=='INPUT-COUNT':self.refused(self.request(5,'run',sid,struct.pack('<HIIBHI',parent,0x2000,1,2,0x100a,1)),'malformed')
            elif kind=='OUTPUT-COUNT':self.refused(self.request(5,'run',sid,struct.pack('<HIIBBH',parent,0x2000,1,0,2,0x100a)),'malformed')
            elif kind=='TRUNCATED':self.refused(self.request(5,'run',sid,b'\x01\0'),'malformed')
            elif kind=='FOREIGN':self.refused(self.request(5,'run',sid%0xffffffff+1,b'\xfe'),'locked')
            elif kind=='RESOURCE':
                self.refused(self.run_request(sid,65535),'no_resource');rid,_=self.console(sid,parent,2);self.refused(self.run_request(sid,rid),'unavailable',cause=6)
                arm,_=self.attach(sid,fn=3);self.refused(self.run_request(sid,arm),'unavailable',cause=6)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical run fixture')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            require(self.operations[5]['run']==22 and self.specs[5]['enum']['run_stopped']==dict(timeout_halted=0,stopped=1,not_halted=2,not_run=3),'selected run grammar differs')
            require(self.c.reg['status']==dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5),'selected standard status meanings differ')
            self.c.confirm();self.c.identity();self.c.declarations();require(22 in ops(dict(reversed(self.c.describe(5)))[7]),'optional run not offered')
            require(self.a.state()['max_op_ms']==1,'explicit run budget differs')
        self.c.check('OEP-RUN-CONTEXT','debug §4/4.4; core §4.4',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[(mode.upper(),lambda mode=mode:self.outcome_run(mode)) for mode in ('stopped','timeout-halted','not-halted','prepare-line','prepare-fault')]
        cases += [('SAME-PC',lambda:self.outcome_run('stopped',after=0x2000)),('U32-PC',lambda:self.outcome_run('stopped',pc=0xfffffffc,after=0)),('EMPTY',lambda:self.outcome_run('stopped',empty=True)),('ZERO-TIMEOUT',lambda:self.outcome_run('timeout-halted',timeout=0)),('FRAME-BOUND',lambda:self.frame_run()),('FRAME-OVER',lambda:self.frame_run(True)),('PROGRAM-EFFECTS',self.program_run)]
        cases += [('CONSOLE-'+mode.upper(),lambda mode=mode:self.console_run(mode)) for mode in ('stopped','timeout-halted','not-halted','prepare-fault')]
        cases += [('REPLAY-'+mode.upper(),lambda mode=mode:self.replay_run(mode)) for mode in ('stopped','timeout-halted','not-halted')]
        cases += [(kind,lambda kind=kind:self.refusal_run(kind)) for kind in ('TIMEOUT','CRITICAL','TLV','INPUT-COUNT','OUTPUT-COUNT','TRUNCATED','SESSION','FOREIGN','RESOURCE')]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-RUN-'+name,'debug §4/4.4; console §3; core §4/5/9',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical optional loader run results, elapsed accounting and program-state retention',
                'unchecked':['physical abstract register/CSR/CPU execution and clock accuracy','all transport route frame capacities','unsupported abstract register and partial preparation policies','initial running-hart admission','failure while reading result/restoring DATA','program mailbox writes during execution','actual response-writer ordering','other targets/capacities and production Host integration']}
