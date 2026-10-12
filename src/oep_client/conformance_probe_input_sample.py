"""Current virtual Core raw input, ownership and timer observations."""
import hashlib
from pathlib import Path
import struct
from .message import Request
from .virtual_resource_model import ResourceModel
from .virtual_core import tlv


class SampleProbeInputAdapter:
    def prepare(self):
        self.ms=0;self.model=ResourceModel(lambda:self.ms,boot_id=17)
        self.core=self.model.ep.current_core
        self.raw(Request(1,0,16,struct.pack('<IB',1000,0)+tlv(1,b'input-checker'),7).pack())
        self.raw(Request(2,1,16,b'\1',7).pack())
        return self.state()
    def advance_to(self,ms):
        if type(ms)is not int or ms<self.ms or ms>60000:raise ValueError('forward explicit logical ms required')
        self.ms=ms
    def raw(self,data):return self.core.handle(data,0)
    def state(self):
        c=self.core;e=self.model.extension
        return dict(now_ms=self.ms,holder=c.holder,last=c.last,owner_hex=c.owner.hex(),deadline=c.deadline,lease_ms=c.lease,
                    generation=c.generation,high=c.high,next_id=e.next_id,
                    live={str(k):list(v) for k,v in e.live.items()},cache={str(k):repr(v) for k,v in c.cache.items()})
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('virtual_core.py','virtual_resource_model.py','endpoint.py','virtual_bench.py','message.py')}
