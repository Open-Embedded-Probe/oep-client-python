"""Independent attach reset, idle_clock and SWD targetsel checks."""
import struct
from .conformance import require, tlvs
from .conformance_attach import AttachChecks


class WireOptionsChecks(AttachChecks):
    def option_request(self,sid,fn=1,pair=None,idle=None,reset=None,targetsel=None,method=0,marked=True,extra=b'',speed=2000):
        bit=128 if marked else 0;options=b'';T=self.T
        if idle is not None:options+=self.tlv(T['idle_clock']|bit,bytes((idle,)))
        if reset is not None:options+=self.tlv(T['reset']|bit,struct.pack('<HH',*reset))
        if targetsel is not None:options+=self.tlv(T['targetsel']|bit,struct.pack('<I',targetsel))
        return self.attach_request(sid,fn,pair or self.a.pairs[fn][0],speed=speed,method=method,extra=options+extra)
    def listing(self,fn):
        before=self.a.state();rows=[];first=0;selected={}
        for _ in range(4):
            value=self.c.success(self.request(fn,'connections',0,bytes((first,))));at=2
            require(len(value)>=2 and value[0] in (0,1),'connections page')
            for _ in range(value[1]):
                require(len(value)>=at+14,'connections fixed entry');row=struct.unpack_from('<IHHIBB',value,at);at+=14;tid=value[at:at+row[-1]];at+=row[-1];require(len(tid)==row[-1],'connections target_id length');rows.append((row,tid))
            for tag,data in tlvs(value[at:]):
                if tag==self.specs[3]['tlv']['connections_answer']['selected_target']:   # entry_index(u8) targetsel(u32) (debug §2.1)
                    require(len(data)==5 and data[0]<value[1],'selected_target entry');selected[first+data[0]]=struct.unpack_from('<I',data,1)[0]
            if not value[0]:break
            require(value[1]>0,'connections paging progress');first+=value[1]
        expected=[];wanted={}
        for rid,s in before['connections'].items():
            if s['fn']!=fn:continue
            # swd: tid_scheme 2, the DPIDR read at attach; rvswd / swio: the target_id attach's answer carried (it may, debug
            # §1), none: 0, 0 (debug §2.1)
            scheme,tid=(2,struct.pack('<I',0x2ba01477)) if fn==3 else getattr(self,'attach_tids',{}).get(rid,(0,b''))
            if s['targetsel'] is not None:wanted[len(expected)]=s['targetsel']
            expected.append(((rid,*s['pins'],s['speed'],scheme,len(tid)),tid))
        require(rows==expected and selected==wanted and self.a.state()==before,'connections target identity, selector or side effects');return rows
    def attached(self,sid,fn=1,idle=None,reset=None,targetsel=None,method=0,marked=True,pair=None,extra=b''):
        before=self.a.state();req=self.option_request(sid,fn,pair,idle,reset,targetsel,method,marked,extra);value=self.c.success(req)
        require(len(value)>=13,'attach response');rid,identity,flags,speed=struct.unpack_from('<IIBI',value);tags=dict(reversed(tlvs(value[13:])))
        F=self.flags;wanted=(F['halted'] if method else 0)|(F['havereset_acked'] if reset is not None and fn!=3 else 0)|(F['dormant_woken'] if fn==3 and before['wire_behavior']=='dormant' else 0)
        require(flags==wanted and speed<=2000 and identity==(0x2ba01477 if fn==3 else 0x302 if method else 0xc02),'attach identity/flags/speed')
        # dpc is an optional diagnostic (debug §3): only with flags bit3, and then the halted pc when the probe attaches it
        dpc,tid=self.answer['dpc'],self.answer['target_id']
        require(tags.get(dpc) in (None,struct.pack('<I',0x1000)) and (flags&self.flags['halted'] or dpc not in tags),'halted dpc metadata')
        target_id=tags.get(tid)
        if target_id is not None:require(len(target_id)>=1,'target_id scheme');self.attach_tids=getattr(self,'attach_tids',{});self.attach_tids[rid]=(target_id[0],target_id[1:])
        after=self.a.state();s=after['connections'][rid];pins=pair or self.a.pairs[fn][0]
        require(tuple(s['pins'])==pins and s['idle_clock']==(idle or 0) and s['targetsel']==targetsel,'attach option binding')
        expected_levels=[1,(idle or 0)^1] if fn==1 else [1] if fn==2 else [0,1]
        require([after['pin_levels'][pin]['level'] for pin in pins if pin!=65535]==expected_levels,'wire idle levels')
        events=after['wire_events'][len(before['wire_events']):]
        if reset is None:require(not any(row[0].startswith('reset-') for row in events),'implicit reset pin')
        else:
            channel,hold=reset;low=[row for row in events if row[0]=='reset-assert'];release=[row for row in events if row[0]=='reset-release']
            require(len(low)==len(release)==1 and low[0][1:3]==(channel,'open-drain-low') and release[0][1:3]==(channel,'free'),'reset pin drive/release')
            require(release[0][3]-low[0][3]==hold and after['pin_levels'][channel]==dict(mode='free',level=None),'reset hold or transient pin leaked')
            polls=[row for row in events if row[0]=='reset-read'];require(polls and all(row[3]>=release[0][3] and row[4]==1000 for row in polls),'reset DM read before release or not slowest')
            require([row[2] for row in polls]==([2,2,0] if before['wire_behavior']=='dm-recover' else [0]),'reset DM recovery')
        require(after['clock_ms']-before['clock_ms']<=5,'attach overall budget');self.listing(fn);return rid,req
    def normal(self,fn=1,idle=None,reset=None,targetsel=None,method=0,behavior='ok',extra=b'',marked=True):
        self.fresh();self.a.behavior(behavior)
        with self.c.holding(3000) as sid:self.attached(sid,fn,idle,reset,targetsel,method,marked=marked,extra=extra)
    def cleanup(self,fn=1,idle=1,targetsel=None,method=0):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid,_=self.attached(sid,fn,idle=idle,targetsel=targetsel,method=method);before=self.a.state();self.detach(sid,fn,rid);after=self.a.state()
            require(after['targets']==before['targets'],'detach reset/resumed target')
            require(all(after['pin_levels'][pin]==dict(mode='free',level=None) for pin in self.a.pairs[fn][0] if pin!=65535),'detach left idle drive')
            require(not self.listing(fn),'detached target still listed')
    def end_idle(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attached(sid,idle=1,method=1);self.console(sid,parent);before=self.a.state()
        after=self.a.state();require(not after['connections'] and not after['streams'],'end did not release wire/streams')
        require(after['targets']==before['targets'] and all(after['pin_levels'][p]==dict(mode='free',level=None) for p in (0,1)),'end touched halted target or left pin drive')
    def failed_reset(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attached(sid,pair=(2,3));rid,_=self.console(sid,parent);self.a.feed_bytes(rid,b'keep');self.a.behavior('dm-line');before=self.a.state()
            req=self.option_request(sid,reset=(6,1));frame=self.c.exchange(req);require(frame[3:6]==bytes((self.c.reg['resolutions']['completed'],self.c.reg['outcomes']['failed'],self.c.reg['status']['line'])),'attach DM failure must be completed line');tlvs(frame[6:]);after=self.a.state()
            for name in ('connections','streams','next_id'):require(after[name]==before[name],'failed reset attach changed existing resources/allocated ID')
            require(all(after['pin_levels'][pin]==dict(mode='free',level=None) for pin in (0,1,6)),'failed attach did not release pins')
            require(all(after['targets'][k]==v for k,v in before['targets'].items()),'failed attach changed existing target')
            require(all(after['pin_levels'][p]==before['pin_levels'][p] for conn in before['connections'].values() for p in conn['pins'] if p!=65535),'failed attach changed existing wire drive')
            events=after['wire_events'][len(before['wire_events']):];polls=[row for row in events if row[0]=='reset-read'];require(len(polls)==4 and all(row[2]==2 and row[3]<before['clock_ms']+5 and row[4]==1000 for row in polls),'DM failure wait/budget')
            require(after['clock_ms']-before['clock_ms']==5,'DM line fixture exhausted budget')
    def refusal(self,kind):
        self.fresh()
        if kind=='SESSION':self.refused(self.request(1,'attach',0,b'\xfe'),'session_required');return
        with self.c.holding(3000) as sid:
            T=self.T;marked_reset=bytes((T['reset']|128,));idle_clock=T['idle_clock'];bad_idle=max(self.idle_values)+1
            if kind=='NO-RESET':self.a.reset_support(False);self.refused(self.option_request(sid,reset=(6,1)),'unsupported',marked_reset)
            elif kind=='CHANNEL':self.refused(self.option_request(sid,reset=(5,1)),'unsupported',marked_reset)
            elif kind=='HOLD':self.refused(self.option_request(sid,reset=(6,6)),'unsupported',marked_reset)
            elif kind=='SELF-CONFLICT':self.refused(self.option_request(sid,pair=(6,7),reset=(6,1)),'unavailable',cause='pin_in_use')
            elif kind=='BUSY-RESET':self.attached(sid,pair=(6,7));self.refused(self.option_request(sid,reset=(6,1)),'unavailable',cause='pin_in_use')
            elif kind in ('DISABLED','OUTPUT','RESERVED'):
                self.a.pin(6,kind.lower());self.refused(self.option_request(sid,reset=(6,1)),'unavailable',cause='pin_in_use' if kind=='RESERVED' else 'held_by_settings')
            elif kind=='IDLE':
                for marked in (True,False):
                    self.refused(self.option_request(sid,idle=bad_idle,marked=marked),'unsupported',bytes((idle_clock|128 if marked else idle_clock,)))
                    self.refused(self.option_request(sid,idle=1,marked=marked,extra=self.tlv(idle_clock,bytes((bad_idle,)))),'unsupported',bytes((idle_clock,)))   # a later copy is checked too
                for fn in (2,3):                                   # rvswd's tag: unknown on swio / swd (core §2.3)
                    self.refused(self.option_request(sid,fn,idle=1),'unsupported',bytes((idle_clock|128,)))
                    rid,_=self.attached(sid,fn,extra=self.tlv(idle_clock,b'\x01'));self.detach(sid,fn,rid)
            elif kind=='WIDTH':
                for fn,tag,size in [(1,idle_clock|128,2),(1,T['reset']|128,3),(3,T['targetsel']|128,3)]:self.refused(self.option_request(sid,fn,extra=self.tlv(tag,b'\0'*size)),'malformed')
            elif kind=='TARGETSEL-WIRE':self.refused(self.option_request(sid,targetsel=1),'unsupported',bytes((T['targetsel']|128,)))
            elif kind=='MISSING-SPEED':self.refused(self.option_request(sid,reset=(6,1),speed=0),'malformed')
    def target_conflict(self,other):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid,_=self.attached(sid,3,targetsel=0x01000001);self.refused(self.option_request(sid,3,targetsel=other),'unavailable',cause='pin_in_use');self.detach(sid,3,rid)
            new,_=self.attached(sid,3,targetsel=other);require(new!=rid,'detach reused connection ID')
    def replay(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid,req=self.attached(sid,reset=(6,1));result=self.c.exchange(req);self.a.behavior('dm-line');before=self.a.state()
            require(self.c.replayed(req,result,frame=True) and self.a.state()==before and rid in before['connections'],'attach replay reset/reallocated')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit attach-options fixture')
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in self.a.selection.items()};self.operations={fn:{r['name']:r['code'] for r in s['op']} for fn,s in self.specs.items()}
            # every number by name from the selected SPEC (the wire family's one space, core §2.5)
            self.T=self.specs[1]['tlv']['attach']|self.specs[3]['tlv']['attach'];self.answer=self.specs[1]['tlv']['attach_answer']
            self.flags=self.specs[1]['enum']['attach_flags']|self.specs[3]['enum']['attach_flags'];self.idle_values=set(self.specs[1]['enum']['idle_clock'].values())
            require({'idle_clock','reset','targetsel'}<=set(self.T) and {'halted','havereset_acked','dormant_woken'}<=set(self.flags) and 'line' in self.c.reg['status'],'selected SPEC lacks the wire options used')
            self.c.confirm();self.c.identity();self.c.declarations();require(self.a.state()['max_op_ms']==5,'explicit attach budget differs')
            for fn in (1,2,3):require((self.c.reg['describe_common']['role_channels'],bytes((self.specs[fn]['enum']['pin_role']['reset'],0,0,0x40))) in self.c.describe(fn),'reset role channel 6 declaration missing')
        self.c.check('OEP-WIRE-OPTIONS-CONTEXT','debug §1/3/5; explicit logical fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('IDLE-'+str(idle),lambda idle=idle:self.normal(idle=idle)) for idle in (None,0,1)]
        cases += [('RESET-'+str(fn),lambda fn=fn:self.normal(fn,reset=(6,1))) for fn in (1,2,3)]
        cases += [('RESET-HALT-'+str(fn),lambda fn=fn:self.normal(fn,reset=(6,1),method=1)) for fn in (1,2)]
        cases += [('RESET-ZERO',lambda:self.normal(reset=(6,0))),('RESET-RECOVER',lambda:self.normal(reset=(6,1),behavior='dm-recover')),('RESET-FAILED',self.failed_reset)]
        cases += [('TARGETSEL-'+str(value),lambda value=value:self.normal(3,targetsel=value)) for value in (None,0x01000001,0xffffffff)]
        cases += [('DORMANT',lambda:self.normal(3,behavior='dormant')),('FIRST-WINS',lambda:self.normal(idle=1,reset=(6,1),extra=self.tlv(self.T['idle_clock'],b'\x00')+self.tlv(self.T['reset'],struct.pack('<HH',6,2)))),('DETACH-IDLE',self.cleanup),('DETACH-SWD',lambda:self.cleanup(3,None,0x01000001)),('REPLAY',self.replay)]
        cases += [('DETACH-HALTED',lambda:self.cleanup(method=1)),('END-IDLE',self.end_idle),('UNMARKED-RV',lambda:self.normal(idle=1,reset=(6,1),marked=False)),('UNMARKED-SWD',lambda:self.normal(3,targetsel=0x01000001,marked=False))]
        cases += [('CONFLICT-'+str(value),lambda value=value:self.target_conflict(value)) for value in (None,0x01000001,0x01000002)]
        cases += [(kind,lambda kind=kind:self.refusal(kind)) for kind in ('NO-RESET','CHANNEL','HOLD','SELF-CONFLICT','BUSY-RESET','DISABLED','OUTPUT','RESERVED','IDLE','WIDTH','TARGETSEL-WIRE','MISSING-SPEED','SESSION')]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-WIRE-OPTIONS-'+name,'debug §1/2/3/5; core §2.3/8.1',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(r['status']=='passed' for r in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical wire attach reset, idle_clock and SWD targetsel lifecycle',
                'unchecked':['scan idle_clock/targetsel','physical open-drain/levels/hold/wake/DM timing','attach speed/scratch algorithms','line-cut response/release ordering','non-RVSWD idle_clock 0 policy','plan integration and other capacities','actual response-writer and production Host integration']}
