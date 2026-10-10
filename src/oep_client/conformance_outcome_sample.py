"""Explicit common-component adoption for the outcome fixture."""
from .conformance_stream_sample import SampleStreamAdapter


class SampleOutcomeAdapter(SampleStreamAdapter):
    adoptions={'stream':{'component':'positioned-stream-v1','fn':1,'write_op':20},
               'debug':{'component':'debug-status-v1','fn':2,'batch_op':16,'single_op':17},
               'tx_capacity':4,'max_steps':8,'reference':'docs/outcome-conformance.ja.md sample contract'}

    def configure_debug(self, done=None, status=1):return self.call('configure_debug',done,status)
    def drain_tx(self, count):return self.call('drain_tx',count)
    def outcome_state(self):return self.call('outcome_state')
