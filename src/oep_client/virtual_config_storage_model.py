"""RAM backend whose actual write calls and committed copies are observable."""
import copy
from .virtual_config_model import ConfigModel


class RamConfigStorage:
    def __init__(self):self.reset()
    def reset(self):self.blob=None;self.writes=[];self.erases=0
    def write(self,items):
        self.blob=copy.deepcopy(list(items))
        self.writes.append(copy.deepcopy(self.blob))
    def erase(self):self.blob=None;self.erases+=1


class ConfigStorageModel(ConfigModel):
    def __init__(self,*,boot_id=17):
        self.storage=RamConfigStorage();super().__init__(boot_id=boot_id)
    def write_config(self,items):
        self.storage.write(items);self.saved=copy.deepcopy(self.storage.blob)
    def erase_config(self):self.storage.erase();super().erase_config()
