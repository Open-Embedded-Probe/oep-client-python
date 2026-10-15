"""Explicit observation/seed adapter for GPIO/plan plus mandatory link fixture."""
import struct

from .conformance import require, tlvs


class SampleLinkTransferAdapter:
    selection={4:'oep.probe.link'}
    link_fn=4
    port_speed=False
    reset_scope='logical-model'

    def __init__(self,model):self.model=model
    @property
    def retained_bytes(self):return self.model.ep.remember_max
    def state(self):return self.model.link_state()
    def restart_fixture(self):return self.model.restart_fixture()
    def seed(self,checks,sid):
        assignment=struct.pack('<HBH',2,1,1)
        # plan_apply / gpio set answer —: no fixed part, a TLV tail may follow (core §2.3, §4.3)
        tlvs(checks.success(checks.request(16,b'\x90\x05\x00'+assignment,fn=1,session=sid)))
        tlvs(checks.success(checks.request(16,b'\x01\x01\x00\x04',fn=2,session=sid)))
        state=self.state()
        require(state['plans']=={2:[1]} and state['pins'][1]==4,'live plan/pin seed missing')
