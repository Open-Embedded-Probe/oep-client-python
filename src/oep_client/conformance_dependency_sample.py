"""Explicit resource-addressed stream and connection sample adapter."""
from .conformance_route_sample import SampleRouteAdapter


class SampleDependencyAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    adoption={'connection':{'component':'debug-connection-v1','fn':1,
                            'ops':{'attach':16,'detach':17,'use':18,'list':19}},
              'stream':{'component':'positioned-stream-v1','addressing':'resource','fn':2,
                        'ops':{'open':16,'read':17,'marks':18,'clear':19,'mark':20,'write':21,'close':22,'list':23}},
              'tx_capacity':4,'max_resources':8,'reference':'docs/dependency-conformance.ja.md sample contract'}

    def restart_fixture(self):return self.call('restart_fixture')
    def feed_bytes(self, rid, data):return self.call('feed',rid,data)
    def reset_target(self, connection):return self.call('reset_target',connection)
    def break_connection(self, connection):return self.call('break_connection',connection)
    def dependency_state(self):return self.call('dependency_state')
