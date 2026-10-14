"""Logical MEM-AP block packet, wrap and progress checks; retains base ARM checks."""
import struct
from .conformance import require, tlvs
from .conformance_arm_adi import ArmAdiChecks


class ArmAdiBlockChecks(ArmAdiChecks):
    base = 0x23fc
    words = (0x10203040,0x50607080,0x90abcdef,0xdeadbeef)
    def block_request(self, sid, parent, write, count=4):
        return self.request(5,'write_block' if write else 'read_block',sid,
            struct.pack('<IIH',parent,self.base,count)+(b''.join(struct.pack('<I',v) for v in self.words[:count]) if write else b''))
    def setup_block(self, sid):
        parent=self.parent(sid)
        self.a.seed(parent,{8:0x12000000},{0:0x23000052,4:0x1abc},0xfedcba98)
        self.a.memory(parent,{self.base+4*i:0x100+i for i in range(4)})
        return parent
    def block_result(self, request, done, status, values):
        frame=self.c.exchange(request)
        require(len(frame)>=8 and frame[3:5]==bytes((1,0 if not status else (2 if done else 1))),'MEM-AP block outcome')
        require(struct.unpack_from('<HB',frame,5)==(done,status),'MEM-AP block done/status')
        end=8+4*len(values);require(len(frame)>=end,'MEM-AP values truncated')
        require([struct.unpack_from('<I',frame,8+4*i)[0] for i in range(len(values))]==list(values),'MEM-AP posted value/order/prefix')
        tlvs(frame[end:]);return frame
    def unchanged_setup(self,parent):
        t=self.a.state()['targets'][parent]
        require(t['dp'][8]==0x12000000 and t['ap'][0]==0x23000052,'MEM-AP changed host SELECT/CSW')
    def boundary(self,write,count):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.setup_block(sid);before=self.a.state()
            self.block_result(self.block_request(sid,parent,write,count),count,0,() if write else range(0x100,0x100+count))
            after=self.a.state();bus=after['bus'][len(before['bus']):];packets=after['packets'][len(before['packets']):]
            require([row[2] for row in bus]==[self.base+4*i for i in range(count)],'MEM-AP TAR wrap accessed wrong bus addresses')
            require(len([row for row in packets if row[1]==(13 if write else 15) and row[2]==1])>=count,'MEM-AP DRW packet bypassed')
            if not write:require(any((row[1]==14 or row[1]&3==3 and row[1]!=15) and row[2]==1 for row in packets),'MEM-AP final posted value was not collected')
            if count>1:require(any(row[1]==5 and row[2]==1 and row[3]==0x2400 for row in packets),'MEM-AP did not reload TAR beyond guaranteed range')
            if write:require([after['targets'][parent]['memory'][self.base+4*i] for i in range(count)]==list(self.words[:count]),'MEM-AP write values/order')
            self.unchanged_setup(parent)
    def failed_block(self,write,index,ack,phase='data'):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.setup_block(sid);self.a.block_fault(index,phase,ack);before=self.a.state()
            self.block_result(self.block_request(sid,parent,write),index,{0:2,2:1,4:3}[ack],() if write else range(0x100,0x100+index))
            after=self.a.state();bus=after['bus'][len(before['bus']):]
            allowed=index+(phase=='buffer' and not write)
            require([row[2] for row in bus]==[self.base+4*i for i in range(allowed)],'MEM-AP executed failed word/suffix or retried store')
            if write:
                require([after['targets'][parent]['memory'][self.base+4*i] for i in range(4)]==list(self.words[:index])+[0x100+i for i in range(index,4)],'MEM-AP failing write changed target or prefix lost')
            self.unchanged_setup(parent)
    def drop(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.setup_block(sid);self.a.delivery(False);before=self.a.state()
            self.block_result(self.block_request(sid,parent,True),4,0,())
            after=self.a.state()
            require(after['targets'][parent]['memory']==before['targets'][parent]['memory'],'selected posted-write drop stimulus did not apply')
            require(len(after['bus'])-len(before['bus'])==4,'done did not count sent words')
            self.a.delivery(True)
            self.block_result(self.block_request(sid,parent,False),4,0,range(0x100,0x104))
    def replay_block(self,write):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.setup_block(sid);request=self.block_request(sid,parent,write)
            frame=self.block_result(request,4,0,() if write else range(0x100,0x104))
            self.a.block_fault(0,'data',4);before=self.a.state()
            require(self.c.exchange(request)==frame and self.a.state()==before,'MEM-AP block replay reexecuted or recomputed')
    def recover(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent=self.setup_block(sid);self.a.acks([1,2,2,1]);before=self.a.state()
            self.block_result(self.block_request(sid,parent,True,2),2,0,())
            bus=self.a.state()['bus'][len(before['bus']):]
            require([row[2] for row in bus]==[self.base,self.base+4],'WAIT recovery repeated store or lost word')
    def run(self):
        report=super().run()
        cases=[('BOUNDARY-'+str(write),lambda write=write:self.boundary(write,4)) for write in (False,True)]
        cases += [('WORD-'+str(write),lambda write=write:self.boundary(write,1)) for write in (False,True)]
        cases += [('FAIL-'+str(write)+'-'+str(ack)+'-'+str(i),lambda write=write,ack=ack,i=i:self.failed_block(write,i,ack)) for write in (False,True) for ack in (0,2,4) for i in (0,1,3)]
        cases += [('FAIL-TAR',lambda:self.failed_block(True,1,4,'tar')),('FAIL-BUFFER',lambda:self.failed_block(False,1,4,'buffer')),('DROP',self.drop),('REPLAY-READ',lambda:self.replay_block(False)),('REPLAY-WRITE',lambda:self.replay_block(True)),('WAIT-STORE',self.recover)]
        start=len(self.c.results)
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-ARM-PACKET-'+name,'debug §2/6; core §4/5',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        report['checks']+=self.c.results[start:];report['status']='passed' if all(x['status']=='passed' for x in report['checks']) else 'failed'
        report['scope']='logical ARM raw transfer and MEM-AP per-word posted blocks, wrap and partial outcomes'
        report['unchecked']=['electrical SWD and timing, TARGETSEL/tid, ADIv6 and full AP bank/CSW semantics','optimized pipelined block strategies and all delayed fault policies','all route capacities and u32 address overflow policy','production Host integration and complete core/common normative audit']
        return report
