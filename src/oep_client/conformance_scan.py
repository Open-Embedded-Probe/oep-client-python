"""Independent finite standard wire scan admission and prefix checks."""
import struct
from .conformance import require, ops, tlvs
from .conformance_attach import AttachChecks


class ScanChecks(AttachChecks):
    def scan_request(self,sid,fn,pairs,extra=b''):
        require(len(pairs)<=255,'scan count u8')
        return self.request(fn,'scan',sid,bytes((len(pairs),))+b''.join(struct.pack('<HH',*pair) for pair in pairs)+extra)
    def scan(self,sid,fn,pairs,expected_tried,expected):
        req=self.scan_request(sid,fn,pairs);before=self.a.state();value=self.c.success(req)
        require(len(value)>=2,'scan response header');tried,count=value[:2]
        require(tried==expected_tried and 1<=tried<=len(pairs) and count<=tried and len(value)>=2+9*count,'scan tried/count/size')
        rows=[struct.unpack_from('<BHHI',value,2+9*i) for i in range(count)];tlvs(value[2+9*count:])
        require(rows==expected,'scan entry kind/order/pins/identity differs')
        require(all((row[1],row[2]) in pairs[:tried] for row in rows),'scan reported an untried candidate')
        after=self.a.state()
        for key in ('connections','streams','next_id','events','pin_modes'):
            require(after[key]==before[key],'scan allocated or changed existing resource/configuration')
        require(not after['scan_live'],'scan left candidate pins held')
        delta=after['scan_pin_events'][len(before['scan_pin_events']):]
        require(delta==[(action,fn,pair) for pair in pairs[:tried] for action in ('reserve','idle')],'scan skipped, invented or retained candidate pins')
        return req,value
    def found(self,fn,pair,identity):return (self.specs[fn]['enum']['scan_kind']['arm_adi' if fn==3 else 'riscv_dm'],*pair,identity)
    def single(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            for fn in (1,2,3):
                pair=self.a.pairs[fn][0];self.a.scan_id(fn,pair,0 if fn==3 else 0x202)
                self.scan(sid,fn,[pair],1,[self.found(fn,pair,0 if fn==3 else 0x202)])
    def prefix(self,empty=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            pairs=list(self.a.pairs[1]);self.a.scan_budget(1)
            for i,pair in enumerate(pairs):self.a.scan_id(1,pair,None if empty else 0x202+i)
            for i in range(3):
                self.scan(sid,1,pairs[i:],1,[] if empty else [self.found(1,pairs[i],0x202+i)])
    def versions(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            for fn in (1,2):
                pair=self.a.pairs[fn][0]
                for version in (0,1,2,3,14,15):
                    identity=0x200|version;self.a.scan_id(fn,pair,identity)
                    self.scan(sid,fn,[pair],1,[self.found(fn,pair,identity)] if 2<=version<15 else [])
    def refusal(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            pairs=list(self.a.pairs[1]);fn=1;extra=b'';reason='malformed';payload=None;cause=None
            if kind=='ZERO':pairs=[]
            elif kind=='DUPLICATE':pairs=[pairs[0],pairs[0]]
            elif kind=='TRUNCATED':
                self.refused(self.request(1,'scan',sid,b'\x02'+struct.pack('<HH',*pairs[0])),'malformed');return
            elif kind=='UNDECLARED-LATE':pairs=[pairs[0],(7,7)];reason='unsupported';payload=b'\0'
            elif kind=='SWIO-CLOCK':fn=2;pairs=[(0,1)];reason='unsupported';payload=b'\0'
            elif kind=='BUSY-LATE':
                self.attach(sid);pairs=[pairs[1],pairs[0]];reason='unavailable';cause=1
            elif kind in ('DISABLED-LATE','OUTPUT-LATE'):
                self.a.pin_mode(pairs[1][0],'disabled' if kind=='DISABLED-LATE' else 'output');pairs=pairs[:2];reason='unavailable';cause=5
            elif kind=='LOW-SPEED':
                for raw in (1,129):self.refused(self.scan_request(sid,1,pairs,self.tlv(raw,struct.pack('<I',999))),'unsupported',bytes((raw,)))
                return
            elif kind=='ZERO-SPEED':                       # max_speed 0: malformed, marked or not (debug §1)
                for raw in (1,129):self.refused(self.scan_request(sid,1,pairs,self.tlv(raw,bytes(4))),'malformed')
                return
            elif kind=='LATER-WIDTH':extra=self.tlv(1,struct.pack('<I',1500))+self.tlv(1,b'x')   # a later copy is checked too (core §2.3)
            elif kind=='LATER-LOW':                        # a later copy below min_clock_hz: unsupported, its tag as sent
                self.refused(self.scan_request(sid,1,pairs,self.tlv(129,struct.pack('<I',1500))+self.tlv(1,struct.pack('<I',999))),'unsupported',b'\x01');return
            elif kind=='WIDTH':extra=self.tlv(129,b'x')
            elif kind=='TLV-STRUCTURE':extra=b'\x7f\x05\0x'
            elif kind=='CRITICAL':extra=self.tlv(254,b'x');reason='unsupported';payload=b'\xfe'
            self.refused(self.scan_request(sid,fn,pairs,extra),reason,payload,cause)
    def extras(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            pair=self.a.pairs[1][0];self.a.scan_id(1,pair,0x202)
            # the first max_speed is used; a later copy is checked too, so it is a valid one here (core §2.3)
            req=self.scan_request(sid,1,[pair],self.tlv(126,b'x')+self.tlv(129,struct.pack('<I',1200))+self.tlv(1,struct.pack('<I',1500)))
            before=self.a.state();value=self.c.success(req);require(value[:2]==b'\x01\x01','known speed/unknown optional/first TLV not accepted')
            require(struct.unpack_from('<BHHI',value,2)==self.found(1,pair,0x202),'scan with optional tags');tlvs(value[11:])
            after=self.a.state();require(after['touch'][-1][-1]<=1200 and after['next_id']==before['next_id'] and not after['scan_live'],'scan speed or resource state')
    def replay(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            pair=self.a.pairs[1][0];self.a.scan_id(1,pair,0x202);req,original=self.scan(sid,1,[pair],1,[self.found(1,pair,0x202)])
            self.a.scan_id(1,pair,None);before=self.a.state()
            require(self.c.replayed(req,original) and self.a.state()==before,'scan replay re-explored or recomputed')
    def retained(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);stream,_=self.console(sid,parent);self.a.feed_bytes(stream,b'keep')
            pair=self.a.pairs[3][0];self.a.scan_id(3,pair,0x2ba01477)
            self.scan(sid,3,[pair],1,[self.found(3,pair,0x2ba01477)])
    def lock_scan(self):
        self.fresh();self.refused(self.scan_request(0,1,[self.a.pairs[1][0]],b'\xfe'),'session_required')
        with self.c.holding(3000) as sid:self.refused(self.scan_request(sid%0xffffffff+1,1,[self.a.pairs[1][0]],b'\xfe'),'locked')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical scan contract')
            self.specs={fn:next(row for row in self.c.reg['interface'] if row['name']==name and row['revision']==1) for fn,name in self.a.selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            require(all(self.operations[fn]['scan']==16 and self.specs[fn]['tlv']['scan']['max_speed']==1 and
                        self.specs[fn]['enum']['scan_kind']['arm_adi' if fn==3 else 'riscv_dm']==(2 if fn==3 else 1) for fn in (1,2,3)), 'selected SPEC differs from scan fixture')
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot()
            for fn in (1,2,3):
                require(any(row['fn']==fn and row['name']==self.a.selection[fn] for row in entries),'selected wire absent')
                values=dict(reversed(self.c.describe(fn)));require(16 in ops(values[self.c.reg['describe_common']['ops']]),'optional scan not offered by selected fixture')
            self.c.observed['scan_fixture']={'reference':self.a.reference,'selection':self.a.selection,'pairs':self.a.pairs,'budget':2,'scope':'logical-model'}
        self.c.check('OEP-SCAN-CONTEXT','debug §0/1/3/5; explicit finite fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('SINGLE',self.single),('PREFIX',self.prefix),('EMPTY-HITS',lambda:self.prefix(True)),('VERSIONS',self.versions)]
        cases += [(kind,lambda kind=kind:self.refusal(kind)) for kind in ('ZERO','DUPLICATE','TRUNCATED','UNDECLARED-LATE','SWIO-CLOCK','BUSY-LATE','DISABLED-LATE','OUTPUT-LATE','LOW-SPEED','ZERO-SPEED','WIDTH','LATER-WIDTH','LATER-LOW','TLV-STRUCTURE','CRITICAL')]
        cases += [('EXTRAS',self.extras),('REPLAY',self.replay),('RETAINED',self.retained),('LOCKS',self.lock_scan)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-SCAN-'+name,'debug §1/3/5; core §2.3/5.2/8.1',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','full_conformance':False,
                'scope':'finite explicit standard wire scan candidates and logical preflight/prefix/replay','checks':rows,'observed':self.c.observed,'adapter_trace':self.a.trace,
                'levels':{'core':'session/replay prerequisites','interface':'common contracts checked separately','oep-interface':'selected finite wire scan'},
                'unchecked':['physical wake/dmactive/scratch/pin idle','targetsel/idle_clock','electrical scan timing/max_op_ms','other budgets/frame limits/concurrent writers','production Host integration']}
