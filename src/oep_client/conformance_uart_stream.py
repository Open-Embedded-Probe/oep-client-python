"""Common positioned-stream checks applied to an explicitly plan-owned UART."""
import struct
from .conformance import require, tlvs, ops
from .conformance_stream import StreamChecks


class UartStreamChecks(StreamChecks):
    def __init__(self,checks,adapter):super().__init__(checks,adapter);self.guard=None
    def fresh(self,position=0,serial=0):
        super().fresh(position,serial);self.guard=self.c.holding(3000);sid=self.guard.__enter__()
        payload=b''.join(bytes((self.assignment,))+struct.pack('<H',5)+struct.pack('<HBH',2,role,role-1) for role in (1,2))
        tlvs(self.c.success(self.c.request(self.plan_op,payload,session=sid,fn=1)))
        tlvs(self.c.success(self.c.request(self.configure_op,struct.pack('<I',115200),session=sid,fn=2))[4:])   # reception starts here (fixture §2)
    def clear(self):
        self.fresh();self.a.feed_bytes(b'abc');request=self.request('clear',sid=self.c.session);frame=self.c.exchange(request);require(frame[3:5]==b'\1\0','clear success');tlvs(frame[5:]);after=self.a.stream_state();require(after['position']==3 and not after['data'],'clear reset byte position');require(self.c.exchange(request)==frame and self.a.stream_state()==after,'clear replay duplicated mark');self.expect_read((3,2,b''));more,rows=self.marks(0);require(not more and len(rows)==1 and rows[0][1:3]==(3,2),'clear mark')
    def write(self):
        self.fresh()
        for data,accepted,outcome in ((b'abc',3,0),(b'de',1,2),(b'x',0,1),(b'',0,0)):
            before=bytes(self.a.stream_state()['tx']);req=self.request('write',struct.pack('<H',len(data))+data,self.c.session);frame=self.c.exchange(req);require(len(frame)>=7 and frame[3:5]==bytes((1,outcome)) and struct.unpack_from('<H',frame,5)[0]==accepted,'common write accepted/outcome');tlvs(frame[7:]);after=self.a.stream_state();require(after['tx']==before+data[:accepted],'common write accepted prefix');require(self.c.exchange(req)==frame and self.a.stream_state()==after,'common write replay duplicated queue')
    def run(self):
        start=len(self.c.results)
        def context():
            a=self.a.adoption;require(a['component']=='positioned-stream-v1' and a['addressing']=='persistent-fn' and a['fn']==2 and a['reference'] and a['byte_capacity']==64 and a['mark_capacity']==5 and a['tx_capacity']==4 and self.a.reset_scope=='logical-model','explicit plan-owned UART adoption/geometry required')
            specs={s['name']:s for s in self.c.reg['interface']};uart=specs['oep.fixture.uart'];plan=specs['oep.probe.plan'];require(uart['revision']==plan['revision']==1,'selected UART/plan revision differs');codes={r['name']:r['code'] for r in uart['op']};require(a['ops']=={n:codes[n] for n in ('read','marks','clear','mark','write')},'adopted common ops differ from registry');self.plan_op=next(r['code'] for r in plan['op'] if r['name']=='plan_apply');self.configure_op=codes['configure'];self.assignment=plan['tlv']['plan_apply']['role_assignment']
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot();require(any(r['fn']==2 and r['name']=='oep.fixture.uart' and r['revision']==1 for r in entries),'selected UART absent');require(set(a['ops'].values())<=ops(dict(reversed(self.c.describe(2)))[self.c.reg['describe_common']['ops']]),'adopted stream ops missing');self.c.observed['uart_stream_adoption']=dict(a)
        self.c.check('IF-UART-STREAM-CONTEXT','common §1; explicit plan-owned adoption',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('EMPTY',self.empty),('POSITION',self.position),('FRAME',self.frame),('GAP',self.gap),('SELECTORS',self.selectors),('PAGES',self.pages),('WRAP',lambda:self.pages(0xfffffffe)),('EVICTION',self.lost_marks),('MARK-EVICTED',self.mark_evicted),('MARK-GAP',self.mark_gap),('LIMIT',self.limit),('CLEAR',self.clear),('REFUSALS',self.locks),('WRITE',self.write)]
        for name,case in cases:
            def owned_case(case=case):
                try:case()
                finally:
                    if self.guard is not None:
                        guard,self.guard=self.guard,None;guard.__exit__(None,None,None)
            first=len(self.a.trace);self.c.check('IF-UART-STREAM-'+name,'common §1; core §2.6/5.2; explicit plan lifecycle',owned_case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,scope='common positioned-stream read/marks/write for explicitly adopted plan-owned UART',unchecked=['UART configure/baud/TX idle and plan recreation: separate OEP checks','physical UART delivery and producer concurrency','notification data and other ring/frame geometries'])
