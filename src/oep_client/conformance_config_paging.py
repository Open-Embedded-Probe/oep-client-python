"""Config pages, numeric keys, independent first offsets and changing hashes."""
import struct
from .conformance import require,tlvs
from .conformance_config import ConfigChecks,item
from .conformance_registered_ops import RegisteredOpsChecks


CHANNELS=(0,1,2,3,4,7,8,255,256,257,300,511)
LABELS=[(2,struct.pack('<H',ch)+bytes((97+i,))*(22 if i<2 else 32)) for i,ch in enumerate(CHANNELS)]


class ConfigPagingChecks(ConfigChecks):
    cases=('EMPTY','DENSE','FIRST','CHANGED','READONLY')
    def __init__(self,c,a):super().__init__(c,a);self.pages=[];self.host_reads=[]
    def context(self):
        super().context()
        for name in ('prepare','read_all'):require(callable(getattr(self.a,name,None)),'explicit config paging controls required')
        require(self.c.max_frame in (64,128,256),'explicit config paging frame profile')
        require(dict(self.c.describe(0))[67]==struct.pack('<H',512),'explicit 512 label channel profile')
    def page(self,first,expected_hash=None,expected=None):
        p=self.c.success(self.op(16,struct.pack('<H',first)))
        require(len(p)>=5 and p[0] in (0,1),'config page fixed shape/more')
        h=int.from_bytes(p[1:5],'little');rows=tlvs(p[5:])
        if expected_hash is not None:require(h==expected_hash,'stable config pages changed hash')
        if expected is not None:
            require(rows==expected[first:first+len(rows)],'config page differs from numeric item offset')
            require(p[0]==int(first+len(rows)<len(expected)),'config page more differs from remaining items')
            require(not p[0] or rows,'config paging made no progress')
        self.pages.append(dict(first=first,more=p[0],hash=h,items=rows,payload_hex=p.hex(),result_length=len(p)+5))
        return p
    def read(self,expected,h,source=None):
        start=len(self.pages)
        result=self.a.read_all(source or (lambda first:self.page(first,h,expected)))
        require(result['hash']==h and result['items']==expected,'Host config read mixed/lost/duplicated items')
        self.host_reads.append(result)
        return self.pages[start:]
    def seed_labels(self,sid):
        h=None
        for t,v in reversed(LABELS):h=self.hashed(17,item(t|128,v),sid)
        return h
    def case(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh paging fixture')
        if name=='EMPTY':
            first=self.page(0,expected=[]);h=int.from_bytes(first[1:5],'little')
            self.page(1,h,[]);self.page(65535,h,[]);self.read([],h);self.observe('empty');return
        with self.c.holding(60000) as sid:
            h=self.seed_labels(sid)
            if name=='DENSE':
                pages=self.read(LABELS,h)
                require(len(pages)>1 and all(p['more']==1 for p in pages[:-1]) and pages[-1]['more']==0,'dense paging fixture did not span pages')
                if self.c.max_frame==64:require(pages[0]['result_length']==64,'exact-fit page boundary fixture differs')
                self.observe('dense')
            elif name=='FIRST':
                presets=[(1,b'\2\0\1\1\0'),(1,b'\2\0\1\2\0'),(1,b'\3\0\1\4\0')]
                self.hashed(17,b''.join(item(t|128,v) for t,v in presets[:2]),sid)
                self.hashed(17,item(129,presets[2][1]),sid)
                self.hashed(17,item(131,b'\3\0\1\0'),sid)
                h=self.hashed(17,item(135,b'\7\0'),sid)
                expected=presets+LABELS+[(3,b'\3\0\1\0'),(7,b'\7\0')]
                for first in range(len(expected)+1):self.page(first,h,expected)
                self.page(65535,h,expected);self.read(expected,h);self.observe('first-offsets')
            elif name=='CHANGED':
                updated=[(2,b'\0\0changed'),*LABELS[1:]];new_hash=None;calls=[]
                def source(first):
                    nonlocal new_hash
                    calls.append(first);p=self.page(first)
                    if len(calls)==1:new_hash=self.hashed(17,item(130,updated[0][1]),sid)
                    return p
                result=self.a.read_all(source);self.host_reads.append(result)
                require(new_hash is not None and new_hash!=h,'config change did not change hash')
                require(len(calls)>=3 and calls[0]==calls[2]==0 and calls[1]>0,'Host did not restart at first0 after hash change')
                require(result['restarts']==1 and result['hash']==new_hash and result['items']==updated,'Host returned mixed epochs')
                self.read(updated,new_hash);self.observe('changed')
            else:
                self.a.seed(self.c,sid);before=self.a.state()
                self.read(LABELS,h);self.page(1,h,LABELS);self.storage(0,0)
                require(self.a.state()==before,'SID0 config read changed config/storage/live resources/lease/cache')
                self.observe('readonly')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-PAGING-CONTEXT','config §2; core §7.1/7.5',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-PAGING-'+name,'config §2; core §2.3',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,
                    max_frame=self.c.max_frame,state_observations=self.observations,
                    page_observations=[dict((k,v) for k,v in p.items() if k!='items')|{'items_hex':[(t,v.hex()) for t,v in p['items']]} for p in self.pages],
                    host_reads=[dict((k,v) for k,v in r.items() if k!='items')|{'items_hex':[(t,v.hex()) for t,v in r['items']]} for r in self.host_reads],
                    unchecked=['all config items and arbitrary channel catalogs','simultaneous requests and perpetual churn',
                               'production Host integration','save timing/write suppression/power loss','physical equipment'])
