"""One current Core exposed by three explicit logical transport routes."""
import struct

from .virtual_link_transfer_model import LinkTransferModel
from .virtual_core import tlv


class LinkRoutesModel(LinkTransferModel):
    limits={0:64,1:256,2:1024}
    kinds={0:1,1:4,2:6}

    def __init__(self,*args,**kwargs):
        super().__init__(*args,max_frame=1024,**kwargs)
        self.open_routes=set(self.limits);self.route_epochs={r:0 for r in self.limits};self.exchanges=[]
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=73)+tuple(
            tlv(73,bytes((r,self.kinds[r],0 if r==1 else 255))) for r in self.limits)

    def handle_route(self,request,route):
        if route not in self.open_routes:raise ValueError('explicit route is closed')
        e=self.extension;fixed,free=e.fixed,e.free
        frame,limit=self.ep.probe.max_frame,e.frame_limit
        fn=int.from_bytes(request[3:5],'little') if len(request)>=6 else 0
        n=request[10] if len(request)>10 else 0
        if fn==4:
            count=int.from_bytes(request[10:12],'little') if len(request)>=12 else 0
            e.fixed={16:4,17:2+count};e.free={16,17}
        elif fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
        else:e.fixed={16:1+3*n,17:1+2*n};e.free={17}
        self.ep.probe.max_frame=e.frame_limit=self.limits[route]
        try:
            reply=self.ep.current_core.handle(request,route)
            self.exchanges.append(dict(route=route,epoch=self.route_epochs[route],request_hex=request.hex(),reply_hex=reply.hex() if reply else None))
            return reply
        finally:
            e.fixed,e.free=fixed,free;self.ep.probe.max_frame,e.frame_limit=frame,limit

    def close_route(self,route):
        self.open_routes.remove(route)
    def reopen_route(self,route):
        if route in self.open_routes:raise ValueError('route is already open')
        self.route_epochs[route]+=1;self.open_routes.add(route)
    def route_trace(self):return list(self.exchanges)
