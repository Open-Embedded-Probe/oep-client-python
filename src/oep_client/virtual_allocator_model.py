"""Logical resource allocator boundary fixture; seed is not a history of real allocations."""
import copy
from .virtual_core import Core
from .virtual_resource_model import ResourceModel, Resources


class U32Resources(Resources):
    """The sample extension with the resource number as core §9 has it: u32, 1 up to 0xFFFFFFFF, then cause 2."""
    fixed = {16: 1, 17: 5, 18: 5, 19: 0}
    rid_format = 'I'
    last_id = 0xFFFFFFFF


class AllocatorModel(ResourceModel):
    def __init__(self, boot_id=17):
        self.milliseconds=0
        super().__init__(lambda:self.milliseconds,boot_id)
        self.extension=U32Resources();self.ep.current_core=Core(self.ep,self.extension)
    def reboot(self, boot_id):
        super().reboot(boot_id)
        self.extension=U32Resources();self.ep.current_core=Core(self.ep,self.extension)
    def restart_fixture(self):
        boot=(self.ep.boot_id+1)&0xffffffff;self.reboot(boot)
        return dict(scope='logical-model',boot_id=boot)
    def seed_allocator(self, value):
        if type(value) is not int or value not in (0xFFFFFFFE,0xFFFFFFFF) or self.extension.live or self.extension.next_id!=1:
            raise ValueError('fresh empty allocator and explicit boundary seed required')
        self.extension.next_id=value
    def allocator_state(self):
        return copy.deepcopy(dict(next_id=self.extension.next_id,live=self.extension.live))
    def advance_ms(self, value):
        if type(value) is not int or value<0:raise ValueError('nonnegative logical milliseconds required')
        self.milliseconds+=value
