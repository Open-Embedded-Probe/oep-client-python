"""Explicit state, restart and generation controls for current RAM storage config fixture."""
from .conformance import require


class SampleConfigAdapter:
    selection={4:'oep.probe.config'}
    def __init__(self,model):self.model=model
    def state(self):return self.model.config_state()
    def restart_fixture(self):return self.model.restart_fixture()
    def generation_at(self,value):self.model.extension.generation=value
    def seed(self,c,sid):
        require(c.success(c.request(16,b'\x90\5\0\2\0\1\1\0',fn=1,session=sid))==b'','seed plan')
        require(c.success(c.request(16,b'\1\1\0\4',fn=2,session=sid))==b'','seed pin')
