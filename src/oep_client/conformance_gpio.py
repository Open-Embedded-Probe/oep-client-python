"""SPEC-driven selected logical GPIO set/read and plan lifecycle checks."""
import struct
from .conformance import require, tlvs
from .conformance_registered_ops import RegisteredOpsChecks


class GpioChecks:
    cases=('PLAN-IDLE','PLAN-REPLACE','PLAN-OTHER','PLAN-TRANSFER','PLAN-CAPACITY','PLAN-CONFLICT','PLAN-MALFORMED','PLAN-UNSUPPORTED','RELEASE-ONE','RELEASE-ALL','SET-ORDER','SET-MODES','SET-MALFORMED','SET-UNAVAILABLE','SET-UNSUPPORTED','TLV','READ','SESSION','REPLAY','END')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    @staticmethod
    def tlv(tag,v):return bytes((tag,))+struct.pack('<H',len(v))+v
    def request(self,fn,name,sid,payload=b''):return self.c.request(self.codes[fn][name],payload,fn=fn,session=sid)
    def apply_request(self,sid,rows,extra=b''):
        return self.request(1,'plan_apply',sid,b''.join(self.tlv(self.assignment,struct.pack('<HBH',*row)) for row in rows)+extra)
    def apply(self,sid,rows):tlvs(self.c.success(self.apply_request(sid,rows)))
    def release(self,sid,fns):tlvs(self.c.success(self.request(1,'plan_release',sid,bytes((len(fns),))+b''.join(struct.pack('<H',fn) for fn in fns))))
    def set_request(self,sid,rows,extra=b'',fn=2):return self.request(fn,'set',sid,bytes((len(rows),))+b''.join(struct.pack('<HB',*row) for row in rows)+extra)
    def set(self,sid,rows,extra=b'',fn=2):tlvs(self.c.success(self.set_request(sid,rows,extra,fn)))
    def read(self,channels,fn=2,sid=0):
        before=self.a.state();value=self.c.success(self.request(fn,'read',sid,bytes((len(channels),))+b''.join(struct.pack('<H',ch) for ch in channels)))
        require(len(value)>=1+len(channels) and value[0]==len(channels) and all(v in (0,1) for v in value[1:1+len(channels)]),'read count/levels');tlvs(value[1+len(channels):]);require(self.a.state()==before,'read changed GPIO/plan state');return list(value[1:1+len(channels)])
    def refused(self,request,reason,payload=None):
        before=self.a.state();value=self.c.rejected(request,reason)
        if payload is not None:require(value==payload,'refusal payload/tag/channel/index')
        require(self.a.state()==before,'refused request touched plan/pin journal')
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model' and boot['boot_id']!=self.c.boot,'fresh logical boot');self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'fresh boot readback')
    def case(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            if kind=='PLAN-IDLE':
                before=self.a.state();self.apply(sid,[(2,1,0),(2,1,1)]);after=self.a.state();require(after['plans']=={2:[0,1]} and after['pins']==before['pins'] and after['events']==before['events'],'plan changed idle before first set')
            elif kind=='PLAN-MALFORMED':
                for rows in ([(0,1,0)],[(2,1,0),(2,1,0)]):self.refused(self.apply_request(sid,rows),'malformed')
                for tail in (b'\x10',self.tlv(self.assignment,b'x')):self.refused(self.apply_request(sid,[],tail),'malformed')
            elif kind=='PLAN-UNSUPPORTED':
                self.refused(self.apply_request(sid,[(99,1,0)]),'unknown_function')
                for rows in ([(1,1,0)],[(2,2,0)],[(2,1,8)]):self.refused(self.apply_request(sid,rows),'unsupported',bytes((self.assignment,)))
            elif kind=='PLAN-CAPACITY':
                self.apply(sid,[(2,1,0)]);self.refused(self.apply_request(sid,[(2,1,ch) for ch in range(5)]),'unavailable',self.tlv(1,b'\2'))
            elif kind=='PLAN-CONFLICT':
                self.apply(sid,[(2,1,0)]);self.set(sid,[(0,3)]);self.refused(self.apply_request(sid,[(3,1,0)]),'unavailable',self.tlv(1,b'\1'))
            elif kind=='PLAN-REPLACE':
                self.apply(sid,[(2,1,0)]);self.set(sid,[(0,3)]);self.apply(sid,[(2,1,0),(2,1,1)]);s=self.a.state();require(s['plans']=={2:[0,1]} and s['pins'][0]==3,'a kept (fn, role, channel) was released (plan §2.1)')
                self.apply(sid,[(2,1,1)]);s=self.a.state();require(s['plans']=={2:[1]} and s['pins'][0]==s['idle'][0] and s['pins'][1]==s['idle'][1],'replacement did not restore old idle')
            elif kind=='PLAN-OTHER':
                self.apply(sid,[(2,1,0),(3,1,1)]);self.set(sid,[(1,4)],fn=3);before=self.a.state();self.apply(sid,[(2,1,2)]);s=self.a.state();require(s['plans']=={3:[1],2:[2]} and s['pins'][1]==before['pins'][1],'replacement touched other fn')
            elif kind=='PLAN-TRANSFER':
                self.apply(sid,[(2,1,0),(3,1,1)]);self.apply(sid,[(2,1,1),(3,1,0)]);require(self.a.state()['plans']=={2:[1],3:[0]},'atomic replacement did not remove old assignments before conflict check')
            else:
                self.apply(sid,[(2,1,0),(2,1,1),(3,1,2)])
                if kind=='RELEASE-ONE':
                    self.set(sid,[(0,3)]);self.set(sid,[(2,4)],fn=3);self.refused(self.request(1,'plan_release',sid,b'\2\2\0\x63\0'),'unknown_function')   # every fn checked first (plan §2.2)
                    before=self.a.state();self.release(sid,[2]);s=self.a.state();require(s['plans']=={3:[2]} and s['pins'][0]==s['idle'][0] and s['pins'][2]==before['pins'][2],'selective release changed other fn')
                elif kind=='RELEASE-ALL':
                    self.set(sid,[(0,3),(1,4)]);self.release(sid,[]);s=self.a.state();require(not s['plans'] and s['pins']==s['idle'],'release all did not restore idle');self.release(sid,[])
                elif kind=='SET-ORDER':
                    before=len(self.a.state()['events']);self.set(sid,[(0,3),(0,4)]);events=self.a.state()['events'][before:];require([(r[0],r[1],r[2]) for r in events]==[('set',0,3),('set',0,4)],'set order/duplicate-channel sequence lost');require(self.read([0])==[1],'final high')
                elif kind=='SET-MODES':
                    for mode in range(7):
                        self.set(sid,[(0,mode)]);require(self.a.state()['pins'][0]==mode,'mode not applied')
                        if mode in (1,2,3,4,5):require(self.read([0])==[int(mode in (1,4))],'mode level semantics')
                    self.set(sid,[]);require(self.read([])==[],'empty GPIO sequence')
                elif kind=='SET-MALFORMED':
                    for payload in (b'',b'\2\0\0\4',b'\1\0\0'):
                        self.refused(self.request(2,'set',sid,payload),'malformed')
                    self.refused(self.request(1,'plan_release',sid,b'\2\2\0'),'malformed')
                elif kind=='SET-UNAVAILABLE':
                    meta=self.tlv(2,struct.pack('<H',7))+self.tlv(self.index,b'\1');self.refused(self.set_request(sid,[(0,3),(7,4)]),'unavailable',meta)
                    self.refused(self.request(2,'read',0,b'\2\0\0\7\0'),'unavailable',meta)
                elif kind=='SET-UNSUPPORTED':
                    meta=self.tlv(2,struct.pack('<H',1))+self.tlv(self.index,b'\1');self.refused(self.set_request(sid,[(0,3),(1,7)]),'unsupported',b'\0'+meta)
                elif kind=='TLV':
                    self.set(sid,[(0,3)],self.tlv(126,b'x'));self.refused(self.set_request(sid,[(0,4)],self.tlv(254,b'x')),'unsupported',b'\xfe');self.refused(self.set_request(sid,[(0,4)],b'\x7e\3\0x'),'malformed')
                elif kind=='READ':
                    self.set(sid,[(0,3),(1,4)]);require(self.read([1,0,1])==[1,0,1],'read must retain requested order/duplicates')
                    self.refused(self.request(2,'read',0,b'\2\0\0'),'malformed')
                elif kind=='SESSION':
                    self.refused(self.set_request(0,[(0,3)]),'session_required');self.refused(self.apply_request(0,[(2,1,0)]),'session_required');self.read([0],sid=0)
                elif kind=='REPLAY':
                    req=self.set_request(sid,[(0,3),(0,4)]);self.c.success(req);before=self.a.state();self.c.success(req);require(self.a.state()==before,'set replay repeated pin writes')
                    req=self.apply_request(sid,[(2,1,1)]);self.c.success(req);before=self.a.state();self.c.success(req);require(self.a.state()==before,'plan replay re-applied pins')
                elif kind=='END':
                    self.set(sid,[(0,3),(1,4)]);self.c.success(self.c.request(self.c.core['end'],session=sid));self.c.session=None;s=self.a.state();require(not s['plans'] and s['pins']==s['idle'],'end did not release plans/restore idle')
                else:raise ValueError(kind)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.selection=={1:'oep.probe.plan',2:'oep.fixture.gpio',3:'oep.fixture.gpio'},'explicit logical fixture selection required')
            base=RegisteredOpsChecks(self.c,self.a.selection);report=base.run();require(report['status']=='passed','registered op context failed');self.codes={fn:{r['name']:r['code'] for r in base.specs[name]['op']} for fn,name in self.a.selection.items()}
            plan=base.specs['oep.probe.plan'];gpio=base.specs['oep.fixture.gpio'];self.assignment=plan['tlv']['plan_apply']['role_assignment'];self.index=gpio['tlv']['unavailable_payload']['index']
            require(self.c.max_frame>=64,'fixture frame budget')
        self.c.check('OEP-GPIO-CONTEXT','fixture §1; plan §2',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for kind in self.cases:self.c.check('OEP-GPIO-'+kind,'fixture §1; plan §2; core §8/9',lambda kind=kind:self.case(kind))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,scope='explicit logical GPIO/plan fixture set/read, admission, sequencing, replay and release',unchecked=['physical levels/pull/drive current and pin waveforms','other fixture plan role constraints and channel_group','persisted config idle/disable and production Host','force/lease expiry with these GPIO plans','other standard operation behaviors'])
