"""Explicit 512 setting channels, eight GPIO pins and small config result frames."""
import struct
from .virtual_config_model import ConfigModel
from .virtual_core import tlv


class ConfigPagingModel(ConfigModel):
    channels=512                    # fn 0 channels: items name 0..511; only 0..7 are GPIO pins of this fixture
    def __init__(self,*,max_frame=64,boot_id=17):
        if max_frame not in (64,128,256):raise ValueError('explicit paging profile frames')
        super().__init__(boot_id=boot_id);self.ep.probe.max_frame=max_frame
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',512)),)
