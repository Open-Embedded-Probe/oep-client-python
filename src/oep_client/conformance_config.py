"""Current config get/set/unset/save/erase/state, the change generation and idle/disable effects on live pins."""
import struct
from .conformance import require,tlvs
from .conformance_registered_ops import RegisteredOpsChecks


def item(tag,value):return bytes((tag,))+len(value).to_bytes(2,'little')+value
def setp(*items):return bytes((len(items),))+b''.join(items)                       # set: count(u8) items [TLV]
def unsetp(*keys):return bytes((len(keys),))+b''.join(struct.pack('<BH',t,ch) for t,ch in keys)   # unset: count(u8) (tag channel)


def split_items(data,count):
    """count items tag(u8) len(u16) value of an answer (tag bit7 clear, number nonzero) -> (items, the answer's TLV tail)."""
    rows=[]
    for _ in range(count):
        require(len(data)>=3,'truncated config item header');tag,n=struct.unpack_from('<BH',data)
        require(tag&127!=0,'config item number zero');require(tag<128,'config item tag bit7 set in answer')
        require(len(data)>=3+n,'truncated config item value');rows.append((tag,bytes(data[3:3+n])));data=data[3+n:]
    return rows,data


def page(p):
    """A get answer -> (more, generation, items); the answer's own TLV tail is decoded apart from the items."""
    require(len(p)>=6 and p[0] in (0,1),'config get fixed shape/more');more,gen,count=struct.unpack_from('<BIB',p)
    rows,rest=split_items(p[6:],count);tlvs(rest);return more,gen,rows


