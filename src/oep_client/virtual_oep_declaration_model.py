"""Read-only standard declaration fixture, not a functional wire/console probe."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, Reject, tlv


class OepDeclarations(Resources):
    functions=(1,2,3,4)
    names=(b'oep.wire.rvswd',b'oep.wire.swio',b'oep.wire.swd',b'oep.target.console')

    def list_page(self, first, limit):
        rows=[struct.pack('<HHBBB',fn,0,1,0,len(name))+name for fn,name in zip(self.functions,self.names)]
        page=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)

    def describe(self, fn):
        if fn==4:return (tlv(7,b'\x10\xff'),tlv(64,b'\x01'))
        rows=[tlv(7,b'\x10\x0e')]
        if fn==1:rows.append(tlv(64,b'\x02'))
        if fn==2:rows.append(tlv(4,struct.pack('<I',1000000)))
        if fn==3:rows.append(tlv(6,b'\x01\x02'+struct.pack('<BH',1,2)+struct.pack('<BH',2,3)))
        else:
            rows.append(tlv(1,struct.pack('<BH',1,0)+b'\x01'))
            if fn!=2:rows.append(tlv(1,struct.pack('<BH',2,0)+b'\x02'))
        return tuple(rows)

    def dispatch(self, fn, op, payload):
        if (fn!=4 and op==19) or (fn==4 and op==23):return b'\0\0'+tlv(127,b'x')
        raise Reject(10)  # declarations only: no simulated attach or target work


class OepDeclarationModel(ResourceModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms,boot_id);self.extension=OepDeclarations();self.ep.current_core=Core(self.ep,self.extension)
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',4)),)

    def handle(self, request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn=struct.unpack_from('<H',request,3)[0]
            if fn==4:e.fixed={16:3,17:13,18:6,19:2,20:3,21:4,22:2,23:2};e.free={17,18,23}
            else:e.fixed={17:1,18:2,19:1};e.free={19}
        try:return super().handle(request)
        finally:e.fixed,e.free=fixed,free
