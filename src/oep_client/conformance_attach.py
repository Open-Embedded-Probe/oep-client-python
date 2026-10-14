"""SPEC-driven standard attach admission and console open checks."""
import struct
from .conformance import require, ops, tlvs


class AttachChecks:
    def __init__(self, checks, adapter):self.c,self.a=checks,adapter
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'fresh logical boot')
        self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'boot readback')
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
        require(len(value)>=13,'attach fixed response');rid,identity,flags,speed=struct.unpack_from('<IIBI',value);tlvs(value[13:])
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
        expected=[((id,*s['pins'],s['speed'],0,0),b'') for id,s in state['connections'].items() if s['fn']==fn]
        require(rows==expected and self.a.state()==before,'connections enumeration/routing mutated state or lost metadata')
        return rid,req
    def refused(self, req, reason, payload=None, cause=None):
        before=self.a.state();value=self.c.rejected(req,reason)
        if payload is not None:require(value==payload,'refusal payload/raw tag')
        if cause is not None:require(dict(reversed(tlvs(value))).get(1)==bytes((cause,)),'unavailable cause')
        require(self.a.state()==before,'refused request changed resource/allocator or touched target')
    def detach(self, sid, fn, rid):tlvs(self.c.success(self.request(fn,'detach',sid,struct.pack('<I',rid))))
    def console_request(self, sid, parent, mechanism=1, extra=b''):
        return self.request(self.a.console_fn,'open',sid,struct.pack('<IB',parent,mechanism)+extra)
    def console(self, sid, parent, mechanism=1):
        req=self.console_request(sid,parent,mechanism);value=self.c.success(req)
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
            elif kind=='TLV':
                self.refused(self.attach_request(sid,extra=self.tlv(0xfe,b'x')),'unsupported',b'\xfe')
                for marked in (True,False):                    # not a reset line: the tag as received (core §2.3)
                    self.refused(self.attach_request(sid,extra=self.reset_tlv(channel=0,marked=marked)),'unsupported',
                                 bytes((self.specs[1]['tlv']['attach']['reset']|(128 if marked else 0),)))
                self.attach(sid,extra=self.tlv(0x7e,b'x'))
            elif kind=='MALFORMED':
                for tail in (b'\0\0\0',b'\x85',b'\xfe\x04\0x',self.reset_tlv()+self.tlv(0x85,b'x')):
                    self.refused(self.attach_request(sid,extra=tail),'malformed')
            elif kind=='UNMARKED':self.attach(sid,extra=self.reset_tlv(marked=False)+self.tlv(0x7e,b'x'))
            elif kind=='FIRST-WINS':
                self.attach(sid,speed=1500,extra=self.reset_tlv(hold=10)+self.reset_tlv(hold=20))
                require(self.a.state()['touch'][-1][2]==10,'a later copy of reset was used')
            elif kind=='BUSY':
                self.attach(sid);self.refused(self.attach_request(sid,pair=(0,1)),'unavailable',cause=1)   # never joined
                self.refused(self.attach_request(sid,fn=2,pair=(0,65535)),'unavailable',cause=1)
            elif kind=='CAPACITY':
                self.attach(sid,pair=(0,1));self.attach(sid,pair=(2,3));self.refused(self.attach_request(sid,pair=(6,7)),'unavailable',cause=2)
            elif kind=='FAILED':
                parent,_=self.attach(sid);stream,_=self.console(sid,parent);self.a.feed_bytes(stream,b'keep');before=self.a.state();self.a.fail_attach(True)
                frame=self.c.exchange(self.attach_request(sid,fn=3,pair=(4,5)))
                require(frame[3:6]==b'\x01\x01\x02','failed bringup must complete with status line');tlvs(frame[6:])
                after=self.a.state();require(after['connections']==before['connections'] and after['streams']==before['streams'],'failed attach changed live resources')
                self.a.fail_attach(False)
            else:
                rid,req=self.attach(sid);old=self.c.success(req);self.detach(sid,1,rid);before=self.a.state()
                require(self.c.success(req)==old and self.a.state()==before,'attach replay resurrected connection or repeated bringup')
    def console_case(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid)
            if kind=='UNSUPPORTED':
                for mechanism in (2,3,255):self.refused(self.console_request(sid,parent,mechanism),'unsupported',b'\0')
            elif kind=='RESOURCE':
                self.refused(self.console_request(sid,65535),'no_resource')
                arm,_=self.attach(sid,fn=3);self.refused(self.console_request(sid,arm),'unavailable',cause=6)
                stream,_=self.console(sid,parent);self.refused(self.console_request(sid,stream),'unavailable',cause=6)
            elif kind=='DUPLICATE':
                self.console(sid,parent,0)
                for mechanism in (0,1):self.refused(self.console_request(sid,parent,mechanism),'unavailable',cause=6)
            elif kind=='MALFORMED':
                self.refused(self.request(4,'open',sid,struct.pack('<I',parent)),'malformed')
                self.refused(self.console_request(sid,parent,extra=b'\x7f'),'malformed')
            elif kind=='INDEPENDENT':
                s1,_=self.console(sid,parent);p2,_=self.attach(sid,pair=(2,3));s2,_=self.console(sid,p2)
                self.a.feed_bytes(s1,b'one');self.a.feed_bytes(s2,b'two')
                value=self.c.success(self.request(4,'streams',0,b'\0\0'))
                require(value[:2]==b'\0\x02' and len(value)>=20,'console streams page');tlvs(value[20:])
                require([struct.unpack_from('<IIB',value,i) for i in (2,11)]==[(s1,parent,1),(s2,p2,1)],'console streams ordering/entry shape')
                before=self.a.state()
                for stream,data in ((s1,b'one'),(s2,b'two')):
                    read=self.c.success(self.request(4,'read',0,struct.pack('<IBQH',stream,1,0,32)))
                    require(len(read)>=14 and struct.unpack_from('<QBH',read)==(0,0,3) and read[11:14]==data,'console read selected wrong stream');tlvs(read[14:])
                require(self.a.state()==before,'console read consumed buffer')
            else:
                stream,req=self.console(sid,parent);self.a.feed_bytes(stream,b'old');old=self.c.success(req);self.close(sid,stream)
                new,_=self.console(sid,parent);state=self.a.state();s=state['streams'][new]
                require(new>stream and s['position']==0 and not s['data'] and not s['tx'],'console recreate reused state')
                require(self.c.success(req)==old and self.a.state()==state,'console open replay resurrected stream')
    def locks(self):
        self.fresh();self.refused(self.attach_request(0,extra=b'\xfe'),'session_required')
        self.refused(self.console_request(0,0),'session_required')
        with self.c.holding(3000) as sid:
            self.refused(self.attach_request(sid%0xffffffff+1,extra=b'\xfe'),'locked')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference and self.a.mechanisms==[0,1],'explicit logical sample required')
            self.specs={fn:next(row for row in self.c.reg['interface'] if row['name']==name and row['revision']==1) for fn,name in self.a.selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            require(all(self.operations[fn]['attach']==17 and self.specs[fn]['tlv']['attach']=={'reset':5} for fn in (1,2,3)) and
                    self.specs[4]['enum']['mechanism']=={'sdi':0,'dmdata':1,'dmseq':2,'none':255},'selected SPEC differs from fixture contract')
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot()
            for fn,name in self.a.selection.items():
                require(any(row['fn']==fn and row['name']==name and row['revision']==1 for row in entries),'standard fn/name/revision mismatch')
                rows=dict(reversed(self.c.describe(fn)));offered=ops(rows[self.c.reg['describe_common']['ops']])
                require({code for op,code in self.operations[fn].items() if op!='scan'}<=offered,'mandatory standard ops missing')
            declared=rows[self.specs[4]['tlv']['describe']['mechanisms']]
            require(declared[:1]==bytes((len(declared)-1,)) and list(declared[1:])==self.a.mechanisms,'declared mechanism subset differs (n(u8) n x mechanism)')
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
