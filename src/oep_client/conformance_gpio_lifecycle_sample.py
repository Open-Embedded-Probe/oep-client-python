"""Explicit GPIO/plan clock controls; no sleep or hardware discovery."""
from .conformance_gpio_sample import SampleGpioAdapter


class SampleGpioLifecycleAdapter(SampleGpioAdapter):
    reference='docs/gpio-lifecycle-conformance.ja.md logical clock fixture'
    def state(self):return self.call('gpio_lifecycle_state')
    def advance(self,ms):return self.call('gpio_advance',ms)
