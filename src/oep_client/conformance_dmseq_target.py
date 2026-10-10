"""Independent target-side dmseq conformance, including explicit logical-time bounds."""
import struct
from .conformance import require, tlvs
from .conformance_dmseq import crc_reference, reply_word, DmseqChecks


class TargetDmseqChecks:
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    @staticmethod
    def frame(s,a,syn,to,data):
        prefix=bytes((128|(s<<5)|(a<<4)|(int(syn)<<3)|(int(to)<<6)|len(data),))+data
        raw=(prefix+bytes((crc_reference(prefix),))).ljust(8,b'\0')
        return int.from_bytes(raw[:4],'little'),int.from_bytes(raw[4:],'little')
    def emitted(self,s,a,syn,to,data,read=None):
        state=self.a.state();d0,d1=self.frame(s,a,syn,to,data)
        expected=[] if read is None else [('read',4,read)]
        if len(data)>=3:expected.append(('write',5,d1))
        expected.append(('write',4,d0))
        require(state['events']==expected and state['data0']==d0,'target frame order/CRC/flags')
        if len(data)>=3:require(state['data1']==d1,'target DATA1 bytes')
        require((state['s'],state['a'],state['syn'],state['to'],state['payload'])==(s,a,syn,to,data),'target framing state')
        return state
    def unchanged(self,advance,word=None):
        if word is not None:self.a.host_word(word)
        before=self.a.state();self.a.service(advance);after=self.a.state()
        require(after['events']==[('read',4,before['data0'])],'target wrote before finite wait elapsed')
        for key in ('s','a','syn','to','payload','input','output','deadline','data0','data1'):
            require(after[key]==before[key],'target changed state without ack/timeout')
    def begin_lengths(self):
        for n in range(7):
            data=bytes(range(n));self.a.begin(data=data);self.emitted(0,1,True,False,data)
    def seeds(self):
        for s,a in ((0,0),(0,1),(1,0),(1,1)):
            self.a.begin(s,a,b'x');self.emitted(s,a,True,False,b'x')
            word=reply_word(s,1-a,b'y');self.a.host_word(word);self.a.service()
            self.emitted(1-s,1-a,False,False,b'',word);require(self.a.state()['input']==b'y','target input/initial H')
    def invalid(self,kind):
        self.a.begin(data=b'abc');original=self.a.state()
        if kind=='FOREIGN':words=[0xffffffff,original['data0']^256]
        elif kind=='M':words=[m for m in range(3,8)]
        elif kind=='K':words=[reply_word(1,0,b'x')]
        else:
            words=[]
            for n in range(3):
                word=reply_word(0,0,b'xy'[:n])
                words += [word^(1<<(8*(n+1)+bit)) for bit in range(8)]
        for word in words:
            self.a.host_word(word);self.a.service();state=self.emitted(0,1,True,False,b'abc',word)
            require(state['input']==b'' and state['deadline']==original['deadline'],'invalid answer accepted or renewed wait')
    def zero(self,long=False):
        self.a.begin(data=b'x')
        if long:
            word=reply_word(0,0);self.a.host_word(word);self.a.service();self.emitted(1,1,False,False,b'',word)
        self.unchanged(19,0);self.a.host_word(0);self.a.service(1)
        self.emitted(1 if long else 0,1,not long,not long,b'' if long else b'x',0)
        if long:
            self.unchanged(979);self.a.host_word(0);self.a.service(1);self.emitted(1,1,False,True,b'',0)
    def timeout(self,long=False):
        self.a.begin(data=b'abc')
        if long:
            self.a.write_output(b'xyz');word=reply_word(0,0);self.a.host_word(word);self.a.service();self.emitted(1,1,False,False,b'xyz',word)
        before=self.a.state();wait=1000 if long else 20;self.unchanged(wait-1)
        self.a.service(1);self.emitted(int(long),1,not long,True,b'xyz' if long else b'abc',before['data0'])
        self.unchanged(19);own=self.a.state()['data0'];self.a.service(1);self.emitted(int(long),1,not long,True,b'xyz' if long else b'abc',own)
    def timed_out_write(self):
        self.a.begin(data=b'x');self.a.service(20);self.emitted(0,1,True,True,b'x',self.frame(0,1,True,False,b'x')[0])
        before=self.a.state();require(self.a.write_output(b'after-timeout')==0,'timed-out output retained')
        require(self.a.state()['output']==before['output'] and self.a.state()['payload']==b'x','timeout overwrote held frame')
        word=reply_word(0,0,b'q');self.a.host_word(word);self.a.service();self.emitted(1,0,False,False,b'',word)
        require(self.a.state()['input']==b'q' and self.a.write_output(b'next')==4,'late attach did not recover output')
        word=reply_word(1,1);self.a.host_word(word);self.a.service();self.emitted(0,0,False,False,b'next',word)
        self.unchanged(999);own=self.a.state()['data0'];self.a.service(1);self.emitted(0,0,False,True,b'next',own)
    def duplicate_input(self):
        self.a.begin();word=reply_word(0,0,b'ab');self.a.host_word(word);self.a.service();self.emitted(1,0,False,False,b'',word)
        require(self.a.read_input(2)==b'ab','first target input')
        word=reply_word(1,0,b'ab');self.a.host_word(word);self.a.service();self.emitted(0,0,False,False,b'',word)
        require(self.a.state()['input']==b'','duplicate H delivered twice')
        word=reply_word(0,1,b'ab');self.a.host_word(word);self.a.service();self.emitted(1,1,False,False,b'',word)
        require(self.a.read_input(2)==b'ab','new H with identical bytes was lost')
    def backpressure(self):
        self.a.begin();word=reply_word(0,0,b'ab');self.a.host_word(word);self.a.service();self.emitted(1,0,False,False,b'',word)
        word=reply_word(1,1,b'cd');self.a.host_word(word);self.a.service();self.emitted(0,0,False,False,b'',word)
        require(self.a.state()['input']==b'ab','full input overwritten')
        require(self.a.read_input(1)==b'a','input drain prefix')
        word=reply_word(0,1,b'cd');self.a.host_word(word);self.a.service();self.emitted(1,0,False,False,b'',word)
        require(self.a.read_input(1)==b'b','insufficient input space consumed partial frame')
        word=reply_word(1,1,b'cd');self.a.host_word(word);self.a.service();self.emitted(0,1,False,False,b'',word)
        require(self.a.read_input(2)==b'cd','input retry after space recovery')
    def empty_ack(self):
        self.a.begin();word=reply_word(0,0);self.a.host_word(word);self.a.service();self.emitted(1,1,False,False,b'',word)
        require(self.a.state()['input']==b'','empty M changed A or delivered data')
    def output_order(self):
        self.a.begin(data=b'first');require(self.a.write_output(b'1234567')==6,'explicit output queue capacity')
        word=reply_word(0,0);self.a.host_word(word);self.a.service();self.emitted(1,1,False,False,b'123456',word)
        require(self.a.state()['output']==b'','queued output not moved as prefix')
    def foreign_after_timeout(self):
        self.a.begin(data=b'x');self.a.service(20);deadline=self.a.state()['deadline']
        self.a.host_word(0xffffffff);self.a.service();state=self.emitted(0,1,True,True,b'x',0xffffffff)
        require(state['deadline']==deadline,'foreign word reset timeout wait')
    def linked(self):
        probe=DmseqChecks(self.c,self.a.probe)
        probe.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in self.a.probe.selection.items()}
        probe.operations={fn:{op['name']:op['code'] for op in spec['op']} for fn,spec in probe.specs.items()}
        probe.fresh()
        with self.c.holding(3000) as sid:
            rid=probe.opened(sid,2);probe.write(sid,rid,b'abcd',0,4);self.a.begin(data=b'x')
            joint=self.a.link_poll(rid)
            require(joint['target']['input']==b'ab' and joint['target']['a']==0 and joint['target']['s']==1,'linked first target input/ack')
            require(joint['probe']['streams'][rid]['data']==b'x' and joint['probe']['streams'][rid]['tx']==b'cd' and joint['probe']['protocols'][rid]['pending']==b'ab','linked first probe output/pending')
            joint=self.a.link_poll(rid)
            require(joint['target']['input']==b'ab' and joint['target']['a']==0 and joint['probe']['protocols'][rid]['pending']==b'cd','linked full input falsely acknowledged')
            require(self.a.read_input(2)==b'ab','linked input read order');joint=self.a.link_poll(rid)
            require(joint['target']['input']==b'cd' and joint['target']['a']==1,'linked pending retry after space recovery')
            joint=self.a.link_poll(rid)
            require(not joint['probe']['protocols'][rid]['pending'] and joint['target']['input']==b'cd' and joint['probe']['streams'][rid]['data']==b'x','linked final ack/duplicate suppression')
            probe.read_all(rid,b'x')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference and self.a.short_ms>=20 and self.a.long_ms>=1000 and self.a.input_capacity==2 and self.a.output_capacity==6,'explicit logical target timing/capacity contract')
            require(self.a.short_ms==20 and self.a.long_ms==1000,'selected fixture timing differs from boundary tests')
            spec=next(s for s in self.c.reg['interface'] if s['name']=='oep.target.console' and s['revision']==1)
            require(spec['enum']['mechanism']['dmseq']==2,'selected SPEC mechanism mismatch');self.c.confirm();self.c.identity()
        self.c.check('OEP-DMSEQ-TARGET-CONTEXT','dmseq target rules and timeout; explicit logical fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('LENGTHS',self.begin_lengths),('SEEDS',self.seeds)]
        cases += [(kind,lambda kind=kind:self.invalid(kind)) for kind in ('FOREIGN','M','K','CRC')]
        cases += [('ZERO-SHORT',self.zero),('ZERO-LONG',lambda:self.zero(True)),('TIMEOUT-SHORT',self.timeout),('TIMEOUT-LONG',lambda:self.timeout(True)),('TIMEOUT-RECOVERY',self.timed_out_write),('DUPLICATE-INPUT',self.duplicate_input),('BACKPRESSURE',self.backpressure),('EMPTY-ACK',self.empty_ack),('OUTPUT-ORDER',self.output_order),('FOREIGN-TIMEOUT',self.foreign_after_timeout)]
        cases += [('LINKED',self.linked)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-DMSEQ-TARGET-'+name,'dmseq target rules 0..3 and timeout',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(r['status']=='passed' for r in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical target-side dmseq with 20/1000 ms waits and two-byte input capacity',
                'clock':'explicit logical milliseconds; no physical timing claim',
                'probe_adapter_trace':self.a.probe.trace,
                'unchecked':['physical elapsed time and interrupt-disabled clock','target firmware integration','other queue capacities/wait policies','resync/TO/framing OEP marks']}
