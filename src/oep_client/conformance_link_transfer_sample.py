"""Explicit observation/seed adapter for GPIO/plan plus mandatory link fixture."""
import struct

from .conformance import require


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
        require(checks.success(checks.request(16,b'\x90\x05\x00'+assignment,fn=1,session=sid))==b'', 'seed plan')
        require(checks.success(checks.request(16,b'\x01\x01\x00\x04',fn=2,session=sid))==b'', 'seed GPIO high')
        state=self.state()
        require(state['plans']=={2:[1]} and state['pins'][1][0]==4,'live plan/pin seed missing')
