"""Multiple logical writers over one current Core, explicit completion tokens."""
import hashlib
from pathlib import Path
import struct
from .message import Request
from .virtual_resource_model import ResourceModel


class SampleResponseRaceAdapter:
    def run_case(self,case):
        clock=[0];model=ResourceModel(lambda:clock[0],boot_id=17);core=model.ep.current_core
        pending={};events=[];lease=case['lease_ms'];kind=case['kind']
        def state():
            return dict(now_ms=clock[0],holder=core.holder,last=core.last,generation=core.generation,
                        lease_ms=core.lease,deadline=core.deadline,next_id=model.extension.next_id,
                        live={str(k):list(v) for k,v in model.extension.live.items()},
                        cache={str(k):repr(v) for k,v in core.cache.items()})
        def prepare(route,req):
            before=state();reply=core.handle(req,route,defer_send=True);token=core.renewal
            pending[route]=(req,reply,token)
            events.append(dict(action='prepare',route=route,request_hex=req.hex(),reply_hex=reply.hex(),token=list(token) if token else None,before=before,after=state()))
            return reply
        def finish(route):
            req,reply,token=pending.pop(route);before=state();core.response_sent(token)
            events.append(dict(action='finish',route=route,request_hex=req.hex(),reply_hex=reply.hex(),token=list(token) if token else None,before=before,after=state()))
        def immediate(route,req):prepare(route,req);finish(route)
        immediate(0,Request(1,0,16,struct.pack('<IB',lease,0),7).pack())
        immediate(0,Request(2,1,16,b'\1',7).pack())
        clock[0]=100
        old=Request(3,1,16,b'\3' if kind=='REJECTED' else b'\1',7).pack()
        prepare(0,old)
        clock[0]=400
        prepare(1,Request(1,0,16,struct.pack('<IB',lease,kind!='NO-FORCE'),8).pack())
        clock[0]=450
        if kind in ('DOUBLE-FORCE','RETURN-ID'):
            holder=7 if kind=='RETURN-ID' else 9
            immediate(2,Request(1,0,16,struct.pack('<IB',lease,1),holder).pack())
        else:finish(1);holder=7 if kind=='NO-FORCE' else 8
        clock[0]=500
        if kind!='NO-FORCE':immediate(2 if kind in ('DOUBLE-FORCE','RETURN-ID') else 1,Request(2,1,16,b'\1',holder).pack())
        if kind=='EXPIRE-NEW':
            clock[0]=500+lease;before=state();core.tick();events.append(dict(action='expire',before=before,after=state()))
        else:clock[0]=700
        finish(0)
        if kind in ('DOUBLE-FORCE','RETURN-ID'):clock[0]=750;finish(1)
        if kind=='NO-FORCE':
            clock[0]=750;before=state();reply=core.handle(old,0)
            events.append(dict(action='replay',request_hex=old.hex(),reply_hex=reply.hex(),before=before,after=state()))
        elif kind!='RETURN-ID':
            if kind!='EXPIRE-NEW':clock[0]=800
            before=state();reply=core.handle(old,0)
            events.append(dict(action='old-request',request_hex=old.hex(),reply_hex=reply.hex(),before=before,after=state()))
        return dict(case=case,events=events,state=state(),pending_routes=list(pending))
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('virtual_core.py','virtual_resource_model.py','endpoint.py','virtual_bench.py','message.py')}
