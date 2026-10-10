"""Independent optional reset flags, DM recovery, state and console checks."""
import struct
from .conformance import require, tlvs, ops
from .conformance_step import StepChecks
from .conformance_blocks import HARTSEL
from .conformance_dmseq import target_frame


class ResetChecks(StepChecks):
    def reset_request(self,sid,parent,mode,extra=b''):return self.request(5,'reset',sid,struct.pack('<HB',parent,mode)+extra)
    def reset(self,sid,parent,mode,result):
        self.a.reset_behavior(result);before=self.a.state();request=self.reset_request(sid,parent,mode);frame=self.c.exchange(request)
        good=result in ('ok','dm-recover','verified-halted');status=0 if good else {'verify-timeout':4,'state-timeout':4,'dm-line':2,'fault':3}[result]
        flags=(2 if result=='verified-halted' else 3 if mode==1 else 1) if good else int(result=='verify-timeout')
        pc=(0 if mode==0 else 0x2004 if mode==1 else 0x2000) if good else 0
        require(len(frame)>=11 and frame[3:5]==bytes((1,int(not good))),'reset completed outcome')
        require(struct.unpack_from('<BBI',frame,5)==(status,flags,pc),'reset status/flags/PC semantics');tlvs(frame[11:])
        require(bool(flags&(2 if mode==1 else 1))==good and not flags&~3 and (mode==1 or not flags&2),'reset success condition/reserved flags')
        after=self.a.state();target=after['targets'][parent];access=after['access'][len(before['access']):]
        require(after['connections']==before['connections'] and after['next_id']==before['next_id'] and after['touch']==before['touch'],'reset touched pins or destroyed connection')
        require(not target['registers'][0x10]&(HARTSEL|2),'reset left hartsel or ndmreset')
        if mode!=2:require(not target['haltreq'] and not target['registers'][0x10]&0x80000000,'reset left haltreq')
        if good:require(target['halted']==(mode==2 or result=='verified-halted'),'reset did not reach fixture state')
        asserted=[i for i,row in enumerate(access) if row[0]=='ndmreset-assert'];released=[i for i,row in enumerate(access) if row[0]=='ndmreset-release']
        require(len(asserted)==len(released)==1 and asserted[0]<released[0],'reset ndmreset sequence')
        require(access[released[0]][2]==(mode==2),'mode 2 did not hold haltreq during ndmreset release')
        polls=[(i,row) for i,row in enumerate(access) if row[0]=='reset-dm-read']
        expected=[2,2,0] if result=='dm-recover' else [2]*5 if result=='dm-line' else [0]
        require([row[2] for _,row in polls]==expected and all(i>released[0] for i,_ in polls),'DM not retried after ndmreset release')
        require(all(0<=row[3]<before['max_op_ms']*1000 for _,row in polls),'DM read beyond overall budget')
        acks=[row for row in access if row[0]=='ack-havereset']
        require(len(acks)==int(result!='dm-line'),'havereset acknowledgement missing/without observed DM')
        if mode==1 and good:
            saved=[row for row in access if row[0]=='reset-verify-before'];require(len(saved)==1,'mode 1 verification missing')
            require(target['dcsr']==saved[0][2] and not target['haltreq'],'internal verification left dcsr/step/haltreq')
            for reg in (4,5):require(target['registers'].get(reg,0)==saved[0][3][reg],'internal verification DATA not restored')
            require([row[2] for row in after['restore'][len(before['restore']):]]==[5,4],'internal verification DATA1/DATA0 order')
        return request,frame
    def outcome_reset(self,mode,result):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.seed_step(parent,0x1000,0x40008001);self.reset(sid,parent,mode,result)
    def console_reset(self,mode,result='dm-recover'):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid,halt=False);rid,_=self.console(sid,parent,2)
            self.c.success(self.request(4,'write',sid,struct.pack('<HH',rid,3)+b'ABC'));self.a.frame(rid,*target_frame(syn=True,data=b'before'));self.a.poll(rid)
            other,_=self.attach(sid,pair=(2,3));other_rid,_=self.console(sid,other,2);self.a.frame(other_rid,*target_frame(syn=True,data=b'peer'));self.a.poll(other_rid)
            self.c.success(self.request(5,'halt',sid,struct.pack('<H',parent)));before=self.a.state();self.reset(sid,parent,mode,result);after=self.a.state()
            require(before['protocols'][rid]['synced'] and before['protocols'][rid]['pending']==b'AB','explicit pending/reset premise')
            if result!='dm-line':require(not after['protocols'][rid]['synced'] and after['protocols'][rid]['pending']==b'','reset retained dmseq sync/pending')
            for field in ('data','position','tx'):require(after['streams'][rid][field]==before['streams'][rid][field],'reset lost live buffer/queue')
            marks=after['streams'][rid]['marks'];new=marks[len(before['streams'][rid]['marks']):]
            require([(row[2],row[-1]) for row in new]==([(1,1)] if result=='dm-line' else [(1,1),(2,1)]) and all(row[1]==before['streams'][rid]['position'] for row in new),'reset/restart mark kind/detail/position')
            for mark in new:
                page=self.c.success(self.request(4,'marks',0,struct.pack('<HI',rid,mark[0])));count=page[1]
                require(count and struct.unpack_from('<IQBQB',page,2)==mark,'reset mark wire readback');tlvs(page[2+22*count:])
            for field in ('targets','streams','mailboxes','protocols'):
                key=other if field=='targets' else other_rid;require(after[field][key]==before[field][key],'reset changed other connection')
            polls=after['arbitration'][len(before['arbitration']):];require(polls and all(not row['events'] for row in polls),'console accessed reset scratch')
    def replay_reset(self,result):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.console(sid,parent,2);request,frame=self.reset(sid,parent,1,result);self.a.reset_behavior('fault');before=self.a.state()
            require(self.c.exchange(request)==frame and self.a.state()==before,'reset replay executed ndmreset or recomputed answer')
    def refusal_reset(self,kind):
        self.fresh()
        if kind=='SESSION':self.refused(self.reset_request(0,65535,255,b'\xfe'),'session_required');return
        with self.c.holding(3000) as sid:
            parent=self.parent(sid)
            if kind=='MODE':self.refused(self.reset_request(sid,parent,3),'unsupported',b'\0')
            elif kind=='CRITICAL':self.refused(self.reset_request(sid,parent,0,self.tlv(254,b'x')),'unsupported',b'\xfe')
            elif kind=='TLV':self.refused(self.reset_request(sid,parent,0,b'\x7f\x05\0x'),'malformed')
            elif kind=='TRUNCATED':self.refused(self.request(5,'reset',sid,struct.pack('<H',parent)),'malformed')
            elif kind=='RESOURCE':
                self.refused(self.reset_request(sid,65535,0),'no_resource');rid,_=self.console(sid,parent,2);self.refused(self.reset_request(sid,rid,0),'unavailable',cause=6)
                arm,_=self.attach(sid,fn=3);self.refused(self.reset_request(sid,arm,0),'unavailable',cause=6)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical reset fixture')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            require(self.operations[5]['reset']==19 and self.specs[5]['enum']['reset_mode']==dict(run=0,run_verified=1,halt_at_reset=2) and self.specs[5]['enum']['reset_flags']==dict(reached=1,verified=2),'selected reset grammar mismatch')
            require(self.c.reg['status']==dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5),'selected standard status meanings differ')
            self.c.confirm();self.c.identity();self.c.declarations();require(19 in ops(dict(reversed(self.c.describe(5)))[7]),'optional reset not offered by selected fixture')
            require(self.a.state()['max_op_ms']==1,'explicit reset budget differs')
        self.c.check('OEP-RESET-CONTEXT','debug §4/4.3/4.6; common §1 marks',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[(result.upper()+'-'+str(mode),lambda mode=mode,result=result:self.outcome_reset(mode,result)) for result in ('ok','state-timeout','dm-recover','dm-line','fault') for mode in (0,1,2)]
        cases += [('VERIFY-TIMEOUT',lambda:self.outcome_reset(1,'verify-timeout')),('VERIFIED-HALTED',lambda:self.outcome_reset(1,'verified-halted'))]
        cases += [('CONSOLE-'+str(mode),lambda mode=mode:self.console_reset(mode)) for mode in (0,1,2)]
        cases += [('CONSOLE-DM-LINE-'+str(mode),lambda mode=mode:self.console_reset(mode,'dm-line')) for mode in (0,1,2)]
        cases += [('REPLAY-'+result.upper(),lambda result=result:self.replay_reset(result)) for result in ('ok','dm-line')]
        cases += [(kind,lambda kind=kind:self.refusal_reset(kind)) for kind in ('MODE','CRITICAL','TLV','TRUNCATED','SESSION','RESOURCE')]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-RESET-'+name,'debug §4/4.3/4.6; console §2/3; common §1; core §5/9',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical optional ndmreset flags, DM recovery and per-connection console reset',
                'unchecked':['physical ndmreset/wake and DM response timing','reset-mode-1 physical execution proof','failure while restoring DATA/acknowledging havereset','actual response-writer ordering','run','other targets/capacities and production Host integration']}
