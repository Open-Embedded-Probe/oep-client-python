"""Explicit target dmseq simulator controls with a logical clock."""
from .conformance_route_sample import SampleRouteAdapter
from .conformance_dmseq_sample import SampleDmseqAdapter


class SampleTargetDmseqAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    reference='docs/dmseq-target-conformance.ja.md target-side logical contract'
    short_ms=20
    long_ms=1000
    input_capacity=2
    output_capacity=6
    def __init__(self,source):
        super().__init__(source);self.probe=SampleDmseqAdapter(source)
    def begin(self,s=0,a=1,data=b''):return self.call('target_begin',s,a,data)
    def state(self):return self.call('target_state')
    def host_word(self,word):return self.call('target_host_word',word)
    def service(self,advance=0):return self.call('target_service',advance)
    def write_output(self,data):return self.call('target_write_output',data)
    def read_input(self,count):return self.call('target_read_input',count)
    def link_poll(self,rid):return self.call('target_link_poll',rid)
