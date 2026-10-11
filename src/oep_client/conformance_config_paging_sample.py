"""Strict Host paging example with explicit bounded recovery; no bench dependency."""
from .conformance import require,tlvs
from .conformance_config_sample import SampleConfigAdapter


def read_config_pages(read_page,*,max_restarts=3):
    restarts=0;pages=[]
    while True:
        items=[];first_hash=None
        while True:
            first=len(items);p=read_page(first)
            require(len(p)>=5 and p[0] in (0,1),'Host config page fixed shape')
            h=int.from_bytes(p[1:5],'little');rows=tlvs(p[5:]);pages.append(dict(first=first,hash=h,count=len(rows)))
            if first_hash is not None and h!=first_hash:
                restarts+=1;require(restarts<=max_restarts,'Host config paging recovery budget exhausted');break
            first_hash=h
            require(not p[0] or rows,'Host config paging made no progress')
            items.extend(rows)
            if not p[0]:return dict(hash=h,items=items,restarts=restarts,pages=pages)


class SampleConfigPagingAdapter(SampleConfigAdapter):
    read_all=staticmethod(read_config_pages)
    def prepare(self):
        self.model.saved=None;self.model.saved_hash=0;self.model.saved_identities={}
        return self.restart_fixture()
    def state(self):
        c=self.model.ep.current_core
        return dict(super().state(),core=dict(holder=c.holder,last=c.last,lease=c.lease,deadline=c.deadline,
                    high=c.high,generation=c.generation,cache={str(k):[None if req is None else req.hex(),None if out is None else out.hex()] for k,(req,out) in c.cache.items()}))
