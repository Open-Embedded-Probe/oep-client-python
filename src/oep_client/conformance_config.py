"""Current config get/set/unset/save/erase/state and live-plan separation."""
import struct
from .conformance import require,tlvs
from .conformance_registered_ops import RegisteredOpsChecks


def item(tag,value):return bytes((tag,))+len(value).to_bytes(2,'little')+value


class ConfigChecks:
    cases=('PERSISTENCE','PRESET','ATOMIC','STORAGE-LIMIT')
    def __init__(self,c,a):self.c,self.a=c,a;self.observations=[]
    def op(self,op,payload=b'',sid=0):return self.c.request(op,payload,fn=4,session=sid)
    def observe(self,name):
        s=self.a.state();self.observations.append(dict(action=name,state=s));return s
    def hashed(self,op,payload,sid):
        p=self.c.success(self.op(op,payload,sid));require(len(p)>=4,'config hash fixed shape');tlvs(p[4:]);return int.from_bytes(p[:4],'little')
    def get(self,expected):
        p=self.c.success(self.op(16,b'\0\0'));require(len(p)>=5 and p[0]==0,'selected config page shape')
        require(tlvs(p[5:])==expected,'config items/order/critical stripping differ');return int.from_bytes(p[1:5],'little')
    def storage(self,state,h):
        p=self.c.success(self.op(21));require(len(p)>=6 and p[:6]==struct.pack('<BIB',state,h,0),'config storage state/hash/reason differ');tlvs(p[6:])
    def context(self):
        for name in ('state','seed','restart_fixture'):require(callable(getattr(self.a,name,None)),'explicit config controls required')
        self.c.confirm();self.c.identity();desc=dict(self.c.describe(4))
        require(desc[64]==struct.pack('<I',64) and desc[65]==b'\1\2\3\7' and desc[66]==b'\0','explicit config item/storage profile differs')
    def case(self,name):
        fresh=self.a.restart_fixture();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh config fixture')
        with self.c.holding(60000) as sid:
            label=item(130,b'\4\0nrst');idle=item(131,b'\1\0\4\0')
            if name=='PERSISTENCE':
                h=self.hashed(17,label+idle,sid);require(self.get([(2,b'\4\0nrst'),(3,b'\1\0\4\0')])==h,'set/get hash differs');self.storage(0,0)
                require(self.hashed(18,b'',sid)==h,'save hash differs');self.storage(1,h)
                saved=self.observe('saved')
                changed=self.hashed(17,item(2,b'\4\0other'),sid);require(changed!=h,'changed config hash unchanged');self.storage(1,h)
                tlvs(self.c.success(self.c.request(self.c.core['end'],session=sid)));self.c.session=None
                self.a.restart_fixture();self.c.confirm();self.c.identity()
                require(self.get([(2,b'\4\0nrst'),(3,b'\1\0\4\0')])==h,'unsaved settings survived restart');self.storage(1,h)
                require(self.a.state()['gpio']['plans']=={} and self.a.state()['gpio']['pins'][1]==(4,0),'saved idle/preset boot differs')
                self.observe('restored')
                with self.c.holding() as new:
                    before=self.a.state();require(self.c.success(self.op(19,sid=new))==b'','erase response')
                    require(self.a.state()['items']==before['items'] and self.a.state()['gpio']==before['gpio'],'erase changed current config/pins')
                    self.storage(0,0);self.observe('erased')
                self.a.restart_fixture();self.c.confirm();self.c.identity();self.get([]);self.storage(0,0)
            elif name=='PRESET':
                self.a.seed(self.c,sid);live=self.a.state()['gpio'];require(live['plans']=={2:[1]},'live seed')
                self.hashed(17,item(129,b'\2\0\1\3\0'),sid)
                require(self.a.state()['gpio']==live,'preset changed live plan/pin')
                self.hashed(20,b'\1\2\1\2\0',sid);require(self.a.state()['gpio']==live,'unset preset changed live plan')
                self.hashed(17,idle,sid);require(self.a.state()['gpio']['pins']==live['pins'],'idle immediately changed owned pin')
                require(self.c.success(self.c.request(17,b'\1\2\0',fn=1,session=sid))==b'','release plan')
                require(self.a.state()['gpio']['pins'][1]==(4,0),'release did not apply configured idle')
                self.observe('preset-separated')
            elif name=='ATOMIC':
                self.hashed(17,label+idle,sid);before=self.a.state()
                for payload,reason in ((item(2,b'\4\0a')+item(2,b'\4\0b'),'malformed'),(item(7,b'\1\0'),'malformed'),(item(2,b'\4'),'malformed'),(item(255,b''),'unsupported')):
                    p=self.c.rejected(self.op(17,payload,sid),reason)
                    if reason=='unsupported':require(p==b'\xff','config unsupported raw tag differs')
                    require(self.a.state()==before,'refused config changed settings/resources')
                self.a.seed(self.c,sid);before=self.a.state()
                self.hashed(20,b'\1\2\3\1\0',sid);before=self.a.state()
                p=self.c.rejected(self.op(17,item(7,b'\1\0'),sid),'unavailable');require(p==b'\1\1\0\1' and self.a.state()==before,'disabled active pin refusal differs')
                self.observe('atomic-refusals')
            else:
                self.hashed(17,item(2,b'\2\0'+b'a'*32)+item(2,b'\3\0'+b'b'*32),sid);before=self.a.state()
                p=self.c.rejected(self.op(18,sid=sid),'unavailable');require(p==b'\1\1\0\3','storage full cause differs')
                require(self.a.state()==before,'failed save changed current/saved state');self.storage(0,0)
                self.observe('storage-limit')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-CONTEXT','config §1/2/3/4',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-'+name,'config §1/2/3; plan §2.3',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,state_observations=self.observations,
                    unchecked=['all config items, numeric key ordering and get paging','saved identity remapping/unreadable fallback','power loss atomic storage/save execution timing','wifi and target integrations','physical persistence and production implementation'])
