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
        _,rows=self.marks(0);require(rows and rows[-1][1:3]==(80,5) and rows[-1][4]==1,'overflow lost mark')

    def selectors(self):
        self.fresh();self.a.feed_bytes(b'ab');self.a.mark_external(7,11);self.a.feed_bytes(b'cd');self.a.mark_external(1,1);self.a.feed_bytes(b'ef')
        self.expect_read((2,0,b'cdef'),mode=3,arg=7)
        self.expect_read((4,0,b'ef'),mode=3,arg=0)
        self.expect_read((6,0,b''),mode=3,arg=0x7f)
        self.expect_read((0,0,b'abcdef'))  # target reset keeps earlier bytes

    def pages(self, serial=10):
        self.fresh(serial=serial)
        before_clock=self.c.success(self.c.request(self.c.core['clock']))
        for detail in range(5):self.a.mark_external(7,detail)
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
        require([row[0] for row in seen]==[(serial+i)&0xffffffff for i in range(5)] and all(row[2]==7 for row in seen),
                'mark serial/kind differ from stimulus')
        require(all(lower<=row[3]<=upper for row in seen),'mark timestamp outside independent clock interval')
        require([row[1] for row in seen]==[0]*5 and [row[4] for row in seen]==list(range(5)),'same-position marks dropped')
        require(all(a[3]<=b[3] for a,b in zip(seen,seen[1:])),'mark timestamps decreased')
        require(self.marks(serial)[1]==seen[:len(self.marks(serial)[1])],'marks not repeatable')

    def lost_marks(self):
        self.fresh(serial=100)
        for detail in range(8):self.a.mark_external(7,detail)
        expected=self.a.stream_state()['marks'];require(len(expected)==5 and expected[0][0]==103,'controlled ring eviction')
        for cursor in (100,99,109):
            more,rows=self.marks(cursor);require(more and rows==expected[:len(rows)] and rows,'unretained serial must fall back to oldest')
        more,rows=self.marks(104);require(more and rows[0][0]==104,'retained serial must be inclusive')
        require(self.marks(108)==(0,[]),'next serial must be empty')
        self.expect_read((0,0,b''),mode=3,arg=1)

    def mark_evicted(self):
        self.fresh();self.a.feed_bytes(b'ab');self.a.mark_external(1,1);self.a.feed_bytes(b'cd')
        for detail in range(5):self.a.mark_external(7,detail)
        self.expect_read((4,0,b''),mode=3,arg=1)
        self.expect_read((0,0,b'abcd'))

    def mark_gap(self):
        self.fresh();self.a.mark_external(7,1);data=bytes(range(80));self.a.feed_bytes(data)
        self.expect_read((16,3,data[16:20]),mode=3,arg=7,maximum=4)

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
        more,rows=self.marks(0);require(not more and len(rows)==1 and rows[0][1:3]==(3,6),'clear mark')

    def persistent(self):
        self.fresh();self.a.feed_bytes(b'abc')
        with self.c.holding(3000) as sid:
            request=self.request('mark',b'\x37',sid);frame=self.c.exchange(request);self.c.success(request)
            require(frame[3:5]==b'\x01\0','host mark success');tlvs(frame[5:])
            state=self.a.stream_state()
            require(len(state['marks'])==1 and state['marks'][0][1:3]==(3,7) and state['marks'][0][4]==0x37,'host mark/replay')
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
                                  ('read_flags',{'more':1,'gap':2}),('mark_kind',{'reset':1,'lost':5,'clear':6,'host':7}),
                                  ('mark_detail_reset',{'ndmreset':1}),('mark_detail_lost',{'overflow':1})]:
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
                          ('EVICTION',self.lost_marks),('MARK-EVICTED',self.mark_evicted),('MARK-GAP',self.mark_gap),
                          ('LIMIT',self.limit),('CLEAR',self.clear),('PERSISTENT',self.persistent),('REFUSALS',self.locks)]:
            first=len(self.a.trace);self.c.check('IF-STREAM-'+name,'common §1.1–1.4; core §2.6/5.2/7.3',case)
            self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        checks=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in checks) else 'failed','full_conformance':False,
                'scope':'explicit persistent-fn positioned stream read/marks','levels':{'core':'session/replay prerequisites',
                'interface':'selected adopted positioned-stream contracts','oep-interface':'not executed'},
                'checks':checks,'observed':self.c.observed,'adapter_trace':self.a.trace,
                'unchecked':['resource-addressed stream lifecycle','write delivery/partial/failed outcomes','notification data',
                             'physical UART/reset','status/done debug component','other ring/frame limits','concurrent producers']}
