"""SPEC mandatory source/sink checks through raw requests and protected state."""
import struct

from .conformance import require, tlvs, ops
from .conformance_registered_ops import RegisteredOpsChecks


class LinkTransferChecks:
    cases=('SOURCE','SINK','TLV','MALFORMED','SESSION','REPLAY','PORT-ABSENT')

    def __init__(self,checks,adapter):self.c,self.a=checks,adapter;self.observations=[]
    @staticmethod
    def field(tag,value):return bytes((tag,))+len(value).to_bytes(2,'little')+value
    def request(self,name,payload=b'',sid=0):
        return self.c.request(self.codes[name],payload,fn=self.a.link_fn,session=sid)
    def unchanged(self,action,request,before):
        after=self.a.state()
        self.observations.append(dict(action=action,request_hex=request.hex(),before=before,after=after))
        require(after==before,action+' changed protected state')
    def source(self,length,sid=0,tail=b''):
        before=self.a.state();request=self.request('source',struct.pack('<I',length)+tail,sid)
        payload=self.c.success(request)
        expected=min(length,self.c.max_frame-7)
        require(len(payload)>=2 and int.from_bytes(payload[:2],'little')==expected,'source route-bound len differs')
        require(len(payload)>=2+expected and payload[2:2+expected]==bytes(i&255 for i in range(expected)),'source data length/pattern differs')
        tlvs(payload[2+expected:])
        require(len(payload)+5<=self.c.max_frame,'source result exceeds current route max_frame')
        self.unchanged('source',request,before)
        return request,payload
    def sink(self,data,sid=0,tail=b''):
        require(len(data)<=self.c.max_frame-12,'invalid sink fixture geometry')
        before=self.a.state();request=self.request('sink',struct.pack('<H',len(data))+data+tail,sid)
        require(len(request)<=self.c.max_frame,'sink request fixture exceeds route max_frame')
        payload=self.c.success(request);tlvs(payload)
        self.unchanged('sink',request,before)
        return request,payload
    def refused(self,request,reason,tag=None):
        before=self.a.state();payload=self.c.rejected(request,reason)
        if tag is not None:require(payload and payload[0]==tag,'unsupported did not echo received tag');tlvs(payload[1:])
        self.unchanged('refusal',request,before)
    def context(self):
        require(self.a.selection=={self.a.link_fn:'oep.probe.link'} and self.a.port_speed is False,
                'explicit mandatory-only link selection required')
        require(self.a.reset_scope=='logical-model' and all(callable(getattr(self.a,n,None)) for n in ('state','seed','restart_fixture')),
                'logical reset and independent protected state/seed required')
        require(self.a.retained_bytes>=23,'explicit small replay request/result retention required')
        spec=next(s for s in self.c.reg['interface'] if s['name']=='oep.probe.link')
        self.codes={row['name']:row['code'] for row in spec['op']}
        require(self.codes==dict(source=16,sink=17,port_speed=18,port_state=19),'selected link operation numbers differ')
        offered=ops(dict(reversed(self.c.describe(self.a.link_fn)))[self.c.reg['describe_common']['ops']])
        require(offered=={16,17},'mandatory-only profile must not advertise port_speed or unadopted ops')
    def fresh(self):
        identity=self.a.restart_fixture()
        require(identity['scope']=='logical-model' and identity['boot_id']!=self.c.boot,'fresh logical boot required')
        self.c.confirm();self.c.identity();require(self.c.boot==identity['boot_id'],'fresh boot readback')
    def case(self,name):
        self.fresh()
        with self.c.holding(3000) as sid:
            self.a.seed(self.c,sid)
            seed=self.a.state()
            self.observations.append(dict(action='seed',case=name,state=seed))
            require(seed['plans']=={2:[1]} and seed['pins'][1]==4 and seed['ownership']['holder']==sid,
                    'protected live GPIO/plan/ownership seed missing')
            if name=='SOURCE':
                for length in (0,1,self.c.max_frame-8,self.c.max_frame-7,self.c.max_frame-6,0xffffffff):self.source(length,sid)
            elif name=='SINK':
                for count in (0,1,self.c.max_frame-13,self.c.max_frame-12):
                    for data in (b'\0'*count,b'\xff'*count,bytes(i&255 for i in range(count))):self.sink(data,sid)
            elif name=='TLV':
                for op,base in (('source',struct.pack('<I',8)),('sink',b'\x03\x00abc')):
                    tail=self.field(127,b'\x06ignore')
                    if op=='source':self.source(8,sid,tail)
                    else:self.sink(b'abc',sid,tail)
                    self.refused(self.request(op,base+self.field(255,b'\x07ignored'),sid),'unsupported',255)
                    for tag in (0,128):self.refused(self.request(op,base+self.field(tag,b''),sid),'malformed')
                    for tail in (b'\x7f',b'\x7f\x00',b'\x7f\x02\x00x',b'\xff\x02\x00x'):
                        self.refused(self.request(op,base+tail,sid),'malformed')
            elif name=='MALFORMED':
                for n in range(4):self.refused(self.request('source',b'\0'*n,sid),'malformed')
                for payload in (b'',b'\x01',b'\x01\x00',b'\x04\x00abc',b'\xff\xffabc'):
                    self.refused(self.request('sink',payload,sid),'malformed')
            elif name=='SESSION':
                self.source(5);self.sink(b'abc') # lock-free while another session owns live pins
                self.source(5,sid);self.sink(b'abc',sid)
                for op,p in (('source',struct.pack('<I',5)),('sink',b'\x03\x00abc')):
                    self.refused(self.request(op,p,sid+1),'locked')
            elif name=='REPLAY':
                for req,payload in (self.source(16,sid),self.sink(b'abc',sid)):
                    before=self.a.state();require(self.c.success(req)==payload,'same request replay changed result')
                    self.unchanged('replay',req,before)
                    self.refused(req+b'\x7f\0\0','malformed')
            elif name=='PORT-ABSENT':
                for session in (0,sid):
                    self.refused(self.request('port_speed',struct.pack('<IBH',230400,0,100),session),'unknown_operation')
        require(not self.a.state()['plans'],'fixture end did not release seeded plan')
        if name=='SESSION':
            self.source(5);self.sink(b'')
            for op,p in (('source',struct.pack('<I',5)),('sink',b'\0\0')):
                self.refused(self.request(op,p,sid),'no_session')
    def run(self):
        start=len(self.c.results)
        RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-LINK-TRANSFER-CONTEXT','link §1–2; explicit fixture',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-LINK-TRANSFER-'+name,'link §1–2; core §2.3/5/6',lambda name=name:self.case(name))
        checks=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in checks) else 'failed',checks=checks,
            full_conformance=False,scope='selected route mandatory link source/sink with protected live GPIO/plan',
            observed=self.c.observed,state_observations=self.observations,unchecked=['port_speed handshake and timing','other routes/transport framing',
                'true concurrent requests and other resources','physical bandwidth/production implementations'])
