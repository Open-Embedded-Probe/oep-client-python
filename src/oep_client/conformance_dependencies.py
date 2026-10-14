"""Independent adopted resource-addressed stream/connection lifetime checks."""
import secrets
import struct
import time
from .conformance import require, ops, tlvs


class DependencyChecks:
    def __init__(self, checks, adapter, *, wait=time.sleep):self.c,self.a,self.wait=checks,adapter,wait

    def request(self, group, action, sid=0, payload=b''):
        a=self.a.adoption[group]
        return self.c.request(a['ops'][action],payload,session=sid,fn=a['fn'])

    def empty(self, request):
        tlvs(self.c.success(request))

    def fresh(self):
        result=self.a.restart_fixture()
        require(result['scope']=='logical-model' and result['boot_id']!=self.c.boot,'fresh logical boot required')
        self.c.confirm();self.c.identity();require(result['boot_id']==self.c.boot,'boot readback')

    def allocate(self, group, sid, parent=0):
        req=self.request(group,'attach' if group=='connection' else 'open',sid,b'' if group=='connection' else struct.pack('<H',parent))
        value=self.c.success(req);require(len(value)>=2,'created ID missing');tlvs(value[2:])
        rid=struct.unpack_from('<H',value)[0];require(rid!=0,'zero resource ID')
        return rid,req

    def read(self, rid, sid=0):
        req=self.request('stream','read',sid,struct.pack('<HBQH',rid,1,0,32));value=self.c.success(req)
        require(len(value)>=11,'read shape');start,flags,size=struct.unpack_from('<QBH',value)
        require(size<=32 and len(value)>=11+size,'read size');tlvs(value[11+size:])
        return (start,flags,value[11:11+size]),req,value

    def listing(self, group):
        result=[];first=0
        for _ in range(9):
            value=self.c.success(self.request(group,'list',payload=struct.pack('<H',first)))
            require(len(value)>=2 and value[0] in (0,1),'resource list header')
            more,count=value[:2];width=2 if group=='connection' else 4
            require(len(value)>=2+width*count,'list count');tlvs(value[2+width*count:])
            rows=[struct.unpack_from('<H' if width==2 else '<HH',value,2+i*width) for i in range(count)]
            result.extend(rows)
            if not more:return result
            require(count>0,'list page made no progress');first+=count
        raise AssertionError('list failed to terminate')

    def visible(self):
        state=self.a.dependency_state()
        require(self.listing('connection')==[(rid,) for rid in state['connections']],'connection wire inventory differs')
        require(self.listing('stream')==[(rid,s['connection']) for rid,s in state['streams'].items()],'stream wire inventory differs')
        return state

    def cause(self, req):
        require(dict(reversed(tlvs(self.c.rejected(req,'unavailable')))).get(1)==b'\x06','wrong resource kind cause')

    def missing(self, rid, sid):
        for action,payload in [('read',struct.pack('<HBQH',rid,1,0,1)),('marks',struct.pack('<HI',rid,0)),
                               ('clear',struct.pack('<H',rid)),('mark',struct.pack('<HB',rid,1)),
                               ('write',struct.pack('<HH',rid,1)+b'x'),('close',struct.pack('<H',rid))]:
            self.c.rejected(self.request('stream',action,0 if action in ('read','marks') else sid,payload),'no_resource')

    def populate(self, sid):
        parent,_=self.allocate('connection',sid);child,_=self.allocate('stream',sid,parent)
        self.a.feed_bytes(child,b'abc')
        accepted=self.c.success(self.request('stream','write',sid,struct.pack('<HH',child,3)+b'xyz'))
        require(len(accepted)>=2 and struct.unpack_from('<H',accepted)[0]==3,'resource write acceptance');tlvs(accepted[2:])
        state=self.visible()['streams'][child]
        require(state['position']==3 and state['data']==b'abc' and state['tx']==b'xyz','populate resource state')
        return parent,child

    def basic(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            p,s=self.populate(sid)
            if kind=='ADDRESS':
                p2,_=self.allocate('connection',sid);s2,_=self.allocate('stream',sid,p2);self.a.feed_bytes(s2,b'def')
                before=self.visible()
                require(self.read(s)[0]==(0,0,b'abc') and self.read(s2)[0]==(0,0,b'def'),'stream addressing/nonconsuming')
                require(self.visible()==before,'read consumed or mutated state')
                ids=list(before['connections'])+list(before['streams']);require(len(set(ids))==4,'global resource ID space')
            elif kind=='CLEAR':
                self.empty(self.request('stream','clear',sid,struct.pack('<H',s)))
                state=self.visible()['streams'][s]
                require(state['position']==3 and state['data']==b'' and state['tx']==b'xyz' and state['marks'][-1][2]==2,'clear destroyed queue/position or missed mark')
            elif kind=='CLOSE':
                self.empty(self.request('stream','close',sid,struct.pack('<H',s)))
                state=self.visible();require(p in state['connections'] and not state['streams'],'stream close released parent or retained buffers')
                self.missing(s,sid);fresh,_=self.allocate('stream',sid,p);new=self.visible()['streams'][fresh]
                require(fresh>s and new['position']==0 and new['next_serial']==0 and not new['data'] and not new['marks'] and not new['tx'],'recreated stream reused state/ID')
            elif kind=='WRONG-KIND':
                before=self.visible()
                self.cause(self.request('stream','read',payload=struct.pack('<HBQH',p,1,0,1)))
                self.cause(self.request('connection','use',sid,struct.pack('<H',s)))
                self.cause(self.request('stream','open',sid,struct.pack('<H',s)))
                require(self.visible()==before,'wrong kind changed resources')
            elif kind=='UNKNOWN':
                before=self.visible();self.missing(65535,sid)
                self.c.rejected(self.request('stream','open',sid,struct.pack('<H',65535)),'no_resource')
                require(self.visible()==before,'missing resource changed state')
            else:
                before=self.visible();self.a.reset_target(p);after=self.visible()
                require(after['connections']==before['connections'] and set(after['streams'])=={s},'target restart released resources')
                a,b=after['streams'][s],before['streams'][s]
                require(all(a[key]==b[key] for key in ('data','position','tx')) and a['marks'][-1][2]==0x41,'restart discarded stream contents or missed mark')

    def marks(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            p,s=self.populate(sid);other,_=self.allocate('stream',sid,p)
            for detail in range(4):
                req=self.request('stream','mark',sid,struct.pack('<HB',s,detail));self.empty(req)
                stable=self.visible();self.empty(req);require(self.visible()==stable,'replayed mark duplicated')
            before=self.visible();expected=before['streams'][s]['marks'];rows=[];serial=0
            for _ in range(4):
                value=self.c.success(self.request('stream','marks',payload=struct.pack('<HI',s,serial)))
                require(len(value)>=2 and value[0] in (0,1),'marks header');more,count=value[:2]
                require(len(value)>=2+22*count,'marks size');tlvs(value[2+22*count:])
                rows += [struct.unpack_from('<IQBQB',value,2+22*i) for i in range(count)]
                if not more:break
                require(count>0,'marks made no progress');serial=(rows[-1][0]+1)&0xffffffff
            require(rows==expected and [row[4] for row in rows]==list(range(4)),'resource mark routing/serial/detail')
            value=self.c.success(self.request('stream','marks',payload=struct.pack('<HI',other,0)))
            require(value[:2]==b'\0\0','marks leaked to other stream');tlvs(value[2:])
            require(self.visible()==before,'marks consumed or changed resources')

    def refusal(self, malformed=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            p,s=self.populate(sid);before=self.visible()
            if malformed:
                for group,action,payload in [('stream','read',struct.pack('<H',s)),('stream','marks',struct.pack('<H',s)),
                                             ('stream','write',struct.pack('<HH',s,4)+b'ab'),('connection','detach',b'')]:
                    self.c.rejected(self.request(group,action,sid,payload),'malformed')
            else:
                foreign=sid%0xffffffff+1
                for group,action,payload in [('stream','close',struct.pack('<H',s)),('connection','detach',struct.pack('<H',p)),
                                             ('stream','write',struct.pack('<HH',s,1)+b'x')]:
                    self.c.rejected(self.request(group,action,foreign,payload),'locked')
            require(self.visible()==before,'refused request changed dependencies or buffers')

    def release_order(self, before, after, parents):
        new=after['events'][len(before['events']):]
        expected=[('stream',rid) for rid,s in before['streams'].items() if s['connection'] in parents]
        expected += [('connection',rid) for rid in before['connections'] if rid in parents]
        require(len(new)==len(expected) and set(map(tuple,new))==set(expected),'release omitted/duplicated resource')
        for parent in parents:
            for rid,s in before['streams'].items():
                if s['connection']==parent:require(new.index(('stream',rid))<new.index(('connection',parent)),'parent released before child')
        require(all(rid not in after['streams'] for kind,rid in expected if kind=='stream') and
                all(rid not in after['connections'] for kind,rid in expected if kind=='connection'),'released resource remains live')

    def detach(self, external=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            p,s=self.populate(sid);s2,_=self.allocate('stream',sid,p)
            p2,_=self.allocate('connection',sid);other,_=self.allocate('stream',sid,p2);self.a.feed_bytes(other,b'other')
            before=self.visible()
            if external:self.a.break_connection(p)
            else:self.empty(self.request('connection','detach',sid,struct.pack('<H',p)))
            after=self.visible();self.release_order(before,after,{p})
            require(after['streams'][other]==before['streams'][other] and after['connections']==[p2],'detach affected unrelated connection')
            self.missing(s,sid);self.missing(s2,sid)
            self.c.rejected(self.request('connection','use',sid,struct.pack('<H',p)),'no_resource')

    def replay(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            p,_=self.allocate('connection',sid);s,opened=self.allocate('stream',sid,p)
            self.a.feed_bytes(s,b'old');_,read,old=self.read(s,sid)
            closed=self.request('stream','close',sid,struct.pack('<H',s));self.empty(closed)
            fresh,_=self.allocate('stream',sid,p);self.a.feed_bytes(fresh,b'new');before=self.visible()
            require(self.c.success(opened)[:2]==struct.pack('<H',s),'replayed open changed ID')
            require(self.c.success(read)==old,'replayed read lost historical bytes')
            self.empty(closed);require(self.visible()==before,'replay resurrected resource or closed replacement')
            self.c.rejected(self.request('stream','read',payload=struct.pack('<HBQH',s,1,0,1)),'no_resource')

    def detach_replay(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            p,s=self.populate(sid);req=self.request('connection','detach',sid,struct.pack('<H',p));self.empty(req)
            new,child=self.populate(sid);before=self.visible();self.empty(req)
            require(self.visible()==before and new>p and child>s,'detach replay affected replacement')

    def same_session(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            self.populate(sid);before=self.visible()
            self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',3000,0),session=sid))
            require(self.visible()==before,'same-session open destroyed stream/connection')

    def ending(self, kind):
        self.fresh()
        with self.c.holding(1000 if kind=='EXPIRY' else 3000) as sid:
            p,s=self.populate(sid);_,read,old=self.read(s,sid);before=self.visible()
            if kind=='END':self.c.success(self.c.request(self.c.core['end'],session=sid));self.c.session=None
            elif kind=='EXPIRY':
                self.wait(1.1);require(self.c.success(self.c.request(self.c.core['lock_state']))[:5]==b'\0'*5,'lease did not expire');self.c.session=None
            else:
                new=sid%0xffffffff+1;self.c.corr=0
                self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',3000,1),session=new));self.c.session=new
            after=self.visible();self.release_order(before,after,{p});require(not after['connections'] and not after['streams'],'session retained resources')
            if kind!='FORCE':
                require(self.c.success(read)==old and self.visible()==after,'historical read resurrected ended resources')
                self.c.rejected(self.request('connection','use',sid,struct.pack('<H',p)),'no_session')
                new=secrets.randbelow(0xffffffff)+1
                while new==sid:new=secrets.randbelow(0xffffffff)+1
                self.c.corr=0;self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',3000,0),session=new));self.c.session=new
            self.missing(s,self.c.session)
            fresh,_=self.allocate('connection',self.c.session);require(fresh>s,'new session reused old ID')

    def locks(self):
        self.fresh();before=self.visible()
        for group,action,payload in [('connection','attach',b''),('connection','detach',b'\0\0'),('connection','use',b'\0\0'),
                                     ('stream','open',b'\0\0'),('stream','clear',b'\0\0'),('stream','mark',b'\0\0\0'),
                                     ('stream','write',b'\0\0\0\0'),('stream','close',b'\0\0')]:
            self.c.rejected(self.request(group,action,payload=payload),'session_required')
        require(self.visible()==before,'unlocked mutation changed resources')

    def run(self):
        start=len(self.c.results)
        def identify():
            a=self.a.adoption
            require(a['connection']['component']=='debug-connection-v1' and a['stream']['component']=='positioned-stream-v1' and
                    a['stream']['addressing']=='resource' and a['reference'] and a['tx_capacity']==4 and a['max_resources']==8,'explicit dependency adoption/geometry')
            require(all(callable(getattr(self.a,key,None)) for key in ('restart_fixture','feed_bytes','reset_target','break_connection','dependency_state')),'adapter controls required')
            require(self.a.reset_scope=='logical-model' and a['connection']['fn']!=a['stream']['fn'],'logical resource fixture required')
            require(self.c.reg['common']['enum']['mark_kind']['clear']==6 and self.c.reg['common']['enum']['mark_kind']['restart']==2 and
                    self.c.reg['common']['enum']['mark_detail_restart']['havereset']==1,'selected SPEC mark mapping differs')
            needed_ops={'connection':{'attach','detach','use','list'},'stream':{'open','read','marks','clear','mark','write','close','list'}}
            require(all(set(a[group]['ops'])==needed and len(set(a[group]['ops'].values()))==len(needed) for group,needed in needed_ops.items()),'adopted operation mapping incomplete/aliased')
            self.c.confirm();self.c.identity();self.c.declarations();listed=self.c.list_snapshot()
            for group,needed in [('connection',{'attach','detach','use','list'}),('stream',{'open','read','marks','clear','mark','write','close','list'})]:
                adopted=a[group];require(set(adopted['ops'])==needed and any(row['fn']==adopted['fn'] for row in listed),'adopted fn/ops absent')
                offered=ops(dict(reversed(self.c.describe(adopted['fn'])))[self.c.reg['describe_common']['ops']])
                require(set(adopted['ops'].values())<=offered,'adopted op not offered')
            self.c.observed['dependency_adoption']=a
        self.c.check('IF-DEPENDENCY-CONTEXT','common §1/2; explicit sample adoption',identify)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[(kind,lambda kind=kind:self.basic(kind)) for kind in ('ADDRESS','CLEAR','CLOSE','WRONG-KIND','UNKNOWN','RESTART')]
        cases += [('MARKS',self.marks),('MALFORMED',lambda:self.refusal(True)),('OWNER',self.refusal),('DETACH',self.detach),('BREAK',lambda:self.detach(True)),('REPLAY',self.replay),('DETACH-REPLAY',self.detach_replay),('SAME-SESSION',self.same_session)]
        cases += [(kind,lambda kind=kind:self.ending(kind)) for kind in ('END','EXPIRY','FORCE')]+[('LOCKS',self.locks)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('IF-DEPENDENCY-'+name,'common §1/2; core §5.2/9',case)
            self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return {'status':'passed' if all(r['status']=='passed' for r in rows) else 'failed','full_conformance':False,
                'scope':'adopted resource-addressed stream and connection lifetime on explicit sample',
                'levels':{'core':'resource/session/replay prerequisites','interface':'adopted dependency lifetime','oep-interface':'not executed'},
                'checks':rows,'observed':self.c.observed,'adapter_trace':self.a.trace,
                'unchecked':['physical release/pin idle','line-loss decision and reply-before-close','standard mechanism restrictions',
                             'target debug operations','notification data/closed mark delivery','production Host integration','other capacity/concurrent writers']}
