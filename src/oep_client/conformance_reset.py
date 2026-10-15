"""Independent optional reset flags, DM recovery, state and console checks."""
import struct
from .conformance import require, tlvs, ops
from .conformance_step import StepChecks
from .conformance_blocks import HARTSEL
from .conformance_dmseq import target_frame


class ResetChecks(StepChecks):
    def reset_request(self,sid,parent,mode,extra=b''):return self.request(5,'reset',sid,struct.pack('<IB',parent,mode)+extra)
    def reset(self,sid,parent,mode,result):
        self.a.reset_behavior(result);before=self.a.state();request=self.reset_request(sid,parent,mode);frame=self.c.exchange(request)
        good=result in ('ok','dm-recover');reached=good or result=='cleanup-line'
        status=0 if good else {'state-timeout':4,'dm-line':2,'fault':3,'cleanup-line':2}[result]
        flags=int(reached);pc=(0 if mode==0 else 0x2000) if reached else 0
        require(len(frame)>=11 and frame[3:5]==bytes((1,int(not good))),'reset completed outcome')
        require(struct.unpack_from('<BBI',frame,5)==(status,flags,pc),'reset status/flags/PC semantics');tlvs(frame[11:])
        require(not frame[6]&~1,'reset reserved flags')
        after=self.a.state();access=after['access'][len(before['access']):]
        require(after['next_id']==before['next_id'] and after['touch']==before['touch'],'reset touched pins or allocated')
        mine=[rid for rid,s in before['streams'].items() if s['connection']==parent]
        if result=='cleanup-line':                             # the cleanup failed: closed after the answer (debug §4)
            require(parent not in after['connections'] and not any(rid in after['streams'] for rid in mine),'connection kept after a failed cleanup')
            return request,frame
        if result=='dm-line' and parent not in after['connections']:
            # a DM that never answered: "必要な後始末が済まなければconnectionを閉じる" (debug §4.3) - a probe that cannot
            # count haltreq / havereset as done closes the connection and its streams after the answer
            require(not any(rid in after['streams'] for rid in mine),'streams kept after their connection closed')
            return request,frame
        require(after['connections']==before['connections'],'reset destroyed connection')
        target=after['targets'][parent]
        require(not target['registers'][0x10]&(HARTSEL|2),'reset left hartsel or ndmreset')
        require(not target['haltreq'] and not target['registers'][0x10]&0x80000000,'reset left haltreq')
        if good:require(target['halted']==(mode==2),'reset did not reach fixture state')
        asserted=[i for i,row in enumerate(access) if row[0]=='ndmreset-assert'];released=[i for i,row in enumerate(access) if row[0]=='ndmreset-release']
        require(len(asserted)==len(released)==1 and asserted[0]<released[0],'reset ndmreset sequence')
        require(access[released[0]][2]==(mode==2),'mode 2 did not hold haltreq during ndmreset release')
        polls=[(i,row) for i,row in enumerate(access) if row[0]=='reset-dm-read']
        expected=[2,2,0] if result=='dm-recover' else [2]*5 if result=='dm-line' else [0]
        require([row[2] for _,row in polls]==expected and all(i>released[0] for i,_ in polls),'DM not retried after ndmreset release')
        require(all(0<=row[3]<before['max_op_ms']*1000 for _,row in polls),'DM read beyond overall budget')
        acks=[row for row in access if row[0]=='ack-havereset']
        require(len(acks)==int(result!='dm-line'),'havereset acknowledgement missing/without observed DM')
        return request,frame
    def outcome_reset(self,mode,result):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.seed_step(parent,0x1000,0x40008001);self.reset(sid,parent,mode,result)
    def console_reset(self,mode,result='dm-recover'):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid,halt=False);rid,_=self.console(sid,parent,2)
            self.c.success(self.request(4,'write',sid,struct.pack('<IH',rid,3)+b'ABC'));self.a.frame(rid,*target_frame(syn=True,data=b'before'));self.a.poll(rid)
            other,_=self.attach(sid,pair=(2,3));other_rid,_=self.console(sid,other,2);self.a.frame(other_rid,*target_frame(syn=True,data=b'peer'));self.a.poll(other_rid)
            self.c.success(self.request(5,'halt',sid,struct.pack('<I',parent)));before=self.a.state();self.reset(sid,parent,mode,result);after=self.a.state()
            require(before['protocols'][rid]['synced'] and before['protocols'][rid]['pending']==b'AB','explicit pending/reset premise')
            if parent not in after['connections']:                 # closed: a silent DM's cleanup not done (debug §4.3)
                for field in ('targets','streams','mailboxes','protocols'):
                    key=other if field=='targets' else other_rid;require(after[field][key]==before[field][key],'reset changed other connection')
                return
            if result!='dm-line':require(not after['protocols'][rid]['synced'] and after['protocols'][rid]['pending']==b'','reset retained dmseq sync/pending')
            for field in ('data','position','tx'):require(after['streams'][rid][field]==before['streams'][rid][field],'reset lost live buffer/queue')
            marks=after['streams'][rid]['marks'];new=marks[len(before['streams'][rid]['marks']):]
            # a successful reset op: one reset mark (0x40, ndmreset), no restart for the same reset; a failed one: none
            require([(row[2],row[-1]) for row in new]==([] if result=='dm-line' else [(0x40,1)]) and all(row[1]==before['streams'][rid]['position'] for row in new),'reset mark kind/detail/position')
            for mark in new:
                page=self.c.success(self.request(4,'marks',0,struct.pack('<II',rid,mark[0])));count=page[1]
                require(count and struct.unpack_from('<IQBQB',page,2)==mark,'reset mark wire readback');tlvs(page[2+22*count:])
            for field in ('targets','streams','mailboxes','protocols'):
                key=other if field=='targets' else other_rid;require(after[field][key]==before[field][key],'reset changed other connection')
            polls=after['arbitration'][len(before['arbitration']):];require(polls and all(not row['events'] for row in polls),'console accessed reset scratch')
    def replay_reset(self,result):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.console(sid,parent,2);request,frame=self.reset(sid,parent,2,result);self.a.reset_behavior('fault');before=self.a.state()
            require(self.c.replayed(request,frame,frame=True) and self.a.state()==before,'reset replay executed ndmreset or recomputed answer')
    def refusal_reset(self,kind):
        self.fresh()
        if kind=='SESSION':self.refused(self.reset_request(0,65535,255,b'\xfe'),'session_required');return
        with self.c.holding(3000) as sid:
            parent=self.parent(sid)
            if kind=='MODE':
                for mode in (1,3):self.refused(self.reset_request(sid,parent,mode),'unsupported',b'\0')   # modes 0 and 2 only (debug §4.3)
            elif kind=='CRITICAL':self.refused(self.reset_request(sid,parent,0,self.tlv(254,b'x')),'unsupported',b'\xfe')
            elif kind=='TLV':self.refused(self.reset_request(sid,parent,0,b'\x7f\x05\0x'),'malformed')
            elif kind=='TRUNCATED':self.refused(self.request(5,'reset',sid,struct.pack('<I',parent)),'malformed')
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
            require(self.operations[5]['reset']==19 and self.specs[5]['enum']['reset_mode']==dict(run=0,halt_at_reset=2) and self.specs[5]['enum']['reset_flags']==dict(reached=1),'selected reset grammar mismatch')
            require(self.c.reg['status']==dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5),'selected standard status meanings differ')
            self.c.confirm();self.c.identity();self.c.declarations();require(19 in ops(dict(reversed(self.c.describe(5)))[7]),'optional reset not offered by selected fixture')
            require(self.a.state()['max_op_ms']==1,'explicit reset budget differs')
        self.c.check('OEP-RESET-CONTEXT','debug §4/4.3/4.6; common §1 marks',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[(result.upper()+'-'+str(mode),lambda mode=mode,result=result:self.outcome_reset(mode,result)) for result in ('ok','state-timeout','dm-recover','dm-line','fault','cleanup-line') for mode in (0,2)]
        cases += [('CONSOLE-'+str(mode),lambda mode=mode:self.console_reset(mode)) for mode in (0,2)]
        cases += [('CONSOLE-DM-LINE-'+str(mode),lambda mode=mode:self.console_reset(mode,'dm-line')) for mode in (0,2)]
        cases += [('REPLAY-'+result.upper(),lambda result=result:self.replay_reset(result)) for result in ('ok','dm-line')]
        cases += [(kind,lambda kind=kind:self.refusal_reset(kind)) for kind in ('MODE','CRITICAL','TLV','TRUNCATED','SESSION','RESOURCE')]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-RESET-'+name,'debug §4/4.3/4.6; console §2/3; common §1; core §5/9',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical optional ndmreset flags, DM recovery and per-connection console reset',
                'unchecked':['physical ndmreset/wake and DM response timing','failure while acknowledging havereset','console reset/restart mark kinds of the current console','actual response-writer ordering','run','other targets/capacities and production Host integration']}
