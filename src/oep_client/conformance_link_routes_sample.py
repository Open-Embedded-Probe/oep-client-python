"""Explicit logical routes and close/reopen controls; no physical enumeration."""
from .conformance_link_transfer_sample import SampleLinkTransferAdapter


class SampleLinkRoutesAdapter(SampleLinkTransferAdapter):
    routes=(0,1,2)
    limits={0:64,1:256,2:1024}
    kinds={0:1,1:4,2:6}
    session_route=2

    def select(self,checks,route):
        checks.send=lambda request:self.model.handle_route(request,route)
    def close(self,route):self.model.close_route(route)
    def reopen(self,route):self.model.reopen_route(route)
    def trace(self):return self.model.route_trace()
