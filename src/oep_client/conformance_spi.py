"""SPEC-first SPI target operations and logical edge checks."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_registered_ops import RegisteredOpsChecks, refusal_carries


class SpiChecks:
    cases=('STATE0','ARM-STATE0','PLAN-SHAPE','PLAN-TAGS','RUNNING-PLAN','CONFIGURE','CONFIGURE-FAIL','FORMAT','LSB-FEATURE','TLV','MODES','PARTIAL','EMPTY-TX','UNARMED','ZERO-BITS','TRUNCATE','OVERFLOW','DOUBLE-ERROR','FIFO','TIMESTAMP','ARM-LIMIT','ARM-BUSY','ARM-READY','RECONFIGURE','MISO-GATE','REPLAY-RX','REPLAY-ARM','REPLAY-CONFIG','RELEASE','END','EXPIRY','FORCE','ACTIVE-RELEASE','ACTIVE-END','ACTIVE-EXPIRY','ACTIVE-FORCE','SESSION','COUNTS')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    @staticmethod
    def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value
    def req(self,fn,name,sid=0,payload=b''):return self.c.request(self.codes[fn][name],payload,fn=fn,session=sid)
    def ok(self,req):v=self.c.success(req);tlvs(v);return v
    def plan_request(self,sid,roles=((1,0),(2,1),(3,2),(4,3))):return self.req(1,'plan_apply',sid,b''.join(self.tlv(self.assignment,struct.pack('<HBH',2,role,ch)) for role,ch in roles))
    def plan(self,sid):self.ok(self.plan_request(sid))
    def release(self,sid):self.ok(self.req(1,'plan_release',sid,b'\1\2\0'))
    def configure(self,sid,mode=0,order=0,extra=b''):self.ok(self.req(2,'configure',sid,bytes((mode,order))+extra))
    def arm_request(self,sid,length=8,tx=b'',extra=b''):return self.req(2,'arm',sid,struct.pack('<HH',length,len(tx))+tx+extra)
    def arm(self,sid,length=8,tx=b''):self.ok(self.arm_request(sid,length,tx))
    def status(self):
        before=self.a.state();v=self.c.success(self.req(2,'status'));require(len(v)>=13,'SPI status width');result=struct.unpack_from('<BBBBBII',v);tlvs(v[13:]);require(self.a.state()==before,'status changed target');return result
    def rx(self,sid):
        req=self.req(2,'read_rx',sid);v=self.c.success(req);require(len(v)>=7,'read_rx header');pending,bits,count=struct.unpack_from('<BIH',v);require(count<=self.maximum and len(v)>=7+count,'SPI read_rx count');tags=tlvs(v[7+count:]);require(all(len(value)==8 for tag,value in tags if tag&127==self.ns),'SPI timestamp width')
        # an empty queue: pending, bits and count all 0, no data and no timestamp (fixture §4; a queued transfer has bits >= 1)
        if count==0:require(pending==0 and bits==0 and not any(tag&127==self.ns for tag,_ in tags),'empty read_rx must be all 0 without a timestamp')
        return pending,bits,v[7:7+count],req,v
    def refused(self,req,reason,payload=None):
        before=self.a.state();v=self.c.rejected(req,reason)
        if payload is not None:require(refusal_carries(reason,v,payload),'SPI refusal raw tag/cause')
        require(self.a.state()==before,'rejected SPI request changed target/pins/queue')
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'explicit logical restart');self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'logical boot readback')
    @staticmethod
    def bits(data,order=0):return [(byte>>((i if order else 7-i)))&1 for byte in data for i in range(8)]
    @staticmethod
    def packed(bits,order=0):
        result=bytearray((len(bits)+7)//8)
        for k,bit in enumerate(bits):result[k//8]|=bit<<(k%8 if order else 7-k%8)
        return bytes(result)
    def transfer(self,bits,expected):
        result=self.a.transfer(bits);require(result['miso']==expected,'SPI MISO bit order/prefix/zero fill')
        state=self.a.state();require(state['pins'][2]==0 and all(state['pins'][ch]==0 for ch in (0,1,3)),'CS inactive MISO/inputs not HiZ')
        return result
    def closed(self):
        state=self.a.state();require(not state['configured'] and state['mode']==0 and state['bit_order']==0 and state['armed'] is None and not state['rx'] and state['transactions']==0 and state['errors']==0 and not state['active'] and not state['bits'] and state['pins']==state['idle'],'plan close retained SPI state/queue/arm/pins')
    def case(self,kind):
        self.fresh()
        with self.c.holding(1000) as sid:
            if kind=='STATE0':
                require(self.status()==(0,0,0,0,0,0,0),'initial status');self.refused(self.req(2,'configure',sid,b'\0\0'),'unavailable',self.tlv(1,b'\6'));self.plan(sid);require(self.a.state()['pins']==self.a.state()['idle'],'plan changed unconfigured pins');self.refused(self.req(2,'read_rx',sid),'unavailable',self.tlv(1,b'\6'));return
            if kind=='ARM-STATE0':
                for planned in (False,True):
                    if planned:self.plan(sid)
                    require(self.status()[0]==0,'arm fixture already configured')
                    req=self.arm_request(sid,1,b'x');self.refused(req,'unavailable',self.tlv(1,b'\6'))
                    if planned:self.configure(sid)
                    # the replay: the recorded refusal, or result_lost from a probe without records (core §5.1)
                    before=self.a.state();reply=self.c.exchange(req)
                    require(reply[3:5]==bytes((0,self.c.reasons['result_lost'])) or (reply[3:5]==bytes((0,self.c.reasons['unavailable'])) and refusal_carries('unavailable',reply[5:],self.tlv(1,b'\6'))),'state0 arm replay not refused');require(self.a.state()==before,'rejected SPI request changed target/pins/queue')
                    if planned:
                        require(self.status()[0]==1 and self.status()[3]==0,'old state0 refusal armed configured target')
                        self.arm(sid,1,b'x');self.transfer(self.bits(b'y'),self.bits(b'x'))
                return
            self.plan(sid)
            if kind!='PLAN-SHAPE':self.configure(sid)
            if kind=='PLAN-SHAPE':                                  # state 0: only the form of the plan applies
                for roles in (((1,0),(2,1),(3,2)),((1,0),(2,1),(3,2),(4,2)),((1,0),(2,1),(3,2),(4,3),(1,1))):self.refused(self.plan_request(sid,roles),'unsupported',bytes((self.assignment,)))
                self.refused(self.plan_request(sid,((1,0),(2,1),(3,2),(4,3),(1,0))),'malformed')   # the same (fn, role, channel) twice
            elif kind=='RUNNING-PLAN':
                self.arm(sid,1,b'x');self.transfer(self.bits(b'y'),self.bits(b'x'));self.arm(sid);before=self.a.state()
                self.ok(self.plan_request(sid));require(self.a.state()==before,'the same set changed a running target')
                self.refused(self.plan_request(sid,((1,1),(2,0),(3,2),(4,3))),'unavailable',self.tlv(1,b'\6'))
            elif kind=='CONFIGURE-FAIL':
                self.arm(sid,1,b'x');self.transfer(self.bits(b'y'),self.bits(b'x'));self.arm(sid);self.a.fail_configure()
                reply=self.c.exchange(self.req(2,'configure',sid,b'\0\0'));require(reply[3:5]==b'\1\1','failed configure must be completed failed');tlvs(reply[5:])
                require(self.status()==(0,0,0,0,0,0,0),'failed configure left state, queue, arm or counters')
                self.refused(self.arm_request(sid,1,b'x'),'unavailable',self.tlv(1,b'\6'));self.configure(sid);require(self.status()[0]==1,'new configure did not recover')
            elif kind=='PLAN-TAGS':
                self.refused(self.plan_request(sid,((5,0),(2,1),(3,2),(4,3))),'unsupported',bytes((self.assignment,)));self.refused(self.req(1,'plan_apply',sid,self.tlv(self.assignment,b'\0'*4)),'malformed')
            elif kind=='CONFIGURE':
                for mode in range(4):self.configure(sid,mode);require(self.status()==(1,mode,0,0,0,0,0),'mode status')
            elif kind=='FORMAT':
                for mode,order in ((4,0),(255,0),(0,2),(0,255)):self.refused(self.req(2,'configure',sid,bytes((mode,order))),'malformed')
            elif kind=='LSB-FEATURE':
                if self.lsb:self.configure(sid,3,1);require(self.status()[1:3]==(3,1),'LSB declared but refused')
                else:self.refused(self.req(2,'configure',sid,b'\0\1'),'unsupported',b'\0')
            elif kind=='TLV':
                self.configure(sid,extra=self.tlv(126,b'x'));self.refused(self.req(2,'configure',sid,b'\0\0'+self.tlv(254,b'x')),'unsupported',b'\xfe');self.refused(self.req(2,'configure',sid,b'\0\0'+self.tlv(126,b'x')+self.tlv(254,b'x')),'unsupported',b'\xfe');self.refused(self.arm_request(sid,extra=b'\x7e\3\0x'),'malformed');self.ok(self.arm_request(sid,extra=self.tlv(126,b'x')))
            elif kind in ('MODES','PARTIAL'):
                for mode in range(4):
                    for order in range(2 if self.lsb else 1):
                        self.configure(sid,mode,order);bits=self.bits(b'\x96\x35',order)
                        if kind=='PARTIAL':bits=bits[:11]
                        self.arm(sid,2,b'\x69\xca');result=self.transfer(bits,self.bits(b'\x69\xca',order)[:len(bits)]);require(self.rx(sid)[:3]==(0,len(bits),self.packed(bits,order)),'mode/partial RX packing');require(result['sampling_edges']==[1-(mode>>1) if mode&1==0 else mode>>1]*len(bits),'SPI CPOL/CPHA sampling edge');require(self.status()==(1,mode,order,0,0,1,0),'mode transfer counters')
            elif kind=='EMPTY-TX':
                self.arm(sid,2);self.transfer(self.bits(b'ab'),[0]*16);require(self.rx(sid)[:3]==(0,16,b'ab'),'empty TX receive');self.arm(sid,2,b'\x80');self.transfer(self.bits(b'cd'),self.bits(b'\x80')+[0]*8)
            elif kind=='UNARMED':
                self.transfer(self.bits(b'abc'),[0]*24);require(self.status()==(1,0,0,0,0,1,1),'unarmed transaction/error count');empty=self.rx(sid);require((empty[0],empty[2])==(0,b''),'unarmed MOSI retained')
            elif kind=='ZERO-BITS':
                self.transfer([],[]);require(self.status()==(1,0,0,0,0,0,0),'unarmed zero bits counted');self.arm(sid,1,b'x');self.transfer([],[]);require(self.status()==(1,0,0,1,0,0,0),'zero bits consumed arm');self.transfer(self.bits(b'y'),self.bits(b'x'));require(self.rx(sid)[:3]==(0,8,b'y'),'arm after zero bits unavailable');require(self.status()==(1,0,0,0,0,1,0),'nonzero transfer did not consume arm')
            elif kind=='TRUNCATE':
                self.arm(sid,1,b'x');self.transfer(self.bits(b'yz'),self.bits(b'x')+[0]*8);require(self.status()==(1,0,0,0,1,1,1),'length overflow counters');require(self.rx(sid)[:3]==(0,16,b'y'),'bits count must not truncate with data')
            elif kind in ('OVERFLOW','DOUBLE-ERROR'):
                for i in range(self.depth):self.arm(sid,1);self.transfer(self.bits(bytes((i,))),[0]*8)
                self.arm(sid,1);bits=self.bits(b'xy' if kind=='DOUBLE-ERROR' else b'x');self.transfer(bits,[0]*len(bits));require(self.status()==(1,0,0,0,self.depth,self.depth+1,1),'overflow must count transaction and one error')
                for i in range(self.depth):require(self.rx(sid)[:3]==(self.depth-i-1,8,bytes((i,))),'new overflow replaced old data')
                empty=self.rx(sid);require((empty[0],empty[2])==(0,b''),'overflow queued new frame')
            elif kind=='FIFO':
                for data in (b'a',b'bc',b'def'):self.arm(sid,len(data));self.transfer(self.bits(data),[0]*(8*len(data)))
                for i,data in enumerate((b'a',b'bc',b'def')):require(self.rx(sid)[:3]==(2-i,8*len(data),data),'RX FIFO/pending');require(self.status()[-2:]==(3,0),'read consumed cumulative counter')
            elif kind=='TIMESTAMP':
                for _ in range(2):
                    self.a.advance(1);self.arm(sid,1);result=self.transfer([1,0,1],[0]*3);pending,bits,data,_,v=self.rx(sid);require((pending,bits,data)==(0,3,b'\xa0'),'partial timestamp payload');timestamps=[value for tag,value in tlvs(v[7+len(data):]) if tag&127==self.ns]
                    if timestamps:require(struct.unpack('<Q',timestamps[0])[0]==result['ended_ns'],'CS inactive timestamp differs')
            elif kind=='ARM-LIMIT':
                self.refused(self.arm_request(sid,0),'malformed');self.refused(self.arm_request(sid,self.maximum+1),'unsupported',b'\0');self.refused(self.arm_request(sid,1,b'xx'),'malformed');self.arm(sid,self.maximum,b'x'*self.maximum);self.transfer(self.bits(b'y'*self.maximum),self.bits(b'x'*self.maximum))
            elif kind=='ARM-BUSY':
                self.arm(sid,1,b'a');self.refused(self.arm_request(sid,1,b'b'),'unavailable');self.transfer(self.bits(b'x'),self.bits(b'a'));self.arm(sid,1,b'b');self.transfer(self.bits(b'y'),self.bits(b'b'))
            elif kind=='ARM-READY':
                self.a.ready(False);before=self.a.state();req=self.arm_request(sid,1,b'x');reply=self.c.exchange(req);require(reply[3:5]==b'\1\1','not ready must complete failed');tlvs(reply[5:]);require(self.a.state()==before and self.status()[3]==0,'failed arm changed target');self.a.ready(True);before=self.a.state();require(self.c.replayed(req,reply,frame=True) and self.a.state()==before,'failed arm replay became ready/armed');self.arm(sid,1,b'x');self.transfer(self.bits(b'y'),self.bits(b'x'))
            elif kind=='RECONFIGURE':
                self.transfer([1],[0]);self.arm(sid,1);self.transfer(self.bits(b'xx'),[0]*16);self.arm(sid,1,b'x');self.configure(sid,2);require(self.status()==(1,2,0,0,0,0,0),'configure retained counters/queue/arm')
            elif kind=='MISO-GATE':
                self.arm(sid,1,b'\x80');require(self.a.state()['pins'][2]==0,'arm drove inactive MISO');self.a.cs(True);state=self.a.state();require(state['pins'][2]==4 and all(state['pins'][ch]==0 for ch in (0,1,3)),'CS active first bit/inputs');self.a.cs(False);require(self.a.state()['pins'][2]==0 and self.status()[3]==1,'CS end HiZ/zero bits arm')
            elif kind=='REPLAY-RX':
                for data in (b'a',b'b'):self.arm(sid,1);self.transfer(self.bits(data),[0]*8)
                _,_,_,req,v=self.rx(sid);before=self.a.state();require(self.c.replayed(req,v) and self.a.state()==before,'read_rx replay popped another frame');require(self.rx(sid)[:3]==(0,8,b'b'),'replay lost next RX')
            elif kind=='REPLAY-ARM':
                req=self.arm_request(sid,1,b'a');first=self.ok(req);self.transfer(self.bits(b'x'),self.bits(b'a'));before=self.a.state();require(self.c.replayed(req,first) and self.a.state()==before,'arm replay rearmed consumed TX');self.transfer(self.bits(b'y'),[0]*8);require(self.status()[-2:]==(2,1),'arm replay accepted next transfer')
            elif kind=='REPLAY-CONFIG':
                req=self.req(2,'configure',sid,b'\0\0');first=self.ok(req);self.configure(sid,3);self.arm(sid,1,b'a');before=self.a.state();require(self.c.replayed(req,first) and self.a.state()==before,'configure replay reset newer target')
            elif kind in ('RELEASE','END','EXPIRY','FORCE','ACTIVE-RELEASE','ACTIVE-END','ACTIVE-EXPIRY','ACTIVE-FORCE'):
                self.configure(sid,3);self.arm(sid,1);self.transfer(self.bits(b'xy'),[0]*16);self.arm(sid,1,b'z')
                if kind.startswith('ACTIVE-'):
                    self.a.cs(True);self.a.clock(0,1);self.a.clock(1,1);require(len(self.a.state()['bits'])==1 and self.a.state()['active'] and self.a.state()['pins'][2] in (3,4),'CS did not drive MISO before release');kind=kind.removeprefix('ACTIVE-')
                if kind=='RELEASE':self.release(sid)
                elif kind=='END':self.ok(self.c.request(self.c.core['end'],session=sid));self.c.session=None
                elif kind=='EXPIRY':self.a.advance(1000);require(struct.unpack_from('<BI',self.c.success(self.c.request(self.c.core['lock_state'])))==(0,0),'SPI expiry');self.c.session=None
                else:
                    old=self.arm_request(sid,1,b'q');replacement=secrets.randbelow(0xffffffff)+1
                    while replacement==sid:replacement=secrets.randbelow(0xffffffff)+1
                    self.c.corr=0;v=self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=replacement));require(struct.unpack_from('<II',v)==(1000,self.c.boot),'force response');tlvs(v[8:]);self.c.session=replacement
                self.closed();require(self.status()==(0,0,0,0,0,0,0),'plan close status')
                require(not self.a.state()['plans'],'closed plan retained')
                if kind=='FORCE':self.plan(replacement);self.configure(replacement);self.refused(old,'locked')
            elif kind=='SESSION':
                for name,payload in [('configure',b'\0\0'),('arm',b'\1\0\0\0'),('read_rx',b'')]:self.refused(self.req(2,name,0,payload),'session_required')
                self.status()
            elif kind=='COUNTS':
                for name,payload in [('configure',b'\0'),('arm',b'\1\0\2\0x'),('read_rx',b'x'),('status',b'x')]:self.refused(self.req(2,name,sid,payload),'malformed')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.selection=={1:'oep.probe.plan',2:'oep.fixture.spi-target'},'explicit SPI fixture');base=RegisteredOpsChecks(self.c,self.a.selection);require(base.run()['status']=='passed','standard SPI ops');self.codes={fn:{row['name']:row['code'] for row in base.specs[name]['op']} for fn,name in self.a.selection.items()};self.assignment=base.specs['oep.probe.plan']['tlv']['plan_apply']['role_assignment'];spec=base.specs['oep.fixture.spi-target'];self.ns=spec['tlv']['read_rx_answer']['ns'];require(spec['enum']['role']=={'sck':1,'mosi':2,'miso':3,'cs':4} and spec['enum']['state']=={'unconfigured':0,'running':1} and spec['enum']['features']['lsb_first']==1,'selected SPI roles/states/features differ');values=dict(reversed(self.c.describe(2)));common=self.c.reg['describe_common'];self.maximum=struct.unpack('<H',values[common['max_length']])[0];self.depth=values[spec['tlv']['describe']['queue_depth']][0];self.lsb=bool(struct.unpack('<I',values.get(common['features'],b'\0'*4))[0]&1);setup=values.get(spec['tlv']['describe']['cs_setup_ns'],b'\0'*4);require(len(setup)==4 and struct.unpack('<I',setup)[0]==0 and self.maximum==8 and self.depth==3 and self.a.state()['clock_scope']=='explicit-logical-ms','logical fixture geometry/CS setup/clock differs')
        self.c.check('OEP-SPI-CONTEXT','fixture §4; plan §2',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for kind in self.cases:
            first=len(self.a.trace);self.c.check('OEP-SPI-'+kind,'fixture §4; plan §2; core §6/8/9',lambda kind=kind:self.case(kind));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(row['status']=='passed' for row in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,scope='logical SPI bit order/CPOL/CPHA/CS gating, FIFO/arm and plan lifecycle',unchecked=['physical SPI waveform and worst-case cs_setup_ns timing','queue count 255, bits saturation and u32 cumulative counter limits','persisted config, other pin candidates/channel groups','concurrent controller/writer and deferred response','production Host integration'])
