"""SPEC-driven standard attach admission and console open checks."""
import struct
from .conformance import require, ops, tlvs
from .conformance_registered_ops import refusal_carries


class AttachChecks:
    def __init__(self, checks, adapter):self.c,self.a=checks,adapter
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'fresh logical boot')
        self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'boot readback');self.tids={}
    def request(self, fn, name, sid, payload):return self.c.request(self.operations[fn][name],payload,session=sid,fn=fn)
    def attach_request(self, sid, fn=1, pair=None, speed=2000, method=0, extra=b'', cut=0):
        """method swdio swclk max_speed (debug §1), `cut` bytes short, then `extra`."""
        payload=struct.pack('<BHHI',method,*(pair or self.a.pairs[fn][0]),speed)
        return self.request(fn,'attach',sid,payload[:len(payload)-cut]+extra)
    def reset_tlv(self, fn=1, channel=None, hold=10, marked=True):
        tag=self.specs[fn]['tlv']['attach']['reset']|(128 if marked else 0)
        return self.tlv(tag,struct.pack('<HH',self.a.reset_channel if channel is None else channel,hold))
    @staticmethod
    def tlv(tag, value):return bytes((tag,))+struct.pack('<H',len(value))+value
    def attach(self, sid, fn=1, pair=None, **kwargs):
        pair=pair or self.a.pairs[fn][0];req=self.attach_request(sid,fn,pair,**kwargs);value=self.c.success(req)
        require(len(value)>=13,'attach fixed response');rid,identity,flags,speed=struct.unpack_from('<IIBI',value);tail=dict(reversed(tlvs(value[13:])))
        # connections' tid (debug §2.1): swd scheme 2, length 4, the DPIDR (attach's id); else attach's target_id, none: 0, 0
        swd=self.specs[fn]['name']=='oep.wire.swd';tid=tail.get(self.specs[fn]['tlv']['attach_answer']['target_id'])
        self.tids=getattr(self,'tids',{});self.tids[rid]=(2,struct.pack('<I',identity)) if swd else (tid[0],tid[1:]) if tid else (0,b'')
        require(rid and flags==0 and 0<speed<=kwargs.get('speed',2000),'attach ID/flags/speed')
        state=self.a.state();require(state['connections'][rid]['fn']==fn and tuple(state['connections'][rid]['pins'])==pair and state['connections'][rid]['speed']==speed,'attach resource differs from request')
        before=self.a.state();rows=[];first=0
        for _ in range(3):
            page=self.c.success(self.request(fn,'connections',0,bytes((first,))))
            require(len(page)>=2 and page[0] in (0,1),'connections header');at=2
            for _ in range(page[1]):
                require(len(page)>=at+14,'connections entry');entry=struct.unpack_from('<IHHIBB',page,at);at+=14
                require(len(page)>=at+entry[-1],'target ID size');tid=page[at:at+entry[-1]];at+=entry[-1];rows.append((entry,tid))
            tlvs(page[at:])
            if not page[0]:break
            require(page[1]>0,'connections paging progress');first+=page[1]
        expected=[((id,*s['pins'],s['speed'],self.tids[id][0],len(self.tids[id][1])),self.tids[id][1]) for id,s in state['connections'].items() if s['fn']==fn]
        require(rows==expected and self.a.state()==before,'connections enumeration/routing mutated state or lost metadata')
        return rid,req
    def refused(self, req, reason, payload=None, cause=None):
        before=self.a.state();value=self.c.rejected(req,reason)
        if payload is not None:require(refusal_carries(reason,value,payload),'refusal payload/raw tag')   # a TLV tail may follow (core §4.3)
        if cause is not None:require(dict(reversed(tlvs(value))).get(self.c.reg['core']['tlv']['unavailable_payload']['cause'])==bytes((self.cause(cause) if isinstance(cause,str) else cause,)),'unavailable cause')
        require(self.a.state()==before,'refused request changed resource/allocator or touched target')
    def detach(self, sid, fn, rid):tlvs(self.c.success(self.request(fn,'detach',sid,struct.pack('<I',rid))))
    def mechanism(self, mechanism):
        """A console mechanism's number: a name looked up in the probe's describe list (core §7.7), or a number as given."""
        if isinstance(mechanism,int):return mechanism
        if not hasattr(self,'mech'):
            tag=next(s for s in self.c.reg['interface'] if s['name']=='oep.target.console')['tlv']['describe']['mechanism']
            self.mech={v.decode():i+1 for i,v in enumerate(v for t,v in self.c.describe(self.a.console_fn) if t==tag)}
        return self.mech[mechanism]
    def cause(self, name):return self.c.reg['core']['enum']['unavailable_cause'][name]
    def console_request(self, sid, parent, mechanism='dmdata', extra=b''):
        return self.request(self.a.console_fn,'open',sid,struct.pack('<IB',parent,self.mechanism(mechanism))+extra)
    def console(self, sid, parent, mechanism='dmdata'):
        mechanism=self.mechanism(mechanism);req=self.console_request(sid,parent,mechanism);value=self.c.success(req)
        require(len(value)>=4,'console open fixed response');rid=struct.unpack_from('<I',value)[0];tlvs(value[4:])
        require(rid,'console stream ID');state=self.a.state()
        require(state['streams'][rid]['connection']==parent and state['streams'][rid]['mechanism']==mechanism,'console binding differs')
        return rid,req
    def close(self,sid,rid):tlvs(self.c.success(self.request(self.a.console_fn,'close',sid,struct.pack('<I',rid))))
    def wire_case(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            if kind=='SHORT':
                for cut in (1,4,8):self.refused(self.attach_request(sid,cut=cut),'malformed')
            elif kind=='ZERO-SPEED':
                self.refused(self.attach_request(sid,speed=0),'malformed')
            elif kind=='WIDTH':
                for marked in (True,False):
                    tag=self.specs[1]['tlv']['attach']['reset']|(128 if marked else 0)
                    self.refused(self.attach_request(sid,extra=self.tlv(tag,b'x')),'malformed')
            elif kind=='LOW-SPEED':
                self.refused(self.attach_request(sid,speed=999),'unsupported',b'\0')
            elif kind=='PINS':
                self.refused(self.attach_request(sid,pair=(0,0)),'unsupported',b'\0')
                self.refused(self.attach_request(sid,fn=2,pair=(0,1)),'unsupported',b'\0')
            elif kind=='METHOD':
                for fn,method in [(1,2),(2,2),(3,1)]:self.refused(self.attach_request(sid,fn,self.a.pairs[fn][0],method=method),'unsupported',b'\0')
                for fn in (1,2):                               # method 1 only with features bit0 attach_halt (debug §3)
                    features=dict(reversed(self.c.describe(fn))).get(self.c.reg['describe_common']['features'],b'\0'*4)
                    if not int.from_bytes(features[:4],'little')&self.specs[fn]['enum']['features']['attach_halt']:
                        self.refused(self.attach_request(sid,fn,self.a.pairs[fn][0],method=self.specs[fn]['enum']['attach_method']['halt']),'unsupported',b'\0')
            elif kind=='TLV':
                self.refused(self.attach_request(sid,extra=self.tlv(self.unknown|128,b'x')),'unsupported',bytes((self.unknown|128,)))
                for marked in (True,False):                    # not a reset line: the tag as received (core §2.3)
                    self.refused(self.attach_request(sid,extra=self.reset_tlv(channel=0,marked=marked)),'unsupported',
                                 bytes((self.specs[1]['tlv']['attach']['reset']|(128 if marked else 0),)))
                self.attach(sid,extra=self.tlv(self.unknown,b'x'))
            elif kind=='MALFORMED':
                reset=self.specs[1]['tlv']['attach']['reset']|128
                for tail in (b'\0\0\0',bytes((reset,)),bytes((self.unknown|128,4,0))+b'x',self.reset_tlv()+self.tlv(reset,b'x')):
                    self.refused(self.attach_request(sid,extra=tail),'malformed')
            elif kind=='UNMARKED':self.attach(sid,extra=self.reset_tlv(marked=False)+self.tlv(self.unknown,b'x'))
            elif kind=='FIRST-WINS':
                self.attach(sid,speed=1500,extra=self.reset_tlv(hold=10)+self.reset_tlv(hold=20))
                require(self.a.state()['touch'][-1][2]==10,'a later copy of reset was used')
            elif kind=='BUSY':
                self.attach(sid);self.refused(self.attach_request(sid,pair=(0,1)),'unavailable',cause='pin_in_use')   # never joined
                self.refused(self.attach_request(sid,fn=2,pair=(0,65535)),'unavailable',cause='pin_in_use')
            elif kind=='CAPACITY':
                self.attach(sid,pair=(0,1));self.attach(sid,pair=(2,3));self.refused(self.attach_request(sid,pair=(6,7)),'unavailable',cause='limit')
            elif kind=='FAILED':
                parent,_=self.attach(sid);stream,_=self.console(sid,parent);self.a.feed_bytes(stream,b'keep');before=self.a.state();self.a.fail_attach(True)
                frame=self.c.exchange(self.attach_request(sid,fn=3,pair=(4,5)))
                require(frame[3:6]==bytes((self.c.reg['resolutions']['completed'],self.c.reg['outcomes']['failed'],self.c.reg['status']['line'])),'failed bringup must complete with status line');tlvs(frame[6:])
                after=self.a.state();require(after['connections']==before['connections'] and after['streams']==before['streams'],'failed attach changed live resources')
                self.a.fail_attach(False)
            else:
                rid,req=self.attach(sid);old=self.c.exchange(req);require(old[3:5] in (b'\1\0',bytes((0,self.c.reasons['result_lost']))),'attach replay answer')   # recorded, or result_lost (core §5.1)
                self.detach(sid,1,rid);before=self.a.state()
                require(self.c.replayed(req,old,frame=True) and self.a.state()==before,'attach replay resurrected connection or repeated bringup')
    def console_case(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid)
            if kind=='UNSUPPORTED':
                for mechanism in (0,len(self.mech)+1,255):self.refused(self.console_request(sid,parent,mechanism),'unsupported',b'\0')   # 0, and past the list (core §7.7)
            elif kind=='RESOURCE':
                self.refused(self.console_request(sid,65535),'no_resource')
                arm,_=self.attach(sid,fn=3);self.refused(self.console_request(sid,arm),'unavailable',cause='wrong_state')
                stream,_=self.console(sid,parent);self.refused(self.console_request(sid,stream),'unavailable',cause='wrong_state')
            elif kind=='DUPLICATE':
                self.console(sid,parent,'sdi')
                for mechanism in ('sdi','dmdata'):self.refused(self.console_request(sid,parent,mechanism),'unavailable',cause='wrong_state')
            elif kind=='MALFORMED':
                self.refused(self.request(4,'open',sid,struct.pack('<I',parent)),'malformed')
                self.refused(self.console_request(sid,parent,extra=b'\x7f'),'malformed')
            elif kind=='INDEPENDENT':
                s1,_=self.console(sid,parent);p2,_=self.attach(sid,pair=(2,3));s2,_=self.console(sid,p2)
                self.a.feed_bytes(s1,b'one');self.a.feed_bytes(s2,b'two')
                value=self.c.success(self.request(4,'streams',0,b'\0\0'))
                require(value[:2]==b'\0\x02' and len(value)>=20,'console streams page');tlvs(value[20:])
                require([struct.unpack_from('<IIB',value,i) for i in (2,11)]==[(s1,parent,self.mechanism('dmdata')),(s2,p2,self.mechanism('dmdata'))],'console streams ordering/entry shape')
                before=self.a.state()
                for stream,data in ((s1,b'one'),(s2,b'two')):
                    # len is max or less and more is set while bytes remain (common §1.2): read on to the end
                    got,mode,arg=b'',1,0
                    while True:
                        read=self.c.success(self.request(4,'read',0,struct.pack('<IBQH',stream,mode,arg,32)));require(len(read)>=11,'console read shape')
                        start,flags,count=struct.unpack_from('<QBH',read);require(start==len(got) and not flags&2 and len(read)>=11+count,'console read selected wrong stream');tlvs(read[11+count:])
                        got+=read[11:11+count];mode,arg=0,start+count
                        if not flags&1 or not count:break
                    require(got==data and not flags&1,'console read selected wrong stream')
                require(self.a.state()==before,'console read consumed buffer')
            else:
                stream,req=self.console(sid,parent);self.a.feed_bytes(stream,b'old');old=self.c.exchange(req);require(old[3:5] in (b'\1\0',bytes((0,self.c.reasons['result_lost']))),'console open replay answer');self.close(sid,stream)
                new,_=self.console(sid,parent);state=self.a.state();s=state['streams'][new]
                require(new>stream and s['position']==0 and not s['data'] and not s['tx'],'console recreate reused state')
                require(self.c.replayed(req,old,frame=True) and self.a.state()==state,'console open replay resurrected stream')
    def locks(self):
        self.fresh();self.refused(self.attach_request(0,extra=b'\xfe'),'session_required')
        self.refused(self.console_request(0,0),'session_required')
        with self.c.holding(3000) as sid:
            self.refused(self.attach_request(sid%0xffffffff+1,extra=b'\xfe'),'locked')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference and self.a.mechanisms==['sdi','dmdata'],'explicit logical sample required')
            self.specs={fn:next(row for row in self.c.reg['interface'] if row['name']==name and row['revision']==1) for fn,name in self.a.selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            # every number by name from the selected SPEC; the wire family shares attach's reset TLV (debug §3, core §2.5)
            require(all('reset' in self.specs[fn]['tlv']['attach'] for fn in (1,2,3)) and len({self.specs[fn]['tlv']['attach']['reset'] for fn in (1,2,3)})==1
                    and set(self.specs[4]['names']['mechanism'])>={'sdi','dmdata'},'selected SPEC lacks what the fixture contract uses')
            self.unknown=max(self.specs[1]['tlv']['attach'].values())+1   # a tag attach does not define (core §2.3)
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot()
            for fn,name in self.a.selection.items():
                require(any(row['fn']==fn and row['name']==name and row['revision']==1 for row in entries),'standard fn/name/revision mismatch')
                rows=dict(reversed(self.c.describe(fn)));offered=ops(rows[self.c.reg['describe_common']['ops']])
                # optional: a wire's scan (debug §0); the console's marks / clear / mark / write (console §1: the minimum is
                # open / read / close / streams)
                optional={'scan'}|({'marks','clear','mark','write'} if name=='oep.target.console' else set())
                require({code for op,code in self.operations[fn].items() if op not in optional}<=offered,'mandatory standard ops missing')
                if fn in (1,2,3):                              # the reset TLV takes only a declared role 3 channel (debug §3)
                    lines=set()
                    for tag,v in self.c.describe(fn):
                        if tag==self.c.reg['describe_common']['role_channels'] and len(v)>=3 and v[0]==self.specs[fn]['enum']['pin_role']['reset']:
                            base=struct.unpack_from('<H',v,1)[0];lines|={base+i for i in range(8*(len(v)-3)) if v[3+i//8]>>(i%8)&1}
                    require(self.a.reset_channel in lines,'reset TLV channel not declared as role 3 (reset)')
            listed=[v.decode() for tag,v in self.c.describe(4) if tag==self.specs[4]['tlv']['describe']['mechanism']]   # a named list (core §7.7)
            require(listed==self.a.mechanisms,'declared mechanism list differs');self.mech={name:i+1 for i,name in enumerate(listed)}
            self.c.observed['attach_fixture']={'selection':self.a.selection,'pairs':self.a.pairs,'mechanisms':self.a.mechanisms,'scope':'logical-model','reference':self.a.reference}
        self.c.check('OEP-ATTACH-CONTEXT','debug §1/3/5; console §1',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('WIRE-'+kind,lambda kind=kind:self.wire_case(kind)) for kind in ('SHORT','ZERO-SPEED','WIDTH','LOW-SPEED','PINS','METHOD','TLV','MALFORMED','UNMARKED','FIRST-WINS','BUSY','CAPACITY','FAILED','REPLAY')]
        cases += [('CONSOLE-'+kind,lambda kind=kind:self.console_case(kind)) for kind in ('UNSUPPORTED','RESOURCE','DUPLICATE','MALFORMED','INDEPENDENT','RECREATE-REPLAY')]+[('LOCKS',self.locks)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-ATTACH-'+name,'debug §1/3/5; console §1/2/3; core §2.3/5.2',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','full_conformance':False,
                'scope':'logical standard attach admission and console open constraints','checks':rows,'observed':self.c.observed,'adapter_trace':self.a.trace,
                'levels':{'core':'session/replay prerequisites','interface':'common contracts checked separately','oep-interface':'selected wire attach and console open'},
                'unchecked':['physical bringup/scratch/speed/pin idle','scan','reset/idle_clock/targetsel behavior','target interface operations',
                             'console mailbox/framing/delivery and SDI zero-count write','notifications','other capacities and concurrent requests','production Host integration']}