class ConfigChecks:
    cases=('PERSISTENCE','IDLE','ATOMIC','GENERATION','STORAGE-LIMIT')
    channels=8
    def __init__(self,c,a):self.c,self.a=c,a;self.observations=[]
    def op(self,op,payload=b'',sid=0):return self.c.request(op,payload,fn=4,session=sid)
    def observe(self,name):
        s=self.a.state();self.observations.append(dict(action=name,state=s));return s
    def settled(self):
        s=self.a.state();return {k:s[k] for k in ('items','generation','storage','saved','gpio')}
    def changed(self,op,payload,sid):
        p=self.c.success(self.op(op,payload,sid));require(len(p)>=4,'config generation fixed shape');tlvs(p[4:]);return int.from_bytes(p[:4],'little')
    def set(self,sid,*items):return self.changed(17,setp(*items),sid)
    def unset(self,sid,*keys):return self.changed(20,unsetp(*keys),sid)
    def end(self,sid):tlvs(self.c.success(self.c.request(self.c.core['end'],session=sid)));self.c.session=None
    def get(self,expected):
        more,gen,rows=page(self.c.success(self.op(16,b'\0\0')));require(more==0,'selected config fits one page')
        require(rows==expected,'config items/order/bit7 differ');return gen
    def storage(self,state,saved,reason=0):
        p=self.c.success(self.op(21));require(len(p)>=6 and p[:6]==struct.pack('<BIB',state,saved,reason),'config storage_state/saved_generation/reason differ');tlvs(p[6:])
    def unavailable(self,request,cause,ch=None):
        rows=tlvs(self.c.rejected(request,'unavailable'));require(dict(reversed(rows)).get(1)==bytes((cause,)),'config unavailable cause differs')
        # core §4.3: the channel TLV is attached as far as the probe knows it; when present it names the clashing channel.
        require(ch is None or all(v==struct.pack('<H',ch) for t,v in rows if t==2),'config unavailable channel differs')
    def context(self):
        for name in ('state','seed','restart_fixture','generation_at'):require(callable(getattr(self.a,name,None)),'explicit config controls required')
        self.c.confirm();self.c.identity();desc=dict(self.c.describe(4))
        require(desc.get(64)==struct.pack('<I',64) and desc.get(65)==b'\2\3\7','explicit config storage/items profile differs')
        require(dict(self.c.describe(0)).get(67)==struct.pack('<H',self.channels),'explicit config channel profile differs')
    def case(self,name):
        fresh=self.a.restart_fixture();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh config fixture')
        label,idle=(2,b'\4\0nrst'),(3,b'\1\0\4')
        with self.c.holding(60000) as sid:
            if name=='PERSISTENCE':
                require(self.get([])==1,'fresh boot generation is not 1');self.storage(0,0)
                g=self.set(sid,item(130,label[1]),item(131,idle[1]));require(g==2,'first change did not count generation 1 to 2')
                require(self.get([label,idle])==g,'set/get generation differs (or item bit7 not ignored)')
                require(self.set(sid,item(3,idle[1]))==g,'same contents changed the generation')
                require(self.changed(18,b'',sid)==g,'save generation differs');self.storage(1,g)
                self.observe('saved')
                require(self.set(sid,item(2,b'\4\0other'))==g+1,'changed contents did not count the generation by one');self.storage(1,g)
                self.end(sid)
                self.a.restart_fixture();self.c.confirm();self.c.identity()
                require(self.get([label,idle])==1,'restart did not apply the saved settings at generation 1');self.storage(1,1)
                s=self.a.state()['gpio'];require(s['plans']=={} and s['pins'][1]==4,'saved idle boot differs')
                self.observe('restored')
                with self.c.holding() as new:
                    before=self.settled();require(self.c.success(self.op(19,sid=new))==b'','erase response')
                    after=self.settled();require(after['items']==before['items'] and after['generation']==before['generation'] and after['gpio']==before['gpio'],'erase changed current config/generation/pins')
                    self.storage(0,0);self.observe('erased')
                self.a.restart_fixture();self.c.confirm();self.c.identity();require(self.get([])==1,'erased settings came back');self.storage(0,0)
            elif name=='IDLE':
                self.a.seed(self.c,sid);live=self.a.state()['gpio'];require(live['plans']=={2:[1]} and live['pins'][1]==4,'live seed')
                self.set(sid,item(3,b'\1\0\2'));require(self.a.state()['gpio']['pins'][1]==4,'idle immediately changed a channel in use')
                self.set(sid,item(3,b'\2\0\1'));require(self.a.state()['gpio']['pins'][2]==1,'idle of a free channel did not take effect at once')
                require(self.c.success(self.c.request(17,b'\1\2\0',fn=1,session=sid))==b'','release plan')
                require(self.a.state()['gpio']['pins'][1]==2,'release did not apply configured idle')
                self.set(sid,item(7,b'\3\0'));require(self.a.state()['gpio']['pins'][3] is None,'disable left the channel driven')
                self.unavailable(self.c.request(16,item(144,b'\3\0\1\3\0'),fn=1,session=sid),5,3)
                self.unset(sid,(7,3));require(self.a.state()['gpio']['pins'][3]==0,'disable unset did not re-park the channel at once')
                require(self.c.success(self.c.request(16,item(144,b'\3\0\1\3\0'),fn=1,session=sid))==b'','enabled channel unusable')
                require(self.c.success(self.c.request(17,b'\1\3\0',fn=1,session=sid))==b'','release plan')
                self.observe('idle-disable')
            elif name=='ATOMIC':
                g=self.set(sid,item(2,label[1]),item(3,idle[1]));before=self.settled()
                refusals=((17,setp(item(2,b'\4\0a'),item(2,b'\4\0b')),'malformed',None),(17,setp(item(130,b'\4\0a'),item(2,b'\4\0b')),'malformed',None),
                          (17,setp(item(3,b'\1\0')),'malformed',None),(17,setp(item(3,b'\1\0\1\0')),'malformed',None),(17,setp(item(7,b'\1\0\0')),'malformed',None),
                          (17,setp(item(2,b'\4\0')),'malformed',None),(17,setp(item(2,b'\4\0'+b'a'*33)),'malformed',None),(17,setp(item(128,b'\4\0')),'malformed',None),
                          (17,b'\2'+item(2,b'\4\0a'),'malformed',None),(17,b'','malformed',None),
                          (17,setp(item(255,b'')),'unsupported',b'\xff'),(17,setp(item(1,b'\2\0\1\3\0')),'unsupported',b'\1'),
                          (17,setp(item(2,b'\x08\0x')),'unsupported',b'\2'),(17,setp(item(3,b'\1\0\5')),'unsupported',b'\3'),
                          (17,setp(item(2,b'\4\0a'))+item(0xc0,b''),'unsupported',b'\xc0'),
                          (20,unsetp((2,4),(130,4)),'malformed',None),(20,unsetp((128,4)),'malformed',None),(20,b'\2'+unsetp((2,4))[1:],'malformed',None),
                          (20,unsetp((5,1)),'unsupported',b'\5'))
                for op,payload,reason,tag in refusals:
                    p=self.c.rejected(self.op(op,payload,sid),reason)
                    if tag is not None:require(p[:1]==tag,'config unsupported item tag differs')
                    require(self.settled()==before,'refused config changed settings/generation/resources')
                require(self.set(sid)==g and self.unset(sid)==g and self.unset(sid,(7,5))==g,'no-op set/unset changed the generation')
                require(self.changed(17,setp(item(2,label[1]))+item(0x40,b'x'),sid)==g,'unknown noncritical request TLV not ignored apart from items')
                require(self.settled()==before,'no-op set/unset changed settings/resources')
                self.unavailable(self.op(17,setp(item(7,b'\1\0')),sid),5,1);require(self.settled()==before,'idle/disable refusal changed settings')
                self.unavailable(self.op(17,setp(item(3,b'\5\0\1'),item(7,b'\5\0')),sid),5,5);require(self.settled()==before,'idle/disable refusal changed settings')
                self.a.seed(self.c,sid);self.unset(sid,(3,1));before=self.settled()
                self.unavailable(self.op(17,setp(item(7,b'\1\0')),sid),1,1);require(self.settled()==before,'disabled active pin refusal changed settings')
                self.observe('atomic-refusals')
            elif name=='GENERATION':
                require(self.get([])==1,'fresh boot generation is not 1')
                require(self.set(sid,item(2,b'\4\0a'))==2,'first change did not count to 2')
                require(self.unset(sid,(3,4))==2,'unset of a missing key changed the generation')
                require(self.unset(sid,(2,4))==3 and self.get([])==3,'unset did not count the generation')
                self.a.generation_at(0xfffffffe)
                require(self.set(sid,item(2,b'\4\0b'))==0xffffffff,'generation did not reach 0xFFFFFFFF');before=self.settled()
                self.unavailable(self.op(17,setp(item(2,b'\4\0c')),sid),2);self.unavailable(self.op(20,unsetp((2,4)),sid),2)
                require(self.settled()==before,'refused change at the last generation changed settings')
                require(self.set(sid,item(2,b'\4\0b'))==0xffffffff and self.unset(sid,(7,6))==0xffffffff,'same contents refused at the last generation')
                self.end(sid);self.a.restart_fixture();self.c.confirm();self.c.identity()
                require(self.get([])==1,'restart did not start the generation at 1')
                self.observe('generation')
            else:
                self.set(sid,item(2,b'\2\0'+b'a'*32),item(2,b'\3\0'+b'b'*32));before=self.settled()
                self.unavailable(self.op(18,sid=sid),3)
                require(self.settled()==before,'failed save changed current/saved state');self.storage(0,0)
                self.observe('storage-limit')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-CONTEXT','config §1/2/3/4',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-'+name,'config §1/2/3',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,state_observations=self.observations,
                    unchecked=['get paging','saved item fallback at boot','power loss atomic storage/save execution timing',
                               'pull/output idle refused per channel and the firmware\'s own channels','physical persistence and production implementation'])
