"""Explicit simultaneous-invalid requests and real replay retention limits."""
from pathlib import Path
import hashlib
import struct
from .message import Request
from .virtual_core import tlv
from .virtual_resource_model import ResourceModel


class SampleRefusalOrderAdapter:
    def prepare(self,context):
        self.ms=0;self.model=ResourceModel(lambda:self.ms,boot_id=17);self.core=self.model.ep.current_core
        self.model.ep.remember_max={'normal':2048,'request-lost':10,'response-lost':16}[context['retention']]
        self.raw(Request(1,0,16,struct.pack('<IB',1000,0)+tlv(1,b'priority'),7).pack())
        cached=Request(3,1,16,b'\1',7).pack();reply=self.raw(cached)
        if context['retention']=='response-lost':
            cached=Request(4,0,1,b'OEP?\1\1',7).pack();reply=self.raw(cached)
        corr=int.from_bytes(cached[1:3],'little');request,result=self.core.cache[corr]
        self.ms=1000 if context['expired'] else 400;self.core.tick()
        return dict(context=context,cached_request_hex=cached.hex(),original_reply_hex=reply.hex(),
                    stored_request_hex=request.hex() if request is not None else None,
                    stored_reply_hex=result.hex() if result is not None else None,state=self.state())
    def raw(self,data,admission=None):return self.core.handle(data,0,admission_reason=admission)
    def state(self):
        c=self.core;e=self.model.extension
        return dict(now_ms=self.ms,holder=c.holder,last=c.last,owner_hex=c.owner.hex(),deadline=c.deadline,lease_ms=c.lease,
                    generation=c.generation,high=c.high,next_id=e.next_id,live={str(k):list(v) for k,v in e.live.items()},
                    cache={str(k):dict(request_hex=v[0].hex() if v[0] is not None else None,
                                       reply_hex=v[1].hex() if v[1] is not None else None) for k,v in c.cache.items()})
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('virtual_core.py','virtual_resource_model.py','endpoint.py','virtual_bench.py','message.py')}
