"""Selected describe fixture, not functional restart/debug/storage operations."""
import struct
from .virtual_registered_ops_model import RegisteredOps, RegisteredOpsModel
from .virtual_core import Core, tlv


class SelectedMetadata(RegisteredOps):
    def __init__(self):
        super().__init__();self.offered[7].update((20,21));self.offered[17].update((18,19,21))
        self.metadata={2:[tlv(64,struct.pack('<I',2500))],7:[tlv(3,struct.pack('<H',8))],8:[tlv(3,struct.pack('<H',8))],17:[tlv(64,struct.pack('<I',256))]}
    def describe(self,fn):return super().describe(fn)+tuple(self.metadata.get(fn,()))


class SelectedMetadataModel(RegisteredOpsModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.extension=SelectedMetadata();self.ep.current_core=Core(self.ep,self.extension)
        self.metadata_selection={fn:self.selection[fn] for fn in (2,7,8,17)}
