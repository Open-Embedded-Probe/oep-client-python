"""Explicit logical restart/write/route controls; no equipment discovery."""
import struct
from .conformance import require, tlvs
from .conformance_link_transfer_sample import SampleLinkTransferAdapter


class SampleRestartAdapter(SampleLinkTransferAdapter):
    selection={4:'oep.probe.restart'}
    restart_fn=4
    routes=(0,1,2)
    restart_delay_ms=50
    reset_scope='logical-model'
    def state(self):return self.model.restart_state()
    def begin(self,request):return self.model.begin(request)
    def finish(self):return self.model.finish()
    def advance(self,delta):self.model.advance(delta)
    def raw(self,request,route):return self.model.handle(request,route)
    def events(self):return self.model.events
    def seed(self,checks,sid):
        assignment=struct.pack('<HBH',2,1,1)
        # plan_apply / gpio set answer —: no fixed part, a TLV tail may follow (core §2.3, §4.3)
        tlvs(checks.success(checks.request(16,b'\x90\x05\x00'+assignment,fn=1,session=sid)))
        tlvs(checks.success(checks.request(16,b'\x01\x01\x00\x04',fn=2,session=sid)))
        state=self.model.gpio_state()
        require(state['plans']=={2:[1]} and state['pins'][1]==4,'live plan/pin seed missing')
