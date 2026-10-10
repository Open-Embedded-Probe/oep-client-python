"""Explicit bounded-history sample; no changes to normal profiles/limits."""
from .virtual_pipeline_model import PipelineModel
from .virtual_route_model import RoutedData
from .virtual_core import Core, Reject, tlv


class RetentionData(RoutedData):
    name = b'io.github.open-embedded-probe.retention'
    fixed = RoutedData.fixed.copy()
    fixed[16] = 2

    def dispatch(self, fn, op, payload):
        if op != 16:
            return super().dispatch(fn, op, payload)
        kind, reply_size = payload
        if reply_size not in (7, 16, 17):
            raise Reject(3)
        rid = super().dispatch(fn, op, bytes((kind,)))
        return rid if reply_size == 7 else rid + tlv(0x40, bytes(range(reply_size - 10)))


class RetentionModel(PipelineModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms, boot_id)
        self.ep.remember_max = 16
        self.ep.static[0] = tuple(tlv(66, b'virtual-retention-1') if row[0] == 66 else row for row in self.ep.static[0])
        self.extension = RetentionData(self)
        self.ep.current_core = Core(self.ep, self.extension)

    def retention_state(self):
        return {'max_bytes': self.ep.remember_max, 'capacity': max(8, self.ep.max_inflight)}

    def state(self):
        value = super().state()
        value.update(next_resource_id=self.extension.next_id, boot_id=self.ep.boot_id)
        return value

    def restart(self, boot_id):
        if type(boot_id) is not int or not 0 <= boot_id <= 0xffffffff or boot_id == self.ep.boot_id:
            raise ValueError('explicit different u32 boot_id required for logical restart')
        old_boot = self.ep.boot_id
        clock, origin = self.ep.now, self.ep.now()
        self.__init__(lambda: clock() - origin, boot_id=boot_id)
        return {'old_boot': old_boot, 'new_boot': boot_id, 'scope': 'logical-model'}
