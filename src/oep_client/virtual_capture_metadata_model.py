"""Independent capture/config describe fixture; no advertised op implementation."""
import struct
from .virtual_registered_ops_model import RegisteredOps, RegisteredOpsModel
from .virtual_core import Core, tlv


class CaptureMetadata(RegisteredOps):
    def __init__(self):
        super().__init__()
        track=[tlv(1,bytes((0,0,0,7))),tlv(65,struct.pack('<II',1,100000)),tlv(68,b'\x04'),tlv(69,struct.pack('<II',1,16))]
        # fn 14 offers one-shot and streaming (so subscribe/unsubscribe), fn 15 one-shot and repeat (so release).
        self.metadata={14:track+[tlv(5,b'\0'*4),tlv(64,struct.pack('<BII',1,32,0)),tlv(64,struct.pack('<BII',3,32,0))],
                       15:track+[tlv(64,struct.pack('<BII',1,32,0)),tlv(64,struct.pack('<BII',2,32,4)),tlv(70,struct.pack('<Bii',0,-1000,3300))],
                       16:[tlv(64,struct.pack('<BHH',2,14,15))],17:[tlv(65,bytes([1,2,3,4,6,7])),tlv(66,b'\x04')]}
        self.offered[14].update((1,2));self.offered[15].add(23)
    def describe(self,fn):return super().describe(fn)+tuple(self.metadata.get(fn,()))


class CaptureMetadataModel(RegisteredOpsModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.extension=CaptureMetadata();self.ep.current_core=Core(self.ep,self.extension)
        # Three probe channels, so the tracks' role_channels/channel_group name real pins (core §7.4).
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',3)),)
        self.metadata_selection={fn:self.selection[fn] for fn in (14,15,16,17)}
