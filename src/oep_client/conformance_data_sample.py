"""Independent stimulus byte cursor; expected positions never come from probe output."""
from .conformance_subscription_sample import SampleSubscriptionAdapter


class SampleDataAdapter(SampleSubscriptionAdapter):
    def __init__(self, source, *, loss_free, **kwargs):
        super().__init__(source, **kwargs)
        self.loss_free = loss_free
        self.positions = {fn: 0 for fn in self.functions}

    def feed(self, fn, data):
        position = self.positions[fn]
        self.source.feed(fn, position, data)
        self.positions[fn] += len(data)
        return position
