"""Config pages, numeric keys, independent first offsets and changing generations."""
import struct
from .conformance import require
from .conformance_config import ConfigChecks,item,page
from .conformance_registered_ops import RegisteredOpsChecks


CHANNELS=(0,1,2,3,4,7,8,255,256,257,300,511)
LABELS=[(2,struct.pack('<H',ch)+bytes((97+i,))*(22 if i==0 else 21 if i==1 else 32)) for i,ch in enumerate(CHANNELS)]


class ConfigPagingChecks(ConfigChecks):
    cases=('EMPTY','DENSE','FIRST','CHANGED','READONLY')
    channels=512
    def __init__(self,c,a):super().__init__(c,a);self.pages=[];self.host_reads=[]
    def context(self):
        super().context()
        for name in ('prepare','read_all'):require(callable(getattr(self.a,name,None)),'explicit config paging controls required')
        require(self.c.max_frame in (64,128,256),'explicit config paging frame profile')
    def page(self,first,expected_generation=None,expected=None):
        p=self.c.success(self.op(16,struct.pack('<H',first)))
        more,g,rows=page(p)
        if expected_generation is not None:require(g==expected_generation,'stable config pages changed generation')
        if expected is not None:
            require(rows==expected[first:first+len(rows)],'config page differs from numeric item offset')
            require(more==int(first+len(rows)<len(expected)),'config page more differs from remaining items')
            require(not more or rows,'config paging made no progress')
        self.pages.append(dict(first=first,more=more,generation=g,items=rows,payload_hex=p.hex(),result_length=len(p)+5))
        return p
    def read(self,expected,g,source=None):
        start=len(self.pages)
        result=self.a.read_all(source or (lambda first:self.page(first,g,expected)))
        require(result['generation']==g and result['items']==expected,'Host config read mixed/lost/duplicated items')
        self.host_reads.append(result)
        return self.pages[start:]
    def seed_labels(self,sid):
        g=None
        for t,v in reversed(LABELS):g=self.set(sid,item(t|128,v))
        require(g==1+len(LABELS),'each label did not count the generation')
        return g
    def case(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh paging fixture')
        if name=='EMPTY':
            self.page(0,1,[]);self.page(1,1,[]);self.page(65535,1,[]);self.read([],1);self.observe('empty');return
        with self.c.holding(60000) as sid:
            g=self.seed_labels(sid)
            if name=='DENSE':
                pages=self.read(LABELS,g)
                require(len(pages)>1 and all(p['more']==1 for p in pages[:-1]) and pages[-1]['more']==0,'dense paging fixture did not span pages')
                if self.c.max_frame==64:require(pages[0]['result_length']==64,'exact-fit page boundary fixture differs')
                self.observe('dense')
            elif name=='FIRST':
                tail=[(3,b'\1\0\2'),(3,b'\0\1\1'),(7,b'\6\0'),(7,b'\7\0'),(7,b'\xff\1')]
                self.set(sid,*(item(t|128,v) for t,v in reversed(tail[2:])))
                g=self.set(sid,item(131,tail[1][1]),item(3,tail[0][1]))
                expected=LABELS+tail
                for first in range(len(expected)+1):self.page(first,g,expected)
                self.page(65535,g,expected);self.read(expected,g);self.observe('first-offsets')
            elif name=='CHANGED':
                updated=[(2,b'\0\0changed'),*LABELS[1:]];new=None;calls=[]
                def source(first):
                    nonlocal new
                    calls.append(first);p=self.page(first)
                    if len(calls)==1:new=self.set(sid,item(130,updated[0][1]))
                    return p
                result=self.a.read_all(source);self.host_reads.append(result)
                require(new==g+1,'config change did not count the generation')
                require(len(calls)>=3 and calls[0]==calls[2]==0 and calls[1]>0,'Host did not restart at first0 after generation change')
                require(result['restarts']==1 and result['generation']==new and result['items']==updated,'Host returned mixed epochs')
                self.read(updated,new);self.observe('changed')
            else:
                self.a.seed(self.c,sid);before=self.a.state()
                self.read(LABELS,g);self.page(1,g,LABELS);self.storage(0,0)
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
                    unchecked=['arbitrary channel catalogs and answer TLV tails','simultaneous requests and perpetual churn',
                               'production Host integration','save timing/write suppression/power loss','physical equipment'])
