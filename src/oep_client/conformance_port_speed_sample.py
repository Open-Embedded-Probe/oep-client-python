"""Logical frame-reception, response-write and clock controls for the port-speed fixture."""
from .conformance_link_transfer_sample import SampleLinkTransferAdapter


class SamplePortSpeedAdapter(SampleLinkTransferAdapter):
    port_speed=True
    route=0
    def state(self):return self.model.link_state()
    def select(self,checks,route):
        self.route=route;checks.send=lambda request:self.model.handle(request,route)
    def speed(self):return self.model.speed_state()
    def begin(self,request):return self.model.begin(request,self.route)
    def finish(self):return self.model.finish()
    def advance(self,delta):self.model.advance(delta)
    def receive(self):self.model.receive(self.route)
    def events(self):return self.model.speed_events()
    def raw(self,frame):return self.model.handle(frame,self.route)
