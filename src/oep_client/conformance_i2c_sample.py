"""Explicit completed I2C controller transactions; no equipment discovery."""
from .conformance_route_sample import SampleRouteAdapter


class SampleI2cAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.i2c-target'}
    reference='docs/i2c-conformance.ja.md logical fixture contract'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('i2c_state')
    def write(self,address,data,end):return self.call('i2c_write',address,data,end)
    def read(self,address,count):return self.call('i2c_read',address,count)
    def advance(self,ms):return self.call('i2c_advance',ms)
