"""Independent standard SDI/DMDATA mailbox checks, with explicit logical stimulus."""
import struct
from .conformance import require, tlvs
from .conformance_attach import AttachChecks


class MailboxChecks(AttachChecks):
    @staticmethod
    def words(status,data=b''):
        padded=data.ljust(7,b'\xa5')
        return status|int.from_bytes(padded[:3],'little')<<8,int.from_bytes(padded[3:7],'little')
    def write(self,sid,rid,data,outcome,accepted):
        req=self.request(4,'write',sid,struct.pack('<HH',rid,len(data))+data)
        before=self.a.state();value=self.c.exchange(req)
        require(value is not None and len(value)>=7 and value[:5]==struct.pack('<BHBB',2,struct.unpack_from('<H',req,1)[0],1,outcome),'console write completion classification')
        require(struct.unpack_from('<H',value,5)[0]==accepted,'console accepted');tlvs(value[7:])
        after=self.a.state();require(after['streams'][rid]['tx']==before['streams'][rid]['tx']+data[:accepted],'write did not append accepted prefix')
        require(after['streams'][rid]['data']==before['streams'][rid]['data'] and after['mailboxes']==before['mailboxes'],'write accessed mailbox or changed received data')
        return req,value
    def read_all(self,rid,expected):
        req=self.request(4,'read',0,struct.pack('<HBQH',rid,1,0,64));value=self.c.success(req)
        require(len(value)>=11,'console read size');start,flags,count=struct.unpack_from('<QBH',value)
        require(start==0 and flags==0 and value[11:11+count]==expected,'mailbox buffer/read mismatch');tlvs(value[11+count:])
        require(self.c.success(self.request(4,'read',0,struct.pack('<HBQH',rid,1,0,64)))==value,'console read consumed bytes')
    def opened(self,sid,mechanism):
        parent,_=self.attach(sid);rid,_=self.console(sid,parent,mechanism);return rid
    def poll_expect(self,rid,data0,data1,received,operations,tx):
        self.a.frame(rid,data0,data1);before=self.a.state();self.a.poll(rid);after=self.a.state()
        require(after['mailboxes'][rid]['events']==operations,'mailbox DMI access order/value differs')
        require(after['streams'][rid]['data']==before['streams'][rid]['data']+received,'mailbox bytes lost/reordered/invented')
        require(after['streams'][rid]['position']==before['streams'][rid]['position']+len(received),'mailbox position')
        require(after['streams'][rid]['tx']==tx,'mailbox tx queue consumption')
        require(after['connections']==before['connections'] and after['next_id']==before['next_id'],'poll changed resource identity')
        expected_word=next((row[2] for row in reversed(operations) if row[:2]==('write',4)),data0)
        require(after['mailboxes'][rid]['data0']==expected_word and after['mailboxes'][rid]['data1']==data1,'mailbox word not acknowledged or DATA1 changed')
    def lengths(self,mechanism):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,mechanism);output=b''
            for n in range(8):
                data=bytes(range(1,n+1));status=n if mechanism==0 else 0x80|(n+4)
                d0,d1=self.words(status,data);ops=[('read',0x11,0),('read',4,d0)]
                if (mechanism==0 and n) or n>=4:ops.append(('read',5,d1))
                if mechanism==1 or n:ops.append(('write',4,0))
                self.poll_expect(rid,d0,d1,data,ops,b'');output+=data
            self.read_all(rid,output)
    def invalid(self,mechanism):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,mechanism)
            if mechanism==1:self.write(sid,rid,b'abcd',0,4)
            statuses=range(8,256) if mechanism==0 else [0x80|n for n in list(range(4))+list(range(12,64))]
            for status in statuses:
                d0,d1=self.words(status);ops=[('read',0x11,0),('read',4,d0)]
                if mechanism==1:ops.append(('write',4,0))
                self.poll_expect(rid,d0,d1,b'',ops,b'abcd' if mechanism else b'')
            self.read_all(rid,b'')
    def own_turn(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,1);self.write(sid,rid,b'abcd',0,4)
            for status in range(128):
                d0,d1=self.words(status);self.poll_expect(rid,d0,d1,b'',[('read',0x11,0),('read',4,d0)],b'abcd')
    def ignored_bit(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,1);d0,d1=self.words(0xc7,b'xyz')
            self.poll_expect(rid,d0,d1,b'xyz',[('read',0x11,0),('read',4,d0),('write',4,0)],b'')
    def sdi_write(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,0)
            for data in (b'',b'x',b'1234567'):
                before=self.a.state();self.write(sid,rid,data,1,0);require(self.a.state()==before,'SDI created or changed sender queue')
    def queue(self,size):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,1);data=b'abcde'[:size];accepted=min(size,4)
            self.write(sid,rid,data,0 if size<=4 else 2,accepted);pending=data[:accepted]
            for _ in range(2):
                reply=pending[:3];word=0 if not reply else (len(reply)+4)|int.from_bytes(reply,'little')<<8;pending=pending[3:]
                self.poll_expect(rid,0x84,0xffffffff,b'',[('read',0x11,0),('read',4,0x84),('write',4,word)],pending)
    def refill(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,1);self.write(sid,rid,b'abcd',0,4);self.write(sid,rid,b'z',1,0)
            self.poll_expect(rid,0x84,0,b'',[('read',0x11,0),('read',4,0x84),('write',4,0x63626107)],b'd')
            self.write(sid,rid,b'efgh',2,3)
            self.poll_expect(rid,0x84,0,b'',[('read',0x11,0),('read',4,0x84),('write',4,0x66656407)],b'g')
    def blocked(self,mechanism,busy):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,mechanism)
            if mechanism:self.write(sid,rid,b'q',0,1)
            control=self.a.debug_busy if busy else self.a.halted;control(rid,True)
            d0,d1=self.words(1 if not mechanism else 0x85,b'x')
            self.poll_expect(rid,d0,d1,b'',[] if busy else [('read',0x11,0x100)],b'q' if mechanism else b'')
            control(rid,False);ops=[('read',0x11,0),('read',4,d0)]
            if not mechanism:ops.append(('read',5,d1))
            ops.append(('write',4,0x7105 if mechanism else 0))
            self.poll_expect(rid,d0,d1,b'x',ops,b'')
    def replay(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,1);req,original=self.write(sid,rid,b'abc',0,3)
            self.poll_expect(rid,0x84,0,b'',[('read',0x11,0),('read',4,0x84),('write',4,0x63626107)],b'')
            before=self.a.state();require(self.c.exchange(req)==original and self.a.state()==before,'write replay refilled or re-delivered sender queue')
    def clear(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,1);self.write(sid,rid,b'q',0,1);d0,d1=self.words(0x85,b'x')
            self.poll_expect(rid,d0,d1,b'x',[('read',0x11,0),('read',4,d0),('write',4,0x7105)],b'')
            self.write(sid,rid,b'z',0,1);self.c.success(self.request(4,'clear',sid,struct.pack('<H',rid)))
            state=self.a.state();require(state['streams'][rid]['data']==b'' and state['streams'][rid]['position']==1 and state['streams'][rid]['tx']==b'z','clear changed sender queue or position')
    def acknowledged(self,mechanism):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,mechanism)
            if mechanism:self.write(sid,rid,b'abcd',0,4)
            d0,d1=self.words(1 if not mechanism else 0x85,b'x');answer=0x63626107 if mechanism else 0
            ops=[('read',0x11,0),('read',4,d0)]
            if not mechanism:ops.append(('read',5,d1))
            ops.append(('write',4,answer))
            self.poll_expect(rid,d0,d1,b'x',ops,b'd' if mechanism else b'')
            before=self.a.state();self.a.poll(rid);after=self.a.state()
            require(after['streams']==before['streams'],'acknowledged frame delivered twice or consumed extra queued bytes')
            require(after['mailboxes'][rid]['events']==[('read',0x11,0),('read',4,answer)],'probe rewrote its acknowledgement')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical mailbox contract')
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in self.a.selection.items()}
            self.operations={fn:{op['name']:op['code'] for op in spec['op']} for fn,spec in self.specs.items()}
            require(self.specs[4]['enum']['mechanism']['sdi']==0 and self.specs[4]['enum']['mechanism']['dmdata']==1 and self.operations[4]['write']==21,'mailbox SPEC differs')
            self.c.confirm();self.c.identity();self.c.declarations()
            require(set(self.a.mechanisms)=={0,1},'selected mailbox subset must be SDI/DMDATA')
        self.c.check('OEP-MAILBOX-CONTEXT','console §1/2/3; explicit logical fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('SDI-LENGTHS',lambda:self.lengths(0)),('DM-LENGTHS',lambda:self.lengths(1)),('SDI-INVALID',lambda:self.invalid(0)),('DM-INVALID',lambda:self.invalid(1)),('DM-OWN-TURN',self.own_turn),('DM-BIT6',self.ignored_bit),('SDI-WRITE',self.sdi_write)]
        cases += [('DM-QUEUE-'+str(n),lambda n=n:self.queue(n)) for n in range(6)]
        cases += [('DM-REFILL',self.refill),('SDI-HALTED',lambda:self.blocked(0,False)),('DM-HALTED',lambda:self.blocked(1,False)),('SDI-DEBUG',lambda:self.blocked(0,True)),('DM-DEBUG',lambda:self.blocked(1,True)),('REPLAY',self.replay),('CLEAR',self.clear)]
        cases += [('SDI-ACK',lambda:self.acknowledged(0)),('DM-ACK',lambda:self.acknowledged(1))]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-MAILBOX-'+name,'console §2/3.1/3.2; common §1.4',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical SDI/DMDATA mailbox and bounded sender queue','unchecked':['dmseq','target self-reset/restart mark','actual standard riscv-dm requests and arbitration','physical DMI/timing/delivery','production Host integration','other queue/ring capacities and concurrent pollers']}
