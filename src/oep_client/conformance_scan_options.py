"""Independent scan idle levels and explicit single TARGETSEL selection."""
import struct
from .conformance import require, tlvs, ops
from .conformance_wire_options import WireOptionsChecks


class ScanOptionsChecks(WireOptionsChecks):
    def scan_request(self,sid,fn,pairs,idle=None,selector=None,speed=None,marked=True,extra=b''):
        bit=128 if marked else 0;payload=bytes((len(pairs),))+b''.join(struct.pack('<HH',*pair) for pair in pairs)
        S=self.S
        if idle is not None:payload+=self.tlv(S['idle_clock']|bit,bytes((idle,)))
        if selector is not None:payload+=self.tlv(S['targetsel']|bit,struct.pack('<I',selector))
        if speed is not None:payload+=self.tlv(S['max_speed']|bit,struct.pack('<I',speed))
        return self.request(fn,'scan',sid,payload+extra)
    def scan(self,sid,fn,pairs,expected,*,tried=None,idle=None,selector=None,speed=None,marked=True,extra=b''):
        before=self.a.state();req=self.scan_request(sid,fn,pairs,idle,selector,speed,marked,extra);value=self.c.success(req)
        tried=min(len(pairs),before['scan_limit']) if tried is None else tried
        require(len(value)>=2 and value[:2]==bytes((tried,len(expected))) and 1<=tried<=len(pairs),'scan tried/count')
        end=2+9*len(expected);require(len(value)>=end,'scan entries truncated');tlvs(value[end:])
        rows=[struct.unpack_from('<BHHI',value,2+9*i) for i in range(len(expected))]
        require(rows==expected,'scan kind/identity/pair/order')
        after=self.a.state()
        for field in ('connections','streams','next_id','targets','pin_modes','pin_levels','events'):
            require(after[field]==before[field],'scan changed resources/target/idle configuration')
        require(not after['scan_live'],'scan retained candidate pins')
        events=after['scan_events'][len(before['scan_events']):];reserved=[r for r in events if r[0]=='reserve'];released=[r for r in events if r[0]=='release'];drives=[r for r in events if r[0]=='drive'];reads=[r for r in events if r[0]=='read']
        require(reserved==[('reserve',fn,pair,selector) for pair in pairs[:tried]] and released==[('release',fn,pair) for pair in pairs[:tried]],'scan implicit/skipped/reused candidates or leaked pins')
        levels=[1,1-(idle or 0)] if fn==1 else [1] if fn==2 else [0,1]
        require([r[0] for r in events]==(['reserve','drive']+(['select'] if selector is not None else [])+['read','release'])*tried,'scan candidate phase ordering')
        require(all(r[6]==levels for r in reads),'scan actual idle levels during read')
        require(drives==[('drive',fn,pair,levels) for pair in pairs[:tried]],'scan idle levels')
        require([(r[1],r[2],r[3]) for r in reads]==[(fn,pair,selector) for pair in pairs[:tried]],'scan ignored selector or invented reads')
        require(all(0<r[4]<=speed for r in reads) if speed is not None else all(r[4]==1000 for r in reads),'scan max_speed/default slowest')
        selects=[r for r in events if r[0]=='select'];require(selects==[('select',fn,selector)]*tried if selector is not None else not selects,'scan implicit TARGETSEL or multiple selector choices')
        require(after['clock_ms']-before['clock_ms']==tried and tried<=before['max_op_ms'],'explicit scan clock/budget')
        return req,value
    def found(self,fn,pair,value):return (self.kinds['arm_adi' if fn==3 else 'riscv_dm'],*pair,value)
    def idle_case(self,idle):
        self.fresh()
        with self.c.holding(3000) as sid:
            pair=self.a.pairs[1][0];self.a.identity(1,pair,None,0x202);self.scan(sid,1,[pair],[self.found(1,pair,0x202)],idle=idle)
    def selector_case(self,selector):
        self.fresh()
        with self.c.holding(3000) as sid:
            pair=self.a.pairs[3][0];self.a.identity(3,pair,0x01000001,0x2ba01477)
            self.scan(sid,3,[pair],[self.found(3,pair,0x2ba01477)] if selector==0x01000001 else [],selector=selector)
    def zero_selector(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            pair=self.a.pairs[3][0];self.a.identity(3,pair,0,0);self.scan(sid,3,[pair],[self.found(3,pair,0)],selector=0)
    def first_wins(self,swd=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            fn=3 if swd else 1;pair=self.a.pairs[fn][0];selector=0x01000001 if swd else None;self.a.identity(fn,pair,selector,0x2ba01477 if swd else 0x202)
            self.scan(sid,fn,[pair],[self.found(fn,pair,0x2ba01477 if swd else 0x202)],selector=selector,idle=None if swd else 1,speed=1500,extra=self.tlv(self.S['targetsel'],struct.pack('<I',0x01000002)) if swd else self.tlv(self.S['idle_clock'],b'\x00')+self.tlv(self.S['max_speed'],struct.pack('<I',2000)))   # valid later copies (core §2.3: checked too)
    def unmarked(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            for fn,selector in ((1,None),(3,0x01000001)):
                pair=self.a.pairs[fn][0];value=0x202 if fn==1 else 0x2ba01477;self.a.identity(fn,pair,selector,value);self.scan(sid,fn,[pair],[self.found(fn,pair,value)],selector=selector,idle=1 if fn==1 else None,marked=False)
    def prefix(self):
        self.fresh();self.a.limit(1)
        with self.c.holding(3000) as sid:
            pairs=self.a.pairs[1]
            for i,pair in enumerate(pairs):self.a.identity(1,pair,None,0x202+i)
            for i in range(3):self.scan(sid,1,pairs[i:],[self.found(1,pairs[i],0x202+i)],idle=1,tried=1)
    def versions(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            for fn in (1,2):
                pair=self.a.pairs[fn][0]
                for v in (0,1,2,3,14,15):
                    value=0x200|v;self.a.identity(fn,pair,None,value);self.scan(sid,fn,[pair],[self.found(fn,pair,value)] if 2<=v<15 else [],idle=1 if fn==1 else None)
    def retained(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attached(sid,idle=1,method=1);rid,_=self.console(sid,parent);self.a.feed_bytes(rid,b'keep');pair=self.a.pairs[3][0];self.a.identity(3,pair,0x01000001,0x2ba01477)
            self.scan(sid,3,[pair],[self.found(3,pair,0x2ba01477)],selector=0x01000001)
    def replay_scan(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            pair=self.a.pairs[3][0];self.a.identity(3,pair,0x01000001,0x2ba01477);req,value=self.scan(sid,3,[pair],[self.found(3,pair,0x2ba01477)],selector=0x01000001)
            self.a.identity(3,pair,0x01000001,None);before=self.a.state();require(self.c.replayed(req,value) and self.a.state()==before,'scan replay probed/selected target again')
    def refused_scan(self,kind):
        self.fresh();pairs=list(self.a.pairs[1])
        if kind=='SESSION':self.refused(self.scan_request(0,1,pairs,extra=b'\xfe'),'session_required');return
        with self.c.holding(3000) as sid:
            if kind=='ZERO':self.refused(self.scan_request(sid,1,[]),'malformed')
            elif kind=='DUP':self.refused(self.scan_request(sid,1,[pairs[0]]*2),'malformed')
            elif kind=='BAD-LATE':self.refused(self.scan_request(sid,1,[pairs[0],(7,7)],idle=1),'unsupported',b'\0')
            elif kind=='BUSY-LATE':self.attached(sid,idle=1);self.refused(self.scan_request(sid,1,[pairs[1],pairs[0]],idle=1),'unavailable',cause='pin_in_use')
            elif kind in ('DISABLED-LATE','OUTPUT-LATE','RESERVED-LATE'):
                self.a.pin(pairs[1][0],kind.split('-')[0].lower());self.refused(self.scan_request(sid,1,pairs[:2],idle=1),'unavailable',cause='pin_in_use' if kind=='RESERVED-LATE' else 'held_by_settings')
            elif kind=='IDLE':
                S=self.S;bad_idle=max(self.idle_values)+1
                self.refused(self.scan_request(sid,1,pairs,idle=bad_idle),'unsupported',bytes((S['idle_clock']|128,)))
                self.refused(self.scan_request(sid,1,pairs,idle=1,extra=self.tlv(S['idle_clock'],bytes((bad_idle,)))),'unsupported',bytes((S['idle_clock'],)))   # a later copy too
                self.refused(self.scan_request(sid,1,pairs,idle=1,speed=1500,extra=self.tlv(S['max_speed'],struct.pack('<I',999))),'unsupported',bytes((S['max_speed'],)))
                for fn in (2,3):self.refused(self.scan_request(sid,fn,self.a.pairs[fn],idle=1),'unsupported',bytes((S['idle_clock']|128,)))
            elif kind=='WIDTH':
                for fn,tag,size in ((1,self.S['idle_clock']|128,2),(3,self.S['targetsel']|128,3)):self.refused(self.scan_request(sid,fn,self.a.pairs[fn],extra=self.tlv(tag,b'\0'*size)),'malformed')
            elif kind=='TARGETSEL-WIRE':self.refused(self.scan_request(sid,1,pairs,selector=1),'unsupported',bytes((self.S['targetsel']|128,)))
            elif kind=='TLV':self.refused(self.scan_request(sid,1,pairs,extra=bytes((self.unknown,5,0))+b'x'),'malformed')   # a TLV past the payload
            elif kind=='FOREIGN':self.refused(self.scan_request(sid%0xffffffff+1,1,pairs,extra=b'\xfe'),'locked')
            elif kind=='LOW':self.refused(self.scan_request(sid,1,pairs,speed=999),'unsupported',bytes((self.S['max_speed']|128,)))
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit scan-options fixture')
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in self.a.selection.items()};self.operations={fn:{r['name']:r['code'] for r in s['op']} for fn,s in self.specs.items()}
            # every number by name from the selected SPEC (the wire family's one space, core §2.5)
            self.S=self.specs[1]['tlv']['scan']|self.specs[3]['tlv']['scan'];self.kinds=self.specs[1]['enum']['scan_kind']|self.specs[3]['enum']['scan_kind']
            self.idle_values=set(self.specs[1]['enum']['idle_clock'].values());self.unknown=max(self.S.values())+1
            self.T=self.specs[1]['tlv']['attach']|self.specs[3]['tlv']['attach'];self.answer=self.specs[1]['tlv']['attach_answer'];self.flags=self.specs[1]['enum']['attach_flags']|self.specs[3]['enum']['attach_flags']   # attach (the wire options' checks)
            require({'idle_clock','targetsel','max_speed'}<=set(self.S) and {'arm_adi','riscv_dm'}<=set(self.kinds),'selected SPEC lacks the scan options used')
            self.c.confirm();self.c.identity();self.c.declarations();require(self.a.state()['max_op_ms']==5,'explicit scan budget differs')
            for fn in (1,2,3):require(self.operations[fn]['scan'] in ops(dict(reversed(self.c.describe(fn)))[self.c.reg['describe_common']['ops']]),'selected optional scan not offered')
        self.c.check('OEP-SCAN-OPTIONS-CONTEXT','debug §1/3/5; explicit finite logical fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('IDLE-'+str(v),lambda v=v:self.idle_case(v)) for v in (None,0,1)]+[('SELECTOR-'+str(v),lambda v=v:self.selector_case(v)) for v in (None,0x01000001,0x01000002)]
        cases += [('ZERO-SELECTOR',self.zero_selector),('FIRST-IDLE',self.first_wins),('FIRST-SELECTOR',lambda:self.first_wins(True)),('UNMARKED',self.unmarked),('PREFIX',self.prefix),('VERSIONS',self.versions),('RETAINED',self.retained),('REPLAY',self.replay_scan)]
        cases += [('REFUSE-'+kind,lambda kind=kind:self.refused_scan(kind)) for kind in ('ZERO','DUP','BAD-LATE','BUSY-LATE','DISABLED-LATE','OUTPUT-LATE','RESERVED-LATE','IDLE','WIDTH','TARGETSEL-WIRE','TLV','SESSION','FOREIGN','LOW')]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-SCAN-OPTIONS-'+name,'debug §1/3/5; core §2.3/5/8.1',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(r['status']=='passed' for r in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical finite scan idle_clock and explicit single TARGETSEL selection',
                'unchecked':['physical wake/dmactive/idle cycles and timing','line-cut response/release ordering','other budgets/frame routes and writer concurrency','plan integration and production Host integration']}
