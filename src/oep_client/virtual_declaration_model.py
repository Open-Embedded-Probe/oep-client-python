"""Independent custom declaration fixture, without target or pin driving."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, tlv


class Declarations(Resources):
    functions=(9,2,5)
    name=b'io.github.open-embedded-probe.declaration'

    def list_page(self, first, limit):
        entries=[]
        for fn in self.functions:
            revision=2 if fn==5 else 1
            instance=1 if fn==9 else 0
            entries.append(struct.pack('<HHBBB',fn,instance,revision,0,len(self.name))+self.name)
        page=bytearray(struct.pack('<HB',len(entries),0))
        for row in entries[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)

    def describe(self, fn):
        return (tlv(7,b'\x10\x0f'),tlv(2,struct.pack('<I',1000000)),tlv(3,struct.pack('<H',8)),
                tlv(4,struct.pack('<I',100)),tlv(5,struct.pack('<I',0)),
                tlv(1,struct.pack('<BH',1,0)+b'\x03'),tlv(1,struct.pack('<BH',1,0)+b'\x04'),
                tlv(6,b'\x01\x02'+struct.pack('<BH',1,0)+struct.pack('<BH',2,3)),
                tlv(0x7f,b'future extension'),tlv(0x40,b'x'*40))


class DeclarationModel(ResourceModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms,boot_id)
        self.extension=Declarations()
        self.ep.current_core=Core(self.ep,self.extension)
        rows=list(self.ep.static[0])
        rows=[row for row in rows if row[0]!=0x43]
        rows.append(tlv(0x43,struct.pack('<H',4)))
        self.ep.static[0]=tuple(rows)
