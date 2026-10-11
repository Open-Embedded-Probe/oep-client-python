"""Logical resource allocator boundary fixture; seed is not a history of real allocations."""
import copy
from .virtual_resource_model import ResourceModel


class AllocatorModel(ResourceModel):
    def __init__(self, boot_id=17):
        self.milliseconds=0
        super().__init__(lambda:self.milliseconds,boot_id)
    def restart_fixture(self):
        boot=(self.ep.boot_id+1)&0xffffffff;self.reboot(boot)
        return dict(scope='logical-model',boot_id=boot)
    def seed_allocator(self, value):
        if type(value) is not int or value not in (65534,65535) or self.extension.live or self.extension.next_id!=1:
            raise ValueError('fresh empty allocator and explicit boundary seed required')
        self.extension.next_id=value
    def allocator_state(self):
        return copy.deepcopy(dict(next_id=self.extension.next_id,live=self.extension.live))
    def advance_ms(self, value):
        if type(value) is not int or value<0:raise ValueError('nonnegative logical milliseconds required')
        self.milliseconds+=value
