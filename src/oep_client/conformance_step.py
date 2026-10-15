"""Independent optional riscv-dm step result, fallback and mailbox checks."""
import struct
from .conformance import require, tlvs, ops
from .conformance_blocks import BlockChecks, HARTSEL
from .conformance_dmseq import target_frame


class StepChecks(BlockChecks):
    def request_step(self,sid,parent,extra=b''):return self.request(5,'step',sid,struct.pack('<I',parent)+extra)
    def step(self,sid,parent,mode,before_pc,after_pc):
        self.a.step_behavior(mode,after_pc);before=self.a.state();req=self.request_step(sid,parent);frame=self.c.exchange(req)
        status={'ok':0,'late-halt':5,'running':5,'prepare-line':2,'prepare-fault':3}[mode]
        require(len(frame)>=15 and frame[3:5]==bytes((1,int(bool(status)))),'step outcome/header')
        expected=(status,int(before_pc!=after_pc),before_pc,after_pc) if not status else (status,0,0,0)
        require(struct.unpack_from('<BBII',frame,5)==expected,'step status/moved/PC validity')
        tags=dict(reversed(tlvs(frame[15:])));left=self.specs[5]['tlv']['step_answer']['step_left']
        if mode=='running':require(tags.get(left)==b'','step_left missing or has nonzero length')
        else:require(left not in tags,'step_left on successful re-halt/preparation failure')
        after=self.a.state();prior=before['targets'][parent]
        if mode=='running':                                # the cleanup could not finish: closed after the answer
            mine=[rid for rid,s in before['streams'].items() if s['connection']==parent]
            require(parent not in after['connections'] and not any(rid in after['streams'] for rid in mine),'step_left kept the connection or its streams')
            require(sum(row[0]=='step-resume' for row in after['access'][len(before['access']):])==1,'step retried resume')
            return req,frame
        target=after['targets'][parent]
        require(target['dcsr']&3==prior['dcsr']&3,'step changed prv')
        if mode!='running':require(not target['dcsr']&4,'step bit not lowered')
        require(not target['haltreq'] and not target['registers'][0x10]&(HARTSEL|0x80000000),'step left haltreq/hartsel')
        require(target['halted']==(mode!='running'),'step final hart state')
        for reg in (4,5,0x18):require(target['registers'].get(reg,0)==prior['registers'].get(reg,0),'step DATA/abstractauto changed')
        require(target['gprs']==prior['gprs'] and after['memory']==before['memory'],'step changed unrelated GPR/memory')
        delta=after['restore'][len(before['restore']):]
        require([row[2] for row in delta if row[2] in (4,5)]==[5,4],'step DATA1/DATA0 restore order')
        resumes=sum(row[0]=='step-resume' for row in after['access'][len(before['access']):])
        require(resumes==(0 if mode.startswith('prepare-') else 1),'step retried resume or ran before preparation')
        rehalts=sum(row[0]=='step-rehalt' for row in after['access'][len(before['access']):])
        require(rehalts==int(mode in ('late-halt','running')),'missing/unexpected re-halt attempt')
        if mode.startswith('prepare-'):require(target['pc']==prior['pc'],'failed preparation changed PC')
        return req,frame
    def completed(self,pc,after,prv=3,initial_step=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.seed_step(parent,pc,0x40008000|prv|(4 if initial_step else 0))
            self.step(sid,parent,'ok',pc,after)
    def failure_step(self,mode):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.seed_step(parent,0x1000,0x40008003)
            self.step(sid,parent,mode,0x1000,0x1004)
    def mailbox_step(self,mode):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);rid,_=self.console(sid,parent,2);data=target_frame(syn=True,data=b'wire');self.a.frame(rid,*data)
            self.a.seed_step(parent,0x1000,0x40008003);before=self.a.state();self.step(sid,parent,mode,0x1000,0x1004);after=self.a.state()
            if mode=='running':require(rid not in after['streams'],'console stream outlived its connection');return
            require(after['mailboxes'][rid]['data0']==data[0] and after['mailboxes'][rid]['data1']==data[1] and after['protocols'][rid]==before['protocols'][rid],'step damaged console frame/pending')
            polls=after['arbitration'][len(before['arbitration']):];require(polls and all(not row['events'] for row in polls),'console accessed mailbox during step')
            self.c.success(self.request(5,'resume',sid,struct.pack('<I',parent)))
            self.a.poll(rid);require(self.a.state()['streams'][rid]['data']==b'wire','console did not resume with restored frame')
    def refusal_step(self,kind):
        self.fresh()
        if kind=='SESSION':self.refused(self.request_step(0,65535,b'\xfe'),'session_required');return
        with self.c.holding(3000) as sid:
            parent=self.parent(sid)
            if kind=='CRITICAL':self.refused(self.request_step(sid,parent,self.tlv(254,b'x')),'unsupported',b'\xfe')
            elif kind=='TLV':self.refused(self.request_step(sid,parent,b'\x7f\x05\0x'),'malformed')
            elif kind=='TRUNCATED':self.refused(self.request(5,'step',sid,b'\x01\0\0'),'malformed')
            elif kind=='FOREIGN':self.refused(self.request_step(sid%0xffffffff+1,parent,b'\xfe'),'locked')
            elif kind=='RESOURCE':
                self.refused(self.request_step(sid,65535),'no_resource');rid,_=self.console(sid,parent,2);self.refused(self.request_step(sid,rid),'unavailable',cause=6)
                arm,_=self.attach(sid,fn=3);self.refused(self.request_step(sid,arm),'unavailable',cause=6)
    def replay_step(self,mode):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.seed_step(parent,0x1000,0x40008003);req,result=self.step(sid,parent,mode,0x1000,0x1004)
            self.a.step_behavior('ok',0x2000);before=self.a.state();require(self.c.replayed(req,result,frame=True) and self.a.state()==before,'step replay executed/resumed/recomputed')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical step fixture')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            require(self.c.reg['status']==dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5),'selected standard status meanings differ')
            require(self.operations[5]['step']==23 and self.specs[5]['tlv']['step_answer']['step_left']==1,'selected step grammar mismatch')
            self.c.confirm();self.c.identity();self.c.declarations();require(23 in ops(dict(reversed(self.c.describe(5)))[7]),'optional step not offered by selected fixture')
        self.c.check('OEP-STEP-CONTEXT','debug §4/4.2; explicit fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('MOVED',lambda:self.completed(0x1000,0x1004)),('SELF-BRANCH',lambda:self.completed(0x1000,0x1000)),('U32-PC',lambda:self.completed(0xfffffffc,0)),('OLD-STEP',lambda:self.completed(0x1000,0x1004,initial_step=True))]
        cases += [('PRV-'+str(prv),lambda prv=prv:self.completed(0x1000,0x1004,prv)) for prv in (0,1)]
        cases += [(mode.upper(),lambda mode=mode:self.failure_step(mode)) for mode in ('late-halt','running','prepare-line','prepare-fault')]
        cases += [('MAILBOX-'+mode.upper(),lambda mode=mode:self.mailbox_step(mode)) for mode in ('ok','late-halt','running')]
        cases += [(kind,lambda kind=kind:self.refusal_step(kind)) for kind in ('CRITICAL','TLV','TRUNCATED','SESSION','FOREIGN','RESOURCE')]
        cases += [('REPLAY-'+mode.upper(),lambda mode=mode:self.replay_step(mode)) for mode in ('ok','running')]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-STEP-'+name,'debug §4/4.2; console §3; core §5/9',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical optional step result and fallback/scratch restoration',
                'unchecked':['physical instructions/abstract CSR access and re-halt timing','DATA restoration failure','initial running-hart admission','actual response-writer ordering','reset/run','other targets and production Host integration']}
