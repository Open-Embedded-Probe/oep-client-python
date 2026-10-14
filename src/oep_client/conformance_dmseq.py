"""SPEC-first dmseq probe framing checks; no import of the virtual implementation."""
import struct
from .conformance import require, tlvs
from .conformance_mailbox import MailboxChecks


def crc_reference(data):
    remainder=255
    for byte in data:
        word=(remainder^byte)<<8
        for bit in range(15,7,-1):
            if word&(1<<bit):word^=0x107<<(bit-8)
        remainder=word
    return remainder


def target_frame(s=0,a=1,syn=False,to=False,data=b''):
    require(len(data)<=6,'finite dmseq target payload')
    prefix=bytes((128|(s<<5)|(a<<4)|(int(syn)<<3)|(int(to)<<6)|len(data),))+data
    raw=(prefix+bytes((crc_reference(prefix),))).ljust(8,b'\xa5')
    return int.from_bytes(raw[:4],'little'),int.from_bytes(raw[4:],'little')


def reply_word(s,h,data=b''):
    prefix=bytes(((s<<5)|(h<<4)|len(data),))+data
    require(len(data)<=2,'host reply payload bound')
    return int.from_bytes((prefix+bytes((crc_reference(prefix),))).ljust(4,b'\0'),'little')


class DmseqChecks(MailboxChecks):
    def step(self,rid,frame,received,answer,tx,protocol=None):
        self.a.frame(rid,*frame);before=self.a.state();self.a.poll(rid);after=self.a.state()
        operations=[('read',0x11,0),('read',4,frame[0])]
        if frame[0]&128 and frame[0]&7>=3:operations.append(('read',5,frame[1]))
        if answer is not None:operations.append(('write',4,answer))
        require(after['mailboxes'][rid]['events']==operations,'dmseq register order/value')
        require(after['mailboxes'][rid]['data0']==(frame[0] if answer is None else answer) and after['mailboxes'][rid]['data1']==frame[1],'dmseq reply word/DATA1 mutated')
        require(after['streams'][rid]['data']==before['streams'][rid]['data']+received and after['streams'][rid]['position']==before['streams'][rid]['position']+len(received),'dmseq duplicate/lost/reordered payload')
        require(after['streams'][rid]['tx']==tx,'dmseq popped wrong queue prefix')
        require(after['connections']==before['connections'] and after['next_id']==before['next_id'],'dmseq resource mutation')
        if protocol is not None:require(after['protocols'][rid]==protocol,'dmseq synchronization/pending state')
        elif answer is None:require(after['protocols'][rid]==before['protocols'][rid],'invalid or host-owned frame changed synchronization')
    @staticmethod
    def protocol(s=0,syn=False,h=0,pending=b'',synced=True):return dict(synced=synced,last_s=s,last_syn=syn,h=h,pending=pending)
    def first(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2)
            if kind=='VECTOR':
                require(crc_reference(b'123456789')==0xfb and crc_reference(b'\0')==0xf3,'normative CRC check values')
                require(target_frame(syn=True,data=b'')[0]&0xffff==0x3298 and reply_word(0,0)==0xf300,'normative word examples')
                self.step(rid,(0x3298,0xdeadbeef),b'',0xf300,b'',self.protocol(syn=True))
            else:
                for s,a in ((0,0),(1,0),(0,1),(1,1)):
                    # Explicit new fixture for each initial S/A; no assumption of zero seeds.
                    self.close(sid,rid);rid=self.opened_new(sid)
                    self.step(rid,target_frame(s,a,kind=='SYN',data=b'x'),b'x',reply_word(s,1-a),b'',self.protocol(s,kind=='SYN',1-a))
    def opened_new(self,sid):
        state=self.a.state();parent=next(iter(state['connections']));rid,_=self.console(sid,parent,2);return rid
    def lengths(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);output=b''
            for n in range(7):
                data=bytes(range(n));self.step(rid,target_frame(n&1,data=data,syn=n==0),data,reply_word(n&1,0),b'',self.protocol(n&1,n==0));output+=data
            self.read_all(rid,output)
    def invalid(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);self.write(sid,rid,b'abcd',0,4)
            if kind=='HOST-TURN':
                for status in range(128):self.step(rid,(0xabcdef00|status,0xffffffff),b'',None,b'abcd')
            elif kind=='N7':
                for status in (0x87,0xff):self.step(rid,(0xffffff00|status,0xffffffff),b'',None,b'abcd')
            else:
                for n in range(7):
                    d0,d1=target_frame(data=bytes(range(n)));raw=bytearray(struct.pack('<II',d0,d1))
                    for bit in range(8):
                        broken=bytearray(raw);broken[1+n]^=1<<bit
                        self.step(rid,struct.unpack('<II',broken),b'',None,b'abcd')
                # Invalid frames must preserve pending state after synchronization too.
                self.step(rid,target_frame(syn=True,data=b'x'),b'x',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=True,pending=b'ab'))
                self.step(rid,(0xffffffff,0xffffffff),b'',None,b'cd')
    def duplicate(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);self.write(sid,rid,b'abcd',0,4)
            syn=kind!='PLAIN';self.step(rid,target_frame(syn=syn,data=b'x'),b'x',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=syn,pending=b'ab'))
            frame=target_frame(syn=syn,to=kind=='TO',data=b'x')
            self.step(rid,frame,b'',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=syn,pending=b'ab'))
    def transitions(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);self.write(sid,rid,b'abcd',0,4)
            self.step(rid,target_frame(data=b'x'),b'x',reply_word(0,0,b'ab'),b'cd',self.protocol(pending=b'ab'))
            if kind=='SAME-S-SYN':self.step(rid,target_frame(a=0,syn=True,data=b'y'),b'y',reply_word(0,1,b'cd'),b'',self.protocol(syn=True,h=1,pending=b'cd'))
            elif kind=='NEW-S-SYN':self.step(rid,target_frame(1,0,True,data=b'y'),b'y',reply_word(1,1,b'cd'),b'',self.protocol(1,True,1,b'cd'))
            elif kind=='TO-NEW':self.step(rid,target_frame(1,to=True,data=b'y'),b'y',reply_word(1,0,b'ab'),b'cd',self.protocol(1,pending=b'ab'))
            elif kind=='IDENTICAL-NEW':self.step(rid,target_frame(1,data=b'x'),b'x',reply_word(1,0,b'ab'),b'cd',self.protocol(1,pending=b'ab'))
            elif kind=='DUP-ACK':self.step(rid,target_frame(a=0,data=b'x'),b'',reply_word(0,1,b'cd'),b'',self.protocol(h=1,pending=b'cd'))
            else:
                self.step(rid,target_frame(1,0,data=b'y'),b'y',reply_word(1,1,b'cd'),b'',self.protocol(1,h=1,pending=b'cd'))
                self.step(rid,target_frame(0,1,data=b'z'),b'z',reply_word(0,0),b'',self.protocol())
                self.write(sid,rid,b'e',0,1)
                self.step(rid,target_frame(1,1),b'',reply_word(1,0,b'e'),b'',self.protocol(1,pending=b'e'))
    def queue(self,n):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);data=b'abcde'[:n];accepted=min(n,4);self.write(sid,rid,data,0 if n<=4 else 2,accepted)
            first=data[:accepted][:2];rest=data[:accepted][2:]
            self.step(rid,target_frame(syn=True),b'',reply_word(0,0,first),rest,self.protocol(syn=True,pending=first))
    def stopped(self,busy):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);self.write(sid,rid,b'abcd',0,4);frame=target_frame(syn=True,data=b'x')
            self.a.frame(rid,*frame);control=self.a.debug_busy if busy else self.a.halted;control(rid,True)
            before=self.a.state();self.a.poll(rid);after=self.a.state()
            require(after['mailboxes'][rid]['events']==([] if busy else [('read',0x11,0x100)]) and after['streams']==before['streams'] and after['protocols']==before['protocols'],'dmseq poll during halt/debug')
            control(rid,False);self.step(rid,frame,b'x',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=True,pending=b'ab'))
    def reset(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);rid,_=self.console(sid,parent,2);self.write(sid,rid,b'abcd',0,4)
            self.step(rid,target_frame(syn=True,data=b'x'),b'x',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=True,pending=b'ab'))
            before=self.a.state();self.a.reset_target(parent);after=self.a.state()
            require(not after['protocols'][rid]['synced'] and not after['protocols'][rid]['pending'],'detected reset did not discard dmseq state')
            require(after['connections']==before['connections'] and after['streams'][rid]['data']==b'x' and after['streams'][rid]['tx']==b'cd' and after['streams'][rid]['position']==1,'target reset discarded OEP resources/output/queue')
            marks=after['streams'][rid]['marks'];require(len(marks)==len(before['streams'][rid]['marks'])+1 and marks[-1][1:3]==(1,0x41) and marks[-1][-1]==1,'detected reset restart mark')
            page=self.c.success(self.request(4,'marks',0,struct.pack('<II',rid,marks[-1][0])));require(page[:2]==b'\0\x01' and struct.unpack_from('<IQBQB',page,2)==marks[-1],'restart mark wire readback');tlvs(page[24:])
            self.step(rid,target_frame(syn=True,data=b'x'),b'x',reply_word(0,0,b'cd'),b'',self.protocol(syn=True,pending=b'cd'))
    def clear(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);self.write(sid,rid,b'abcd',0,4);frame=target_frame(syn=True,data=b'x')
            self.step(rid,frame,b'x',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=True,pending=b'ab'))
            before=self.a.state();self.c.success(self.request(4,'clear',sid,struct.pack('<I',rid)));after=self.a.state()
            require(after['protocols']==before['protocols'] and after['streams'][rid]['tx']==b'cd' and after['streams'][rid]['data']==b'' and after['streams'][rid]['position']==1,'clear discarded pending or queue or synchronization')
            self.step(rid,frame,b'',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=True,pending=b'ab'))
    def independent(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);one,_=self.console(sid,parent,2)
            parent2,_=self.attach(sid,pair=self.a.pairs[1][1]);two,_=self.console(sid,parent2,2)
            self.write(sid,one,b'abcd',0,4);self.write(sid,two,b'XYZ',0,3)
            self.step(one,target_frame(syn=True,data=b'1'),b'1',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=True,pending=b'ab'))
            before=self.a.state();self.step(two,target_frame(1,0,True,data=b'2'),b'2',reply_word(1,1,b'XY'),b'Z',self.protocol(1,True,1,b'XY'))
            after=self.a.state();require(after['streams'][one]==before['streams'][one] and after['protocols'][one]==before['protocols'][one] and after['mailboxes'][one]==before['mailboxes'][one],'cross-connection dmseq state')
    def close_replay(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2);req,original=self.write(sid,rid,b'abcd',0,4)
            self.step(rid,target_frame(syn=True,data=b'x'),b'x',reply_word(0,0,b'ab'),b'cd',self.protocol(syn=True,pending=b'ab'))
            before=self.a.state();require(self.c.exchange(req)==original and self.a.state()==before,'write replay duplicated dmseq queue')
            self.close(sid,rid);state=self.a.state()
            require(rid not in state['streams'] and rid not in state['protocols'] and rid not in state['mailboxes'],'closed dmseq pending remained live')
            require(self.c.exchange(req)==original and self.a.state()==state,'historical write replay recreated dmseq state')
            new=self.opened_new(sid);protocol=self.a.state()['protocols'][new]
            require(new>rid and not protocol['synced'] and not protocol['pending'],'reopened dmseq reused state or ID')
    def mark_event(self,kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2)
            self.step(rid,target_frame(data=b'x'),b'x',reply_word(0,0),b'',self.protocol())
            if kind in ('RESYNC-SAME','RESYNC-NEW'):
                bit=int(kind=='RESYNC-NEW');frame=target_frame(bit,syn=True,data=b'y');expected=(0x41,2)
                received=b'y';answer=reply_word(bit,0)
            elif kind in ('TO-NEW','TO-DUP'):
                bit=int(kind=='TO-NEW');frame=target_frame(bit,to=True,data=b'y' if bit else b'x');expected=(1,0x40)
                received=b'y' if bit else b'';answer=reply_word(bit,0)
            else:
                valid=target_frame(1,syn=True,to=True,data=b'y');frame=(valid[0]^(1<<16),valid[1]);expected=(1,2)
                received=b'';answer=None
            before=self.a.state()['streams'][rid];self.step(rid,frame,received,answer,b'')
            marks=self.a.state()['streams'][rid]['marks'];new=[m for m in marks if m not in before['marks']]
            require(any((m[2],m[4])==expected for m in new),'dmseq required event mark missing/wrong detail')
            if kind=='CRC-FLAGS':require(not any((m[2],m[4]) in ((0x41,2),(1,0x40)) for m in new),'invalid CRC SYN/TO emitted valid-frame mark')
            for m in new:
                page=self.c.success(self.request(4,'marks',0,struct.pack('<II',rid,m[0])))
                require(page[1]>=1 and struct.unpack_from('<IQBQB',page,2)==m,'dmseq event mark wire readback');tlvs(page[2+22*page[1]:])
            # A repeated observation may coalesce or append; payload/queue must not repeat.
            self.step(rid,frame,b'',answer,b'')
            after=self.a.state()['streams'][rid]['marks']
            if kind=='CRC-FLAGS':require(not any((m[2],m[4]) in ((0x41,2),(1,0x40)) for m in after),'invalid repeated CRC SYN/TO mark')
    def invalid_flags(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            rid=self.opened(sid,2)
            self.step(rid,target_frame(data=b'x'),b'x',reply_word(0,0),b'',self.protocol())
            for frame in ((0xff,0xffffffff),(0x78,0xffffffff)):
                before=self.a.state()['streams'][rid]['marks'];self.step(rid,frame,b'',None,b'')
                new=[m for m in self.a.state()['streams'][rid]['marks'] if m not in before]
                require(not any((m[2],m[4]) in ((0x41,2),(1,0x40)) for m in new),'invalid N/host word SYN/TO emitted valid-frame mark')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical dmseq fixture')
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in self.a.selection.items()}
            self.operations={fn:{op['name']:op['code'] for op in spec['op']} for fn,spec in self.specs.items()}
            require(self.specs[4]['enum']['mechanism']['dmseq']==2 and 2 in self.a.mechanisms,'selected SPEC/mechanism mismatch')
            console=self.specs[4]['enum'];common=self.c.reg['common']['enum']
            require(console['mark_kind']['restart']==0x41 and console['mark_detail_restart']=={'havereset':1,'resync':2},'reset mark SPEC mismatch')
            require(common['mark_kind']['lost']==1 and console['mark_detail_lost']['target_timeout']==0x40,'dmseq event mark SPEC mismatch')
            self.c.confirm();self.c.identity();self.c.declarations()
            declared=dict(reversed(self.c.describe(4)))[64];require(2 in declared[1:1+declared[0]],'probe did not declare dmseq')   # n x mechanism
        self.c.check('OEP-DMSEQ-CONTEXT','dmseq host rules; console §1/2/3',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[(k,lambda k=k:self.first(k)) for k in ('VECTOR','SYN','UNSYNCED')]+[('LENGTHS',self.lengths)]
        cases += [(k,lambda k=k:self.invalid(k)) for k in ('HOST-TURN','N7','CRC')]
        cases += [('DUP-'+k,lambda k=k:self.duplicate(k)) for k in ('PLAIN','SYN','TO')]
        cases += [(k,lambda k=k:self.transitions(k)) for k in ('SAME-S-SYN','NEW-S-SYN','TO-NEW','IDENTICAL-NEW','DUP-ACK','ACK-EMPTY')]
        cases += [('QUEUE-'+str(n),lambda n=n:self.queue(n)) for n in range(6)]
        cases += [('HALTED',lambda:self.stopped(False)),('DEBUG',lambda:self.stopped(True)),('RESET',self.reset),('CLEAR',self.clear)]
        cases += [('INDEPENDENT',self.independent),('CLOSE-REPLAY',self.close_replay)]
        cases += [('MARK-'+k,lambda k=k:self.mark_event(k)) for k in ('RESYNC-SAME','RESYNC-NEW','TO-NEW','TO-DUP','CRC-FLAGS')]
        cases += [('MARK-INVALID-FLAGS',self.invalid_flags)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-DMSEQ-'+name,'dmseq host rules 1..5; debug §4.6',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(r['status']=='passed' for r in rows) else 'failed','checks':rows,'adapter_trace':self.a.trace,'observed':self.c.observed,'full_conformance':False,
                'scope':'logical probe-side dmseq framing, pending queue and detected target reset',
                'unchecked':['target-side retransmission/timeout algorithm','actual riscv-dm wire requests/arbitration','physical DMI/timing/delivery','production Host integration','other capacities and concurrent pollers']}
