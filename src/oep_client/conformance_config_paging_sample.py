"""Strict Host paging example with explicit bounded recovery; no bench dependency."""
from .conformance import require
from .conformance_config import page
from .conformance_config_sample import SampleConfigAdapter


def read_config_pages(read_page,*,max_restarts=3):
    restarts=0;pages=[]
    while True:
        items=[];first_generation=None
        while True:
            first=len(items);more,g,rows=page(read_page(first));pages.append(dict(first=first,generation=g,count=len(rows)))
            if first_generation is not None and g!=first_generation:
                restarts+=1;require(restarts<=max_restarts,'Host config paging recovery budget exhausted');break
            first_generation=g
            require(not more or rows,'Host config paging made no progress')
            items.extend(rows)
            if not more:return dict(generation=g,items=items,restarts=restarts,pages=pages)


class SampleConfigPagingAdapter(SampleConfigAdapter):
    read_all=staticmethod(read_config_pages)
    def prepare(self):
        self.model.erase_config()
        return self.restart_fixture()
    def state(self):
        c=self.model.ep.current_core
        return dict(super().state(),core=dict(holder=c.holder,last=c.last,lease=c.lease,deadline=c.deadline,
                    high=c.high,generation=c.generation,cache={str(k):[None if req is None else req.hex(),None if out is None else out.hex()] for k,(req,out) in c.cache.items()}))
