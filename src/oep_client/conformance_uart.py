"""SPEC-driven logical UART operation and plan-owned stream checks."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_registered_ops import RegisteredOpsChecks, refusal_carries


class UartChecks:
    cases=('DEFAULT','RX-ONLY','TX-ONLY','CONFIGURE','CONFIGURE-FAIL','BAUD-BOUNDARY','FORMAT','FORMAT-SHAPE','FIRST-WINS','TLV','RECONFIGURE','READ','OVERFLOW','MARKS','CLEAR','WRITE','WRITE-REPLAY','CONFIGURE-REPLAY','COUNTS','SESSION','RECREATE','REPLACE','END','EXPIRY','FORCE')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    @staticmethod
    def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value
    def req(self,fn,name,sid=0,payload=b''):return self.c.request(self.codes[fn][name],payload,fn=fn,session=sid)
    def plan(self,sid,roles=(1,2)):
        payload=b''.join(self.tlv(self.assignment,struct.pack('<HBH',2,role,role-1)) for role in roles);tlvs(self.c.success(self.req(1,'plan_apply',sid,payload)))
    def release(self,sid):tlvs(self.c.success(self.req(1,'plan_release',sid,b'\1\2\0')))
    def config_request(self,sid,baud=115200,format_value=None,raw=None,extra=b''):
        payload=struct.pack('<I',baud)
        if format_value is not None:payload+=self.tlv(self.format_tag if raw is None else raw,bytes((format_value,)))
        return self.req(2,'configure',sid,payload+extra)
    def configure(self,sid,baud=115200,format_value=None,**kwargs):
        value=self.c.success(self.config_request(sid,baud,format_value,**kwargs));require(len(value)>=4,'configure actual baud');actual=struct.unpack_from('<I',value)[0];tlvs(value[4:]);require(actual>0 and abs(actual-baud)*100<=baud*self.tolerance,'actual baud exceeds tolerance');return actual
    def status(self):
        before=self.a.state();value=self.c.success(self.req(2,'status'));require(len(value)>=5,'UART status width');baud,format_value=struct.unpack_from('<IB',value);tlvs(value[5:]);require(self.a.state()==before,'status changed UART');return baud,format_value
    def read(self,mode=0,arg=0,maximum=65535):
        before=self.a.state();value=self.c.success(self.req(2,'read',payload=struct.pack('<BQH',mode,arg,maximum)));require(len(value)>=11,'UART read header');start,flags,count=struct.unpack_from('<QBH',value);require(count<=maximum and len(value)>=11+count,'UART read count');data=value[11:11+count];tlvs(value[11+count:]);require(self.a.state()==before,'read consumed UART bytes or changed state');return start,flags,data
    def marks(self,first=0):
        before=self.a.state();rows=[]
        for _ in range(8):
            value=self.c.success(self.req(2,'marks',payload=struct.pack('<I',first)));require(len(value)>=2 and value[0] in (0,1) and len(value)>=2+22*value[1],'UART marks page shape');page=[struct.unpack_from('<IQBQB',value,2+22*i) for i in range(value[1])];tlvs(value[2+22*value[1]:]);rows+=page
            if not value[0]:break
            require(page,'marks paging progress');first=(page[-1][0]+1)&0xffffffff
        else:raise ValueError('marks did not terminate')
        require(self.a.state()==before,'marks mutated UART');return rows
    def write_request(self,sid,data):return self.req(2,'write',sid,struct.pack('<H',len(data))+data)
    def write(self,sid,data,accepted,outcome):
        req=self.write_request(sid,data);reply=self.c.exchange(req);require(reply[3:5]==bytes((1,outcome)) and len(reply)>=7 and struct.unpack_from('<H',reply,5)[0]==accepted,'UART accepted/outcome');tlvs(reply[7:]);return req,reply
    def refused(self,req,reason,payload=None):
        before=self.a.state();value=self.c.rejected(req,reason)
        if payload is not None:require(refusal_carries(reason,value,payload),'UART refusal payload/raw tag/cause')
        require(self.a.state()==before,'refused UART request touched stream/config/pins')
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'new logical boot');self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'boot readback')
    def closed(self):
        state=self.a.state();require(not state['plans'] and not state['live'] and not state['data'] and not state['tx'] and state['pins']==state['idle'],'plan close retained UART stream/queue/pin driving');return state
    def case(self,kind):
        self.fresh()
        with self.c.holding(1000) as sid:
            if kind=='RX-ONLY':
                self.plan(sid,(1,));self.configure(sid);self.a.feed_bytes(b'rx');require(self.read()[2]==b'rx','RX-only input');self.refused(self.write_request(sid,b'x'),'unavailable',self.tlv(1,b'\6'));return
            if kind=='TX-ONLY':
                self.plan(sid,(2,));self.configure(sid);self.write(sid,b'tx',2,0);require(self.a.drain(2)==b'tx','TX-only logical queue');return
            if kind=='CONFIGURE':
                self.refused(self.config_request(sid),'unavailable',self.tlv(1,b'\6'))
            self.plan(sid)
            if kind not in ('DEFAULT','CONFIGURE','CONFIGURE-FAIL'):self.configure(sid)   # nothing is received before a configure
            if kind=='DEFAULT':
                state=self.a.state();require(state['live'] and state['pins'][1]==4 and state['pins'][0]==state['idle'][0],'plan did not start UART/TX idle before configure')
                require(self.status()==(0,0),'unconfigured status must be baud 0 format 0');self.a.feed_bytes(b'early');require(self.read()[2]==b'','received before configure')
                self.refused(self.write_request(sid,b'x'),'unavailable',self.tlv(1,b'\6'));self.configure(sid);self.a.feed_bytes(b'late');require(self.read()[2]==b'late','configure did not start reception')
            elif kind=='CONFIGURE-FAIL':
                self.configure(sid,57600,4);self.a.feed_bytes(b'kept');self.write(sid,b'tx',2,0);self.a.fail_configure()
                reply=self.c.exchange(self.config_request(sid,115200));require(reply[3:5]==b'\1\1' and len(reply)>=9 and struct.unpack_from('<I',reply,5)[0]==0,'failed configure must answer completed failed baud 0');tlvs(reply[9:])
                state=self.a.state();require(state['live'] and not state['tx'] and self.status()==(0,0),'failed configure left the UART configured or kept unsent TX')
                require(self.read()[2]==b'kept','failed configure lost received bytes');self.a.feed_bytes(b'lost');require(self.read()[2]==b'kept','received while not configured')
                self.refused(self.write_request(sid,b'x'),'unavailable',self.tlv(1,b'\6'));self.configure(sid);require(self.status()==(115200,0),'new configure did not recover')
            elif kind=='CONFIGURE':
                for raw in (self.format_tag,self.format_tag|128):
                    actual=self.configure(sid,57600,4,raw=raw);require(self.status()==(actual,4),'actual configure/status');self.configure(sid);require(self.status()[1]==0,'omitted format must mean 8N1')
            elif kind=='BAUD-BOUNDARY':
                for baud in (1755429,1940210):require(self.configure(sid,baud)==1843200,'fixture divider boundary')
                for baud in (1755428,1940211):self.refused(self.config_request(sid,baud),'unsupported',b'\0')
                self.refused(self.config_request(sid,0),'malformed')   # baud 0 is malformed (fixture §2)
            elif kind=='FORMAT':
                for raw in (self.format_tag,self.format_tag|128):
                    for value in (1,2,12,32,255):self.refused(self.config_request(sid,format_value=value,raw=raw),'unsupported',bytes((raw,)))
            elif kind=='FORMAT-SHAPE':
                for value in (b'',b'\0\0'):self.refused(self.config_request(sid,extra=self.tlv(self.format_tag,value)),'malformed')
            elif kind=='FIRST-WINS':
                self.configure(sid,format_value=4,extra=self.tlv(self.format_tag|128,b'\0'));require(self.status()[1]==4,'duplicate format did not retain first')
                self.refused(self.config_request(sid,57600,0,extra=self.tlv(self.format_tag|128,b'\xff')),'unsupported',bytes((self.format_tag|128,)))   # a later copy is checked too (core §2.3)
            elif kind=='TLV':
                self.configure(sid,extra=self.tlv(126,b'x'));self.refused(self.config_request(sid,extra=self.tlv(254,b'x')),'unsupported',b'\xfe');self.refused(self.config_request(sid,extra=b'\x7e\3\0x'),'malformed')
            elif kind=='RECONFIGURE':
                self.a.feed_bytes(b'keep');tlvs(self.c.success(self.req(2,'mark',sid,b'\x11')));before=self.a.state();self.configure(sid,57600,17);after=self.a.state();require(all(after[k]==before[k] for k in ('position','data','marks','next_serial')),'reconfigure discarded received bytes/positions/marks');require(self.read()[2]==b'keep','reconfigure lost readback')
            elif kind=='READ':
                self.a.feed_bytes(b'abcdef');require(self.read(maximum=2)==(0,1,b'ab'),'read max/more');require(self.read(arg=2)==(2,0,b'cdef'),'read continuation');require(self.read(arg=100)==(6,0,b''),'future position');require(self.read(mode=2)==(6,0,b''),'read now');self.refused(self.req(2,'read',payload=struct.pack('<BQH',4,0,1)),'unsupported',b'\0')
            elif kind=='OVERFLOW':
                data=bytes(range(80));self.a.feed_bytes(data);start,flags,part=self.read();require(start==16 and flags&2 and part==data[16:16+len(part)] and flags&1,'overflow gap/frame bound');require(self.read(arg=start+len(part))[2]==data[start+len(part):],'overflow continuation');require(any(r[2]==1 and r[4]==1 for r in self.marks()),'overflow lost mark')
            elif kind=='MARKS':
                self.a.feed_bytes(b'ab');self.a.error(2);self.a.feed_bytes(b'c');self.a.error(3);rows=self.marks();require([(r[1],r[2],r[4]) for r in rows]==[(2,1,2),(3,1,3)],'framing/parity lost mark or position');require(rows[0][3]<=rows[1][3],'mark time decreased');require(self.marks(rows[-1][0])==[rows[-1]],'marks cursor not inclusive')
            elif kind=='CLEAR':
                self.a.feed_bytes(b'abc');tlvs(self.c.success(self.req(2,'clear',sid)));require(self.read()==(3,2,b''),'clear reset position or failed gap');tlvs(self.c.success(self.req(2,'mark',sid,b'\x22')));require([(r[1],r[2],r[4]) for r in self.marks()]==[(3,2,0),(3,3,34)],'clear/host marks');require(self.read(mode=3,arg=3)==(3,0,b''),'read from last host mark')
            elif kind=='WRITE':
                self.write(sid,b'abc',3,0);self.write(sid,b'def',1,2);require(self.a.state()['tx']==b'abcd','write must accept prefix');self.write(sid,b'x',0,1);self.write(sid,b'',0,0);require(self.a.drain(4)==b'abcd','queue drain order');self.write(sid,b'ef',2,0)
            elif kind=='WRITE-REPLAY':
                req,reply=self.write(sid,b'ab',2,0);self.a.drain(2);before=self.a.state();require(self.c.exchange(req)==reply and self.a.state()==before,'write replay enqueued or delivered twice')
            elif kind=='CONFIGURE-REPLAY':
                req=self.config_request(sid,57600,4);self.c.success(req);self.configure(sid,115200,17);before=self.a.state();self.c.success(req);require(self.a.state()==before and self.status()==(115200,17),'configure replay restored old UART settings')
            elif kind=='COUNTS':
                for name,payload in [('write',b'\2\0x'),('read',b'\0'),('marks',b'\0'),('configure',b'\0')]:self.refused(self.req(2,name,sid,payload),'malformed')
            elif kind=='SESSION':
                for name,payload in [('configure',struct.pack('<I',115200)),('write',b'\0\0'),('clear',b''),('mark',b'\0')]:self.refused(self.req(2,name,0,payload),'session_required')
                self.read();self.marks();self.status()
            elif kind in ('RECREATE','REPLACE'):
                self.a.feed_bytes(b'old');self.configure(sid,57600,4);tlvs(self.c.success(self.req(2,'mark',sid,b'\7')));before=self.a.state()
                if kind=='REPLACE':
                    payload=self.tlv(self.assignment,struct.pack('<HBH',2,1,0));self.refused(self.req(1,'plan_apply',sid,payload),'unavailable',self.tlv(1,b'\6'))
                    require(self.a.state()==before,'refused configured replacement changed the UART');self.release(sid);self.plan(sid,(1,))
                else:self.release(sid);self.closed();self.plan(sid)
                state=self.a.state();require(state['position']==before['position'] and ((state['next_serial']-before['next_serial'])&0xffffffff)<0x40000000 and not state['data'] and not state['tx'],'plan recreate reset counters or retained data');require(self.status()==(0,0),'plan did not discard session configure');self.configure(sid);self.a.feed_bytes(b'new');require(self.read()==(3,2,b'new'),'recreated UART byte positions')
                if kind=='REPLACE':require(state['pins'][1]==state['idle'][1],'replacement RX-only left TX driving')
            elif kind=='END':
                self.a.feed_bytes(b'x');self.write(sid,b'tx',2,0);tlvs(self.c.success(self.c.request(self.c.core['end'],session=sid)));self.c.session=None;require(self.closed()['position']==1,'end reset UART position')
            elif kind=='EXPIRY':
                self.a.feed_bytes(b'x');self.write(sid,b'tx',2,0);self.a.advance(1000);require(struct.unpack_from('<BI',self.c.success(self.c.request(self.c.core['lock_state'])))==(0,0),'UART session did not expire');require(self.closed()['position']==1,'expiry reset UART position');self.c.session=None
            elif kind=='FORCE':
                self.a.feed_bytes(b'old');tlvs(self.c.success(self.req(2,'mark',sid,b'\7')));old=self.a.state();req,_=self.write(sid,b'tx',2,0);replacement=secrets.randbelow(0xffffffff)+1
                while replacement==sid:replacement=secrets.randbelow(0xffffffff)+1
                self.c.corr=0;opened=self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=replacement));self.c.session=replacement;require(struct.unpack_from('<II',opened)==(1000,self.c.boot),'force result');tlvs(opened[8:]);closed=self.closed();require(closed['position']==old['position'] and ((closed['next_serial']-old['next_serial'])&0xffffffff)<0x40000000,'force reset UART counters');self.plan(replacement);self.configure(replacement);self.a.feed_bytes(b'new');require(self.read()==(3,2,b'new'),'force/recreate reset UART position');before=self.a.state();self.c.rejected(req,'locked');require(self.a.state()==before,'old UART write touched new owner')
            else:raise ValueError(kind)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.selection=={1:'oep.probe.plan',2:'oep.fixture.uart'},'explicit logical UART selection required');base=RegisteredOpsChecks(self.c,self.a.selection);require(base.run()['status']=='passed','UART standard op context');self.codes={fn:{r['name']:r['code'] for r in base.specs[name]['op']} for fn,name in self.a.selection.items()};self.assignment=base.specs['oep.probe.plan']['tlv']['plan_apply']['role_assignment'];self.format_tag=base.specs['oep.fixture.uart']['tlv']['configure']['format'];self.tolerance=self.c.reg['limits']['uart_baud_tolerance_pct'];require(self.tolerance==5 and self.a.state()['clock_scope']=='explicit-logical-ms','selected logical UART limits/clock differ')
        self.c.check('OEP-UART-CONTEXT','fixture §2; common §1; plan §2',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for kind in self.cases:
            first=len(self.a.trace);self.c.check('OEP-UART-'+kind,'fixture §2; common §1; plan §2; core §6/8/9',lambda kind=kind:self.case(kind));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,scope='explicit logical UART configure/stream/queue and plan lifecycle',unchecked=['physical UART waveform/baud/drive and delivery','persisted UART presets and default fallback from unrealizable preset','other UART pin candidates/channel_group and concurrent writers','full mark eviction/wrap geometries on this UART fixture','production Host and other standard operation behaviors'])
