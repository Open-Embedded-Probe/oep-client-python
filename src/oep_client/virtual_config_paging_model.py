"""Explicit 512 label channels, eight GPIO pins and small config result frames."""
import struct
from .virtual_config_model import ConfigModel,ConfigExtension
from .virtual_core import Core,Reject,tlv


class ConfigPagingExtension(ConfigExtension):
    def key(self,tag,value):
        if tag!=2:return super().key(tag,value)
        if len(value)<2:raise Reject(3)
        if int.from_bytes(value[:2],'little')>=512:raise Reject(11,bytes((tag,)))
        super().key(tag,b'\0\0'+value[2:])
        return value[:2]


class ConfigPagingModel(ConfigModel):
    def __init__(self,*,max_frame=64,boot_id=17):
        if max_frame not in (64,128,256):raise ValueError('explicit paging profile frames')
        super().__init__(boot_id=boot_id);self.ep.probe.max_frame=max_frame
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',512)),)
    def install(self):self.extension=ConfigPagingExtension(self);self.ep.current_core=Core(self.ep,self.extension)
