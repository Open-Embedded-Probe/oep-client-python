"""Explicit logical standard attach/console fixture controls."""
from .conformance_route_sample import SampleRouteAdapter


class SampleAttachAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.wire.rvswd',2:'oep.wire.swio',3:'oep.wire.swd',4:'oep.target.console'}
    reference='docs/attach-conformance.ja.md logical sample contract'
    pairs={1:[(0,1),(2,3),(6,7)],2:[(0,65535),(2,65535),(6,65535)],3:[(4,5)]}
    console_fn=4
    mechanisms=[0,1]
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('attach_state')
    def fail_attach(self, value):return self.call('fail_attach',value)
    def feed_bytes(self, rid, value):return self.call('feed',rid,value)
