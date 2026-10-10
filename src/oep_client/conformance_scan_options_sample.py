"""Explicit finite scan selector, identities and pin controls."""
from .conformance_wire_options_sample import SampleWireOptionsAdapter


class SampleScanOptionsAdapter(SampleWireOptionsAdapter):
    reference='docs/scan-options-conformance.ja.md logical scan options contract'
    def state(self):return self.call('scan_options_state')
    def identity(self,fn,pair,selector,value):return self.call('scan_identity',fn,pair,selector,value)
    def limit(self,count):return self.call('scan_limit',count)
