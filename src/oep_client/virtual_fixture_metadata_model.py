"""Independent fixture metadata declarations; advertised ops are not implemented."""
import struct
from .virtual_registered_ops_model import RegisteredOps, RegisteredOpsModel
from .virtual_core import Core, tlv


class FixtureMetadata(RegisteredOps):
    def __init__(self):
        super().__init__();self.offered[12].add(20)
        self.metadata={
            10:[tlv(64,struct.pack('<I',127))],
            11:[tlv(64,bytes([3,0,4,17]))],
            12:[tlv(2,struct.pack('<I',400000)),tlv(3,struct.pack('<H',8)),tlv(5,struct.pack('<I',4)),tlv(64,b'\x04'),tlv(65,struct.pack('<I',1000))],
            13:[tlv(2,struct.pack('<I',1000000)),tlv(3,struct.pack('<H',8)),tlv(5,struct.pack('<I',1)),tlv(64,b'\x04'),tlv(65,b'\x0f'),tlv(67,struct.pack('<I',100))],
        }
    def describe(self,fn):return super().describe(fn)+tuple(self.metadata.get(fn,()))


class FixtureMetadataModel(RegisteredOpsModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.extension=FixtureMetadata();self.ep.current_core=Core(self.ep,self.extension)
        self.metadata_selection={fn:self.selection[fn] for fn in (10,11,12,13)}
