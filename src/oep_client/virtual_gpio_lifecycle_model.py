"""Explicit logical clock for GPIO/plan expiry and ownership transitions."""
from .virtual_gpio_model import GpioModel


class GpioLifecycleModel(GpioModel):
    def __init__(self,boot_id=None):
        self.milliseconds=0
        super().__init__(lambda:self.milliseconds,boot_id=boot_id)
    def gpio_lifecycle_state(self):
        state=self.gpio_state();core=self.ep.current_core
        state.update(clock_scope='explicit-logical-ms',now_ms=self.milliseconds,holder=core.holder,deadline=core.deadline,lease_ms=core.lease,generation=core.generation)
        return state
    def gpio_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit 0..60000 logical milliseconds required')
        self.milliseconds+=ms
