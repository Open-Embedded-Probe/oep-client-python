"""Independent capture/config describe fixture; no advertised op implementation."""
import struct
from .virtual_registered_ops_model import RegisteredOps, RegisteredOpsModel
from .virtual_core import Core, tlv


class CaptureMetadata(RegisteredOps):
    def __init__(self):
        super().__init__()
        track=[tlv(5,b'\0'*4),tlv(64,struct.pack('<BII',1,32,1)),tlv(64,struct.pack('<BII',3,32,1)),tlv(65,struct.pack('<IIB',1,100000,1)),tlv(68,b'\x04'),tlv(69,struct.pack('<II',1,16))]
        self.metadata={14:track+[tlv(96,struct.pack('<IIIB',7,2,64,1))],15:track+[tlv(70,struct.pack('<BiiI',0,-1000,3300,0))],16:[tlv(5,b'\0'*4),tlv(64,struct.pack('<BHH',2,14,15))],17:[tlv(65,bytes([1,2,3,4,6,7])),tlv(66,b'\x04')]}
    def describe(self,fn):return super().describe(fn)+tuple(self.metadata.get(fn,()))


class CaptureMetadataModel(RegisteredOpsModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.extension=CaptureMetadata();self.ep.current_core=Core(self.ep,self.extension)
        self.metadata_selection={fn:self.selection[fn] for fn in (14,15,16,17)}
