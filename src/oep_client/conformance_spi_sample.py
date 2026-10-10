"""Explicit logical SPI controller edges, CS and arm readiness controls."""
from .conformance_route_sample import SampleRouteAdapter


class SampleSpiAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.spi-target'}
    reference='docs/spi-conformance.ja.md logical edge fixture'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('spi_state')
    def transfer(self,bits):return self.call('spi_transfer',bits)
    def cs(self,active):return self.call('spi_cs',active)
    def clock(self,level,mosi):return self.call('spi_clock',level,mosi)
    def ready(self,value):return self.call('spi_ready',value)
    def advance(self,ms):return self.call('spi_advance',ms)
