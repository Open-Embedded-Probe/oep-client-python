"""SPEC-first explicit I2C target operations and plan lifecycle checks."""
import secrets
import struct
from .conformance import require, tlvs, ops
from .conformance_registered_ops import RegisteredOpsChecks


class I2cChecks:
    cases=('STATE0','PLAN-SHAPE','PLAN-TAGS','RUNNING-PLAN','CONFIGURE','CONFIGURE-FAIL','ADDRESS','MISMATCH','TLV','WRITE','EMPTY','TRUNCATE','OVERFLOW','DOUBLE-ERROR','READ-FIFO','PRELOAD','PRELOAD-LENGTH','SLOTS','READ-LENGTH','RECONFIGURE','STRETCH','PULLUPS','REPLAY-RX','REPLAY-TX','REPLAY-CONFIG','RELEASE','END','EXPIRY','FORCE','SESSION','COUNTS')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    @staticmethod
    def tlv(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value
    def req(self,fn,name,sid=0,payload=b''):return self.c.request(self.codes[fn][name],payload,fn=fn,session=sid)
    def ok(self,req):value=self.c.success(req);tlvs(value);return value
    def plan_request(self,sid,roles=((1,0),(2,1))):return self.req(1,'plan_apply',sid,b''.join(self.tlv(self.assignment,struct.pack('<HBH',2,r,ch)) for r,ch in roles))
    def plan(self,sid):self.ok(self.plan_request(sid))
    def release(self,sid):self.ok(self.req(1,'plan_release',sid,b'\1\2\0'))
    def configure(self,sid,address=0x42,extra=b''):self.ok(self.req(2,'configure',sid,bytes((address,))+extra))
    def preload_request(self,sid,data):return self.req(2,'preload_tx',sid,struct.pack('<H',len(data))+data)
    def preload(self,sid,data):self.ok(self.preload_request(sid,data))
    def status(self):
        before=self.a.state();v=self.c.success(self.req(2,'status'));require(len(v)>=11,'I2C status width');result=struct.unpack_from('<BBIBI',v);tlvs(v[11:]);require(self.a.state()==before,'status mutated target');return result
    def rx(self,sid):
        req=self.req(2,'read_rx',sid);v=self.c.success(req);require(len(v)>=3,'read_rx header');pending,count=struct.unpack_from('<BH',v);require(count<=self.maximum and len(v)>=3+count,'read_rx frame bound');tags=tlvs(v[3+count:]);require(all(len(value)==8 for tag,value in tags if tag&127==self.ns),'read_rx timestamp width');return pending,v[3:3+count],req,v
    def refused(self,req,reason,payload=None):
        before=self.a.state();value=self.c.rejected(req,reason)
        if payload is not None:require(value==payload,'I2C refusal raw tag/cause')
        require(self.a.state()==before,'refused request mutated target/plan/pins')
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'explicit new logical boot');self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'boot identity')
    def idle(self):
        state=self.a.state();require(not state['configured'] and not state['rx'] and not state['tx'] and state['rx_frames']==0 and state['errors']==0 and state['stretch_us']==0 and state['pins']==state['idle'],'plan close retained target state/queues/counters/pins')
    def written(self,data,address=0x42,end='stop'):
        result=self.a.write(address,data,end);require(result['ack'] and result['acked_bytes']==len(data),'configured address/data ACK');return result
    def read_bus(self,count,address=0x42):
        result=self.a.read(address,count);require(result['ack'] and len(result['data'])==count,'controller read ACK/length');return result['data']
    def case(self,kind):
        self.fresh()
        with self.c.holding(1000) as sid:
            if kind=='STATE0':
                require(self.status()==(0,0,0,0,0),'initial status');self.refused(self.req(2,'configure',sid,b'B'),'unavailable',self.tlv(1,b'\6'))
                self.plan(sid);require(self.status()==(0,0,0,0,0),'plan configured target early');state=self.a.state();require(state['pins']==state['idle'],'plan changed pins before configure');require(not self.a.write(0x42,b'x','stop')['ack'] and not self.a.read(0x42,1)['ack'],'state0 ACKed address');self.refused(self.req(2,'read_rx',sid),'unavailable',self.tlv(1,b'\6'));self.refused(self.preload_request(sid,b'x'),'unavailable',self.tlv(1,b'\6'));return
            self.plan(sid)
            if kind!='PLAN-SHAPE':self.configure(sid)
            if kind=='PLAN-SHAPE':                                  # state 0: only the form of the plan applies
                for roles in (((1,0),),((2,1),),((1,0),(1,1),(2,1)),((1,0),(2,0))):self.refused(self.plan_request(sid,roles),'unsupported',bytes((self.assignment,)))
                self.refused(self.plan_request(sid,((1,0),(2,1),(1,0))),'malformed')   # the same (fn, role, channel) twice
            elif kind=='RUNNING-PLAN':
                self.written(b'keep');self.preload(sid,b'tx');before=self.a.state();self.ok(self.plan_request(sid));require(self.a.state()==before,'the same set changed a running target')
                self.refused(self.plan_request(sid,((1,1),(2,0))),'unavailable',self.tlv(1,b'\6'))
            elif kind=='CONFIGURE-FAIL':
                self.written(b'keep');self.preload(sid,b'tx');self.a.fail_configure();before=self.a.state()
                reply=self.c.exchange(self.req(2,'configure',sid,b'B'));require(reply[3:5]==b'\1\1','failed configure must be completed failed');tlvs(reply[5:])
                require(self.status()==(0,0,0,0,0),'failed configure left state, queue, slots or counters');require(self.a.state()['stretch_us']==before['stretch_us'],'failed configure lost stretch')
                self.refused(self.req(2,'read_rx',sid),'unavailable',self.tlv(1,b'\6'));self.configure(sid);require(self.status()[0]==1,'new configure did not recover')
            elif kind=='PLAN-TAGS':
                self.refused(self.plan_request(sid,((3,0),(2,1))),'unsupported',bytes((self.assignment,)));self.refused(self.req(1,'plan_apply',sid,self.tlv(self.assignment,b'\0'*4)),'malformed')
            elif kind=='CONFIGURE':
                self.configure(sid,8);require(self.status()==(1,0,0,0,0),'configure status');require(not self.a.write(0x42,b'x','stop')['ack'] and self.a.write(8,b'x','stop')['ack'],'address selection')
            elif kind=='ADDRESS':
                for address in range(128,256):self.refused(self.req(2,'configure',sid,bytes((address,))),'malformed')
                for address in (*range(8),*range(120,128)):self.refused(self.req(2,'configure',sid,bytes((address,))),'unsupported',b'\0')
                for address in (8,119):self.configure(sid,address);require(self.a.write(address,b'x','stop')['ack'],'legal boundary address')
            elif kind=='MISMATCH':
                self.written(b'keep');self.preload(sid,b'tx');before=self.a.state()
                require(not self.a.write(0x43,b'bad','stop')['ack'] and not self.a.read(0x43,2)['ack'],'wrong address ACK')
                after=self.a.state();require(all(after[k]==before[k] for k in ('rx','tx','rx_frames','errors','stretch_us','pins','configured','address')),'wrong address consumed target state')
                require(self.read_bus(2)==b'tx' and self.rx(sid)[:2]==(0,b'keep'),'wrong address corrupted queues')
            elif kind=='TLV':
                self.configure(sid,extra=self.tlv(126,b'x'));self.refused(self.req(2,'configure',sid,b'B'+self.tlv(254,b'x')),'unsupported',b'\xfe');self.refused(self.req(2,'configure',sid,b'B\x7e\3\0x'),'malformed')
            elif kind=='WRITE':
                result=self.written(b'abc');require(self.status()==(1,1,1,0,0),'one frame count');pending,data,_,v=self.rx(sid);require((pending,data)==(0,b'abc'),'write data');timestamps=[value for tag,value in tlvs(v[3+len(data):]) if tag&127==self.ns]
                if timestamps:require(struct.unpack('<Q',timestamps[0])[0]==result['ended_ns'],'STOP timestamp')
                self.a.advance(1);result=self.written(b'd',end='start');pending,data,_,v=self.rx(sid);require((pending,data)==(0,b'd'),'next START frame boundary');timestamps=[value for tag,value in tlvs(v[3+len(data):]) if tag&127==self.ns]
                if timestamps:require(struct.unpack('<Q',timestamps[0])[0]==result['ended_ns'],'next START timestamp')
            elif kind=='EMPTY':
                self.written(b'');require(self.status()==(1,0,0,0,0),'address-only write counted');require(self.rx(sid)[:2]==(0,b''),'empty RX response')
            elif kind=='TRUNCATE':
                data=bytes(range(self.maximum+2));self.written(data);require(self.status()==(1,1,1,0,1),'length overflow counters');require(self.rx(sid)[:2]==(0,data[:self.maximum]),'truncate prefix')
            elif kind in ('OVERFLOW','DOUBLE-ERROR'):
                for i in range(self.depth):self.written(bytes((i,)))
                self.written(b'overflow-too-long' if kind=='DOUBLE-ERROR' else b'x');require(self.status()==(1,self.depth,self.depth,0,1),'queue overflow errors must increase once and rx_frames must not count dropped frame')
                for i in range(self.depth):require(self.rx(sid)[:2]==(self.depth-i-1,bytes((i,))),'queue overflow replaced old frame')
                require(self.rx(sid)[:2]==(0,b''),'dropped new frame remained')
            elif kind=='READ-FIFO':
                for data in (b'a',b'bc',b'def'):self.written(data)
                for i,data in enumerate((b'a',b'bc',b'def')):require(self.rx(sid)[:2]==(2-i,data),'RX FIFO/pending');require(self.status()[2]==3,'read consumed cumulative frame count')
            elif kind=='PRELOAD':
                self.preload(sid,b'ab');self.preload(sid,b'cd');require(self.status()==(1,0,0,2,0),'TX slot count');require(self.read_bus(2)==b'ab' and self.read_bus(2)==b'cd' and self.read_bus(3)==b'\xff'*3,'preload FIFO/empty fill');require(self.status()==(1,0,0,0,0),'read changed RX counters')
            elif kind=='PRELOAD-LENGTH':
                self.refused(self.preload_request(sid,b''),'malformed');self.refused(self.preload_request(sid,b'x'*(self.maximum+1)),'unsupported',b'\0');self.preload(sid,b'x'*self.maximum);require(self.read_bus(self.maximum)==b'x'*self.maximum,'max preload')
            elif kind=='SLOTS':
                for i in range(self.depth):self.preload(sid,bytes((i,)))
                self.refused(self.preload_request(sid,b'x'),'unavailable',self.tlv(1,b'\2'));require(self.status()[3]==self.depth,'full slot count')
                for i in range(self.depth):require(self.read_bus(1)==bytes((i,)),'full TX FIFO')
                self.preload(sid,b'y');require(self.read_bus(1)==b'y','freed slot unavailable')
            elif kind=='READ-LENGTH':
                self.preload(sid,b'abc');self.preload(sid,b'd');require(self.read_bus(1)==b'a' and self.read_bus(3)==b'd\xff\xff','short read reused slot or long read missing fill');self.preload(sid,b'ef');self.preload(sid,b'g');require(self.read_bus(4)==b'ef\xff\xff' and self.read_bus(1)==b'g','long read consumed next slot')
            elif kind=='RECONFIGURE':
                self.written(b'x'*(self.maximum+1));self.preload(sid,b'tx');self.configure(sid,0x43);require(self.status()==(1,0,0,0,0),'configure retained queue/counters');require(self.read_bus(2,0x43)==b'\xff'*2,'configure retained TX')
            elif kind=='STRETCH':
                if not self.stretch_available:self.refused(self.req(2,'stretch',sid,struct.pack('<I',1)),'unknown_operation');return
                self.release(sid);self.plan(sid);self.ok(self.req(2,'stretch',sid,struct.pack('<I',17)));require(self.a.state()['stretch_us']==17,'state0 stretch rejected');self.ok(self.req(2,'stretch',sid,struct.pack('<I',self.max_stretch)));self.configure(sid);require(self.a.state()['stretch_us']==self.max_stretch,'configure cleared stretch');result=self.written(b'ab');require(result['stretches']==[self.max_stretch]*2,'write stretch must apply to data bytes only');result=self.a.read(0x42,3);require(result['stretches']==[self.max_stretch],'read stretch must follow address');self.refused(self.req(2,'stretch',sid,struct.pack('<I',self.max_stretch+1)),'unsupported',b'\0');self.ok(self.req(2,'stretch',sid,struct.pack('<I',0)));require(self.written(b'a')['stretches']==[],'zero stretch active')
            elif kind=='PULLUPS':
                state=self.a.state();require(state['pullups']==self.internal_pullups and all(state['pins'][ch] in (1,6) for ch in (0,1)),'internal pullups or open-drain released pins differ from declaration');self.written(b'xy');require(all(self.a.state()['pins'][ch] not in (3,4) for ch in (0,1)),'I2C pushed output high')
            elif kind=='REPLAY-RX':
                self.written(b'a');self.written(b'b');_,_,req,payload=self.rx(sid);before=self.a.state();require(self.c.success(req)==payload and self.a.state()==before,'read_rx replay consumed another frame');require(self.rx(sid)[:2]==(0,b'b'),'second RX lost')
            elif kind=='REPLAY-TX':
                req=self.preload_request(sid,b'a');self.ok(req);self.read_bus(1);before=self.a.state();self.ok(req);require(self.a.state()==before and self.read_bus(1)==b'\xff','preload replay duplicated TX slot')
            elif kind=='REPLAY-CONFIG':
                req=self.req(2,'configure',sid,b'B');self.ok(req);self.configure(sid,0x43);self.written(b'x',0x43);before=self.a.state();self.ok(req);require(self.a.state()==before,'configure replay reset current target')
            elif kind in ('RELEASE','END','EXPIRY','FORCE'):
                self.written(b'x'*(self.maximum+1));self.preload(sid,b'tx')
                if self.stretch_available:self.ok(self.req(2,'stretch',sid,struct.pack('<I',7)))
                if kind=='RELEASE':self.release(sid)
                elif kind=='END':self.ok(self.c.request(self.c.core['end'],session=sid));self.c.session=None
                elif kind=='EXPIRY':self.a.advance(1000);require(struct.unpack_from('<BI',self.c.success(self.c.request(self.c.core['lock_state'])))==(0,0),'expiry did not end owner');self.c.session=None
                else:
                    old=self.preload_request(sid,b'z');replacement=secrets.randbelow(0xffffffff)+1
                    while replacement==sid:replacement=secrets.randbelow(0xffffffff)+1
                    self.c.corr=0;v=self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=replacement));require(struct.unpack_from('<II',v)==(1000,self.c.boot),'force response');tlvs(v[8:]);self.c.session=replacement
                self.idle();require(self.status()==(0,0,0,0,0),'close status not initial')
                require(not self.a.state()['plans'],'release retained plan')
                if kind=='FORCE':self.plan(replacement);self.configure(replacement);self.refused(old,'locked')
            elif kind=='SESSION':
                for name,payload in [('configure',b'B'),('read_rx',b''),('preload_tx',b'\1\0x')]:self.refused(self.req(2,name,0,payload),'session_required')
                if self.stretch_available:self.refused(self.req(2,'stretch',0,b'\0'*4),'session_required')
                self.status()
            elif kind=='COUNTS':
                for name,payload in [('configure',b''),('preload_tx',b'\2\0x'),('read_rx',b'x'),('status',b'x'),('stretch',b'\0')]:self.refused(self.req(2,name,sid,payload),'malformed' if name!='stretch' or self.stretch_available else 'unknown_operation')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.selection=={1:'oep.probe.plan',2:'oep.fixture.i2c-target'},'explicit I2C fixture');base=RegisteredOpsChecks(self.c,self.a.selection);require(base.run()['status']=='passed','registered I2C ops');self.codes={fn:{r['name']:r['code'] for r in base.specs[name]['op']} for fn,name in self.a.selection.items()};self.assignment=base.specs['oep.probe.plan']['tlv']['plan_apply']['role_assignment'];spec=base.specs['oep.fixture.i2c-target'];self.ns=spec['tlv']['read_rx_answer']['ns'];require(spec['enum']['role']=={'sda':1,'scl':2} and spec['enum']['state']=={'unconfigured':0,'running':1} and spec['enum']['features']['internal_pullups']==4,'selected I2C roles/states/features differ');values=dict(reversed(self.c.describe(2)));common=self.c.reg['describe_common'];self.maximum=struct.unpack('<H',values[common['max_length']])[0];self.depth=values[spec['tlv']['describe']['queue_depth']][0];self.internal_pullups=bool(struct.unpack('<I',values[common['features']])[0]&4);self.stretch_available=self.codes[2]['stretch'] in ops(values[common['ops']]);self.max_stretch=struct.unpack('<I',values[spec['tlv']['describe']['max_stretch_us']])[0] if self.stretch_available else 0
            require(self.maximum==8 and self.depth==3 and self.a.state()['clock_scope']=='explicit-logical-ms','logical fixture geometry/clock')
        self.c.check('OEP-I2C-CONTEXT','fixture §3; plan §2',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for kind in self.cases:
            first=len(self.a.trace);self.c.check('OEP-I2C-'+kind,'fixture §3; plan §2; core §6/8/9',lambda kind=kind:self.case(kind));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,scope='explicit logical I2C target transactions/queues/stretch and plan lifecycle',unchecked=['physical I2C START/STOP/ACK/clock/stretch timing and open-drain waveform','persisted config and other pin candidates/channel groups','queue geometries reaching 255 and u32 cumulative counter limits','concurrent writers/controllers and deferred response gating','production Host integration'])
