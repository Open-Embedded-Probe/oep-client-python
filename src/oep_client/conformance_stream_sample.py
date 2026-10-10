"""Explicit adoption and external stimulus for the independent stream fixture."""
from .conformance_route_sample import SampleRouteAdapter


class SampleStreamAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    adoption={'component':'positioned-stream-v1','addressing':'persistent-fn','fn':1,
              'ops':{'read':16,'marks':17,'clear':18,'mark':19,'write':20},
              'byte_capacity':64,'mark_capacity':5,'reference':'docs/stream-conformance.ja.md sample contract'}

    def restart_fixture(self, position=0, serial=0):return self.call('restart_fixture',position,serial)
    def feed_bytes(self, data):return self.call('feed',data)
    def mark_external(self, kind, detail=0):return self.call('mark_external',kind,detail)
    def stream_state(self):return self.call('stream_state')
