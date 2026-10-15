"""Positioned stream read/marks verdicts from explicit adoption and independent stimuli."""
import struct
from .conformance import require, ops, tlvs


class StreamChecks:
    def __init__(self, checks, adapter):self.c,self.a=checks,adapter

    def fresh(self, position=0, serial=0):
        result=self.a.restart_fixture(position,serial)
        require(result['scope']=='logical-model' and result['boot_id']!=self.c.boot,'logical fixture needs new boot')
        self.c.confirm();self.c.identity()
        require(self.c.boot==result['boot_id'],'reset boot readback')

    def request(self, action, payload=b'', sid=0):
        return self.c.request(self.a.adoption['ops'][action],payload,session=sid,fn=self.a.adoption['fn'])

    def read(self, mode=0, arg=0, maximum=65535):
        before=self.a.stream_state()
        payload=self.c.success(self.request('read',struct.pack('<BQH',mode,arg,maximum)))
        require(len(payload)>=11,'read fixed fields')
        start,flags,size=struct.unpack_from('<QBH',payload)
        require(size<=maximum and len(payload)>=11+size,'read count/maximum')
        tlvs(payload[11+size:]);after=self.a.stream_state()
        require(before==after,'SID 0 read consumed or changed stream')
        return start,flags&3,payload[11:11+size]

    def marks(self, first):
        before=self.a.stream_state();payload=self.c.success(self.request('marks',struct.pack('<I',first)))
        require(len(payload)>=2 and payload[0] in (0,1) and len(payload)>=2+22*payload[1],'marks count/header')
        rows=[struct.unpack_from('<IQBQB',payload,2+22*i) for i in range(payload[1])]
        tlvs(payload[2+22*payload[1]:]);require(before==self.a.stream_state(),'SID 0 marks consumed or changed stream')
        return payload[0],rows

    def expect_read(self, expected, **kwargs):require(self.read(**kwargs)==expected,'read position/flags/data')

    def empty(self):
        self.fresh()
        for mode in range(4):self.expect_read((0,0,b''),mode=mode)
        require(self.marks(0)==(0,[]),'empty mark ring')

    def position(self):
        base=(1<<32)+7;self.fresh(base);data=b'abcdefgh';self.a.feed_bytes(data)
        self.expect_read((base,0,data),arg=base)
        self.expect_read((base+2,1,b'cde'),arg=base+2,maximum=3)
        self.expect_read((base+8,0,b''),arg=base+100)
        self.expect_read((base+8,0,b''),mode=2)
        self.expect_read((base,1,b''),arg=base,maximum=0)

    def frame(self):
        self.fresh();data=bytes(range(64));self.a.feed_bytes(data);position=0;seen=bytearray()
        while position<len(data):
            start,flags,part=self.read(arg=position)
            require(start==position and part and len(part)<=self.c.max_frame-16,'read frame/count progress')
            seen.extend(part);position+=len(part)
            require(flags==int(position<len(data)),'read more flag')
        require(bytes(seen)==data,'read pages changed bytes')
        self.expect_read((0,1,data[:3]),maximum=3)
        self.expect_read((0,1,data[:3]),maximum=3)

    def gap(self):
        self.fresh();data=bytes(range(80));self.a.feed_bytes(data)
        self.expect_read((16,3,data[16:20]),arg=0,maximum=4)
        self.expect_read((16,1,data[16:20]),mode=1,maximum=4)
        self.expect_read((80,0,b''),mode=2)
        _,rows=self.marks(0);require(rows and rows[-1][1:3]==(80,1) and rows[-1][4]==1,'overflow lost mark')

    def selectors(self):
        self.fresh();self.a.feed_bytes(b'ab');self.a.mark_external(3,11);self.a.feed_bytes(b'cd');self.a.mark_external(0x40,1);self.a.feed_bytes(b'ef')
        self.expect_read((2,0,b'cdef'),mode=3,arg=3)
        self.expect_read((4,0,b'ef'),mode=3,arg=0)
        self.expect_read((6,0,b''),mode=3,arg=0x7f)
        self.expect_read((0,0,b'abcdef'))  # an interface-defined event mark keeps earlier bytes

    def pages(self, serial=10):
        self.fresh(serial=serial)
        before_clock=self.c.success(self.c.request(self.c.core['clock']))
        for detail in range(5):self.a.mark_external(3,detail)
        after_clock=self.c.success(self.c.request(self.c.core['clock']))
        expected=self.a.stream_state()['marks'];cursor=serial;seen=[]
        for _ in range(6):
            more,rows=self.marks(cursor)
            require(rows==expected[len(seen):len(seen)+len(rows)] and rows,'inclusive mark page/order')
            seen.extend(rows);cursor=(rows[-1][0]+1)&0xffffffff
            require(more==int(len(seen)<len(expected)),'mark more flag')
            if not more:break
        require(seen==expected and self.marks(cursor)==(0,[]),'next serial marks end')
        lower=struct.unpack_from('<Q',before_clock,4)[0];upper=struct.unpack_from('<Q',after_clock,4)[0]
        require([row[0] for row in seen]==[(serial+i)&0xffffffff for i in range(5)] and all(row[2]==3 for row in seen),
                'mark serial/kind differ from stimulus')
        require(all(lower<=row[3]<=upper for row in seen),'mark timestamp outside independent clock interval')
        require([row[1] for row in seen]==[0]*5 and [row[4] for row in seen]==list(range(5)),'same-position marks dropped')
        require(all(a[3]<=b[3] for a,b in zip(seen,seen[1:])),'mark timestamps decreased')
        require(self.marks(serial)[1]==seen[:len(self.marks(serial)[1])],'marks not repeatable')

    def retained_span(self):
        state=self.a.stream_state();rows=state['marks']
        require(len(rows)<=self.a.adoption['mark_capacity'],'mark ring exceeded explicit capacity')
        # core §2.6: the newest kept minus the oldest kept is under a quarter of the space (not next_serial minus it)
        require(not rows or all((rows[-1][0]-row[0])%2**32<2**30 for row in rows),'retained mark serial reached quarter span')

    def lost_marks(self, serial=100):
        self.fresh(serial=serial)
        for detail in range(8):self.a.mark_external(3,detail)
        self.retained_span()
        expected=self.a.stream_state()['marks'];require(len(expected)==5 and expected[0][0]==(serial+3)%2**32,'controlled ring eviction')
        require([row[0] for row in expected]==[(serial+i)%2**32 for i in range(3,8)] and [row[4] for row in expected]==list(range(3,8)),'wrapped eviction serial/detail order')
        for cursor in (serial,(serial-1)%2**32,(serial+9)%2**32):
            more,rows=self.marks(cursor);require(more and rows==expected[:len(rows)] and rows,'unretained serial must fall back to oldest')
        more,rows=self.marks((serial+4)%2**32);require(more and rows[0][0]==(serial+4)%2**32,'retained serial must be inclusive')
        require(self.marks((serial+8)%2**32)==(0,[]),'next serial must be empty')
        self.expect_read((0,0,b''),mode=3,arg=1)

    def moving_pages(self):
        serial=0xfffffffe;base=(1<<40)+17
        self.fresh(base,serial);self.a.feed_bytes(b'ab')
        before=self.c.success(self.c.request(self.c.core['clock']))
        for detail in range(5):self.a.mark_external(3,detail)
        self.retained_span()
        more,first=self.marks(serial)
        require(more and len(first)==2 and [row[0] for row in first]==[serial,0xffffffff],'initial moving page geometry/order differs')
        cursor=(first[-1][0]+1)%2**32
        # Ordered producer arrivals between reads, not inside a read operation.
        for detail in range(5,8):self.a.mark_external(3,detail)
        self.retained_span()
        seen=list(first);expected_details=[3,4,5,6,7];lost=0
        for _ in range(4):
            more,rows=self.marks(cursor)
            require(rows,'moving page ended before retained marks')
            gap=(rows[0][0]-cursor)%2**32
            lost+=gap;require(gap==(1 if len(seen)==2 else 0),'moving cursor did not report exactly one lost mark')
            details=expected_details[:len(rows)];expected_details=expected_details[len(rows):]
            require([row[0] for row in rows]==[(serial+i)%2**32 for i in details] and [row[4] for row in rows]==details,'moving page duplicated/skipped/reordered retained marks')
            seen.extend(rows);cursor=(rows[-1][0]+1)%2**32
            require(more==int(bool(expected_details)),'moving page more flag differs')
            if not more:break
        else:raise ValueError('moving mark pages did not terminate')
        after=self.c.success(self.c.request(self.c.core['clock']))
        lower=struct.unpack_from('<Q',before,4)[0];upper=struct.unpack_from('<Q',after,4)[0]
        require(lost==1 and [row[4] for row in seen]==[0,1,3,4,5,6,7] and len({row[0] for row in seen})==7,'moving mark loss/duplicates differ')
        require(all(row[1]==base+2 and row[2]==3 and lower<=row[3]<=upper for row in seen),'moving mark position/kind/time differs')
        require(self.marks(cursor)==(0,[]) and cursor==(serial+8)%2**32,'moving mark next cursor differs')
        self.expect_read((base,0,b'ab'),arg=base)
        self.c.observed['moving_mark_pages']={'seed':serial,'base':base,'seen_serials':[row[0] for row in seen],
                                             'seen_details':[row[4] for row in seen],'missing_marks':lost,'next_serial':cursor}

    def mark_evicted(self):
        self.fresh();self.a.feed_bytes(b'ab');self.a.mark_external(1,1);self.a.feed_bytes(b'cd')
        for detail in range(5):self.a.mark_external(3,detail)
        self.expect_read((4,0,b''),mode=3,arg=1)
        self.expect_read((0,0,b'abcd'))

    def mark_gap(self):
        self.fresh();self.a.mark_external(3,1);data=bytes(range(80));self.a.feed_bytes(data)
        self.expect_read((16,3,data[16:20]),mode=3,arg=3,maximum=4)

    def limit(self):
        base=(1<<64)-4;self.fresh(base);self.a.feed_bytes(b'abc')
        self.expect_read((base,0,b'abc'),arg=base)
        self.expect_read((base+3,0,b''),mode=2)
        self.expect_read((base+3,0,b''),arg=(1<<64)-1)

    def clear(self):
        self.fresh();self.a.feed_bytes(b'abc')
        with self.c.holding(3000) as sid:
            request=self.request('clear',sid=sid);frame=self.c.exchange(request)
            require(frame[3:5]==b'\x01\0','clear success');tlvs(frame[5:])
            after=self.a.stream_state();require(after['position']==3 and not after['data'],'clear reset byte position')
            require(self.c.exchange(request)==frame and self.a.stream_state()==after,'clear replay duplicated mark')
        self.expect_read((3,2,b''))
        more,rows=self.marks(0);require(not more and len(rows)==1 and rows[0][1:3]==(3,2),'clear mark')

    def persistent(self):
        self.fresh();self.a.feed_bytes(b'abc')
        with self.c.holding(3000) as sid:
            request=self.request('mark',b'\x37',sid);frame=self.c.exchange(request);self.c.success(request)
            require(frame[3:5]==b'\x01\0','host mark success');tlvs(frame[5:])
            state=self.a.stream_state()
            require(len(state['marks'])==1 and state['marks'][0][1:3]==(3,3) and state['marks'][0][4]==0x37,'host mark/replay')
        require(self.a.stream_state()==state,'persistent fn resets position/marks at end')
        with self.c.holding(3000):require(self.a.stream_state()==state,'persistent fn resets at new session')
        self.expect_read((0,0,b'abc'))

    def locks(self):
        self.fresh()
        for action,payload in [('clear',b''),('mark',b'\0'),('write',b'\0\0')]:self.c.rejected(self.request(action,payload),'session_required')
        self.c.rejected(self.request('read',struct.pack('<BQH',4,0,1)),'unsupported')
        self.c.rejected(self.request('read',b''),'malformed')
        self.c.rejected(self.request('marks',b''),'malformed')
        self.expect_read((0,0,b''));require(self.marks(0)==(0,[]),'refusal changed marks')

    def run(self):
        start=len(self.c.results)
        def identify():
            a=self.a.adoption
            enums=self.c.reg['common']['enum']
            for name,expected in [('read_from',{'position':0,'oldest':1,'now':2,'last_mark':3}),
                                  ('read_flags',{'more':1,'gap':2}),('mark_kind',{'lost':1,'clear':2,'host':3}),
                                  ('mark_detail_lost',{'overflow':1})]:
                require(all(enums[name].get(key)==value for key,value in expected.items()),'selected SPEC enum differs from this sample contract')
            require(a['component']=='positioned-stream-v1' and a['addressing']=='persistent-fn' and a['reference'] and
                    set(a['ops'])=={'read','marks','clear','mark','write'} and len(set(a['ops'].values()))==5 and
                    a['byte_capacity']==64 and a['mark_capacity']==5,'explicit sample adoption/geometry required')
            require(getattr(self.a,'reset_scope',None)=='logical-model' and all(callable(getattr(self.a,name,None)) for name in
                    ('restart_fixture','feed_bytes','mark_external','stream_state')),'explicit logical stream controls required')
            self.c.confirm();self.c.identity();self.c.declarations()
            entries=self.c.list_snapshot();require(any(row['fn']==a['fn'] for row in entries),'explicit stream fn absent')
            rows=self.c.describe(a['fn']);offered=ops(dict(reversed(rows))[self.c.reg['describe_common']['ops']])
            require(set(a['ops'].values())<=offered,'adopted stream requires all five operations')
            self.c.observed['stream_adoption']=dict(a)
        self.c.check('IF-STREAM-CONTEXT','common §1; explicit adoption',identify)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name,case in [('EMPTY',self.empty),('POSITION',self.position),('FRAME',self.frame),('GAP',self.gap),
                          ('SELECTORS',self.selectors),('PAGES',self.pages),('WRAP',lambda:self.pages(0xfffffffe)),
                          ('EVICTION',self.lost_marks),('EVICTION-WRAP',lambda:self.lost_marks(0xfffffffc)),
                          ('MOVING-PAGES',self.moving_pages),('MARK-EVICTED',self.mark_evicted),('MARK-GAP',self.mark_gap),
                          ('LIMIT',self.limit),('CLEAR',self.clear),('PERSISTENT',self.persistent),('REFUSALS',self.locks)]:
            first=len(self.a.trace);self.c.check('IF-STREAM-'+name,'common §1.1–1.4; core §2.6/5.2/7.3',case)
            self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        checks=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in checks) else 'failed','full_conformance':False,
                'scope':'explicit persistent-fn positioned stream read/marks','levels':{'core':'session/replay prerequisites',
                'interface':'selected adopted positioned-stream contracts','oep-interface':'not executed'},
                'checks':checks,'observed':self.c.observed,'adapter_trace':self.a.trace,
                'unchecked':['resource-addressed stream lifecycle','write delivery/partial/failed outcomes','notification data',
                             'physical UART/reset','status/done debug component','other ring/frame limits','simultaneous producers inside a request']}
