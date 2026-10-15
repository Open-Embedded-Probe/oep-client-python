"""Independent optional riscv-dm block admission, progress and restoration checks."""
import struct
from .conformance import require, tlvs, ops
from .conformance_debug import DebugChecks
from .conformance_dmseq import target_frame


HARTSEL=(0x3ff<<6)|(0x3ff<<16)


class BlockChecks(DebugChecks):
    base=0x20000000
    def parent(self,sid,halt=True):
        parent,_=self.attach(sid)
        if halt:self.c.success(self.request(5,'halt',sid,struct.pack('<I',parent)))
        self.a.memory(parent,{self.base:0x12345678,self.base+4:0xabcdef01})
        self.a.registers(parent,{4:0x33445566,5:0x778899aa,0x18:0x55,0x10:0x800700c1})
        self.a.scratch(parent,{8:0x11110008,9:0x22220009,10:0x3333000a,11:0x4444000b,12:0x5555000c})
        return parent
    def block_request(self,sid,parent,write=False,count=2,address=None,values=None):
        payload=struct.pack('<IIH',parent,self.base if address is None else address,count)
        if write:payload+=b''.join(struct.pack('<I',value) for value in (values if values is not None else [0x55+i for i in range(count)]))
        return self.request(5,'write_block' if write else 'read_block',sid,payload)
    def block(self,sid,parent,write=False,count=2,done=2,status=0,values=None):
        before=self.a.state();request=self.block_request(sid,parent,write,count);frame=self.c.exchange(request)
        require(len(frame)>=8 and frame[3:5]==bytes((1,0 if not status else (2 if done else 1))),'block outcome/header')
        require(struct.unpack_from('<HB',frame,5)==(done,status),'block done/status')
        size=8 if write else 8+4*done
        require(len(frame)>=size,'block response truncated')
        if not write:require([struct.unpack_from('<I',frame,8+4*i)[0] for i in range(done)]==(values or []),'block bus values/order')
        tlvs(frame[size:]);after=self.a.state()
        prior=before['targets'][parent];current=after['targets'][parent]
        for address in (4,5,0x18):require(current['registers'].get(address,0)==prior['registers'].get(address,0),'DATA/abstractauto not restored')
        require(current['gprs']==prior['gprs'],'GPR scratch or unrelated register not restored')
        require(current['registers'][0x10]&HARTSEL==0,'high-level op left hartsel nonzero')
        require(current['halted']==prior['halted'] and current['haltreq']==prior['haltreq'],'block changed hart state/haltreq')
        if count and prior['halted']:
            delta=after['restore'][len(before['restore']):]
            require([row[2] for row in delta if row[2] in (4,5)]==[5,4],'DATA1 must restore before DATA0')
        expected_memory=dict(before['memory'][parent])
        if write:
            for i in range(done):expected_memory[self.base+4*i]=0x55+i
        require(after['memory'][parent]==expected_memory,'block changed non-prefix or failed suffix')
        return request,frame
    def zero(self,halt):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid,halt)
            self.block(sid,parent,count=0,done=0,values=[]);self.block(sid,parent,write=True,count=0,done=0)
    def roundtrip(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.block(sid,parent,write=True)
            self.block(sid,parent,values=[0x55,0x56])
    def live(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.block(sid,parent,values=[0x12345678,0xabcdef01])
            self.a.memory(parent,{self.base:0x87654321,self.base+4:0x10203040})
            self.block(sid,parent,values=[0x87654321,0x10203040])
    def partial(self,write,index):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);self.a.block_failure(index,2)
            self.block(sid,parent,write=write,done=index,status=2,values=[0x12345678][:index])
    def rejected_block(self,write,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid)
            if kind=='ALIGN':self.refused(self.block_request(sid,parent,write,address=self.base+1),'malformed')
            elif kind=='OVER':self.refused(self.block_request(sid,parent,write,count=3),'unsupported',b'\0')
            else:self.refused(self.block_request(sid,parent,True,count=2,values=[123]),'malformed')
    def running(self,write):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid,False);self.block(sid,parent,write,done=0,status=5,values=[])
            require(not self.a.state()['restore'],'running-hart refusal touched scratch')
    def mailbox(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);rid,_=self.console(sid,parent,2);frame=target_frame(syn=True,data=b'wire');self.a.frame(rid,*frame)
            before=self.a.state();self.block(sid,parent,values=[0x12345678,0xabcdef01]);after=self.a.state()
            require(after['mailboxes'][rid]['data0']==frame[0] and after['mailboxes'][rid]['data1']==frame[1] and after['protocols'][rid]==before['protocols'][rid],'block destroyed live console frame/state')
            polls=after['arbitration'][len(before['arbitration']):];require(len(polls)==2 and all(not row['events'] for row in polls),'console read block scratch mid-operation')
            self.c.success(self.request(5,'resume',sid,struct.pack('<I',parent)));self.a.poll(rid)
            require(self.a.state()['streams'][rid]['data']==b'wire','restored console frame not delivered after resume')
    def replay_block(self,write):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);req,result=self.block(sid,parent,write,values=[0x12345678,0xabcdef01])
            self.a.memory(parent,{self.base:999});before=self.a.state()
            require(self.c.replayed(req,result,frame=True) and self.a.state()==before,'block replay touched bus or recomputed result')
    def cleanup_failed(self,write):
        """debug §4: the restore after a transfer fails - partial even with every word done, status the failure, then
        the connection and its streams close."""
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);rid,_=self.console(sid,parent,2);self.a.restore_failure(True)
            try:frame=self.c.exchange(self.block_request(sid,parent,write))
            finally:self.a.restore_failure(False)
            require(len(frame)>=8 and frame[3:5]==b'\x01\x02','cleanup failure must answer completed partial')
            done,status=struct.unpack_from('<HB',frame,5);require(done==2 and status!=0,'cleanup failure done/status')
            after=self.a.state();require(parent not in after['connections'] and rid not in after['streams'],'connection/streams kept after a failed cleanup')
            self.refused(self.block_request(sid,parent,write),'no_resource')
    def bad_resource(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.parent(sid);rid,_=self.console(sid,parent,2)
            self.refused(self.block_request(sid,65535),'no_resource');self.refused(self.block_request(sid,rid),'unavailable',cause=6)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference and self.a.max_length==8,'explicit logical block fixture')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()}
            self.operations={fn:{row['name']:row['code'] for row in spec['op']} for fn,spec in self.specs.items()}
            require(self.operations[5]['read_block']==20 and self.operations[5]['write_block']==21,'selected block grammar mismatch')
            self.c.confirm();self.c.identity();self.c.declarations();description=dict(reversed(self.c.describe(5)))
            require({20,21}<=ops(description[7]) and struct.unpack('<H',description[self.c.reg['describe_common']['max_length']])[0]==8,'paired block ops/max_length missing')
            require(20+8<=self.c.max_frame and 8+8<=self.c.max_frame,'declared block size exceeds negotiated route')
        self.c.check('OEP-BLOCK-CONTEXT','debug §4/4.5; core §7.4; explicit fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('ZERO-HALTED',lambda:self.zero(True)),('ZERO-RUNNING',lambda:self.zero(False)),('ROUNDTRIP',self.roundtrip),('LIVE',self.live)]
        cases += [(('WRITE' if write else 'READ')+'-FAIL-'+str(index),lambda write=write,index=index:self.partial(write,index)) for write in (False,True) for index in (0,1)]
        cases += [(('WRITE' if write else 'READ')+'-'+kind,lambda write=write,kind=kind:self.rejected_block(write,kind)) for write in (False,True) for kind in ('ALIGN','OVER')]
        cases += [('TRUNCATED',lambda:self.rejected_block(True,'TRUNCATED')),('RUNNING-READ',lambda:self.running(False)),('RUNNING-WRITE',lambda:self.running(True)),('MAILBOX',self.mailbox),('REPLAY-READ',lambda:self.replay_block(False)),('REPLAY-WRITE',lambda:self.replay_block(True)),('RESOURCES',self.bad_resource),('CLEANUP-READ',lambda:self.cleanup_failed(False)),('CLEANUP-WRITE',lambda:self.cleanup_failed(True))]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-BLOCK-'+name,'debug §4/4.5; common §2/3',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical optional riscv-dm block bus access and scratch/mailbox restoration',
                'unchecked':['all transport route max_frame limits','physical abstract commands/GPR restore failure','program buffer/sysbus algorithms','RV32 address overflow and bus access permissions','reset/step/run','actual response-writer ordering','other capacities and production Host integration']}
