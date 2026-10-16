"""Read-only standard declaration fixture, not a functional wire/console probe."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, Reject, tlv
from . import registry as _reg
from .catalog import pack_ops
from .virtual_names import R, DC, extension

WOP, WT, WE = _reg.WIRE_RVSWD.op, _reg.WIRE_RVSWD.tlv, _reg.WIRE_RVSWD.enum
COP, CD = _reg.TARGET_CONSOLE.op, _reg.TARGET_CONSOLE.tlv['describe']


class OepDeclarations(Resources):
    functions=(1,2,3,4)
    names=(b'oep.wire.rvswd',b'oep.wire.swio',b'oep.wire.swd',b'oep.target.console')

    def list_page(self, first, limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in zip(self.functions,self.names)]
        page=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)

    def describe(self, fn):
        if fn==4:return (tlv(DC['ops'],pack_ops(set(COP.values()))),tlv(CD['mechanism'],b'dmdata'))
        rows=[tlv(DC['ops'],pack_ops({WOP['attach'],WOP['detach'],WOP['connections'],WOP['holds']}))]
        if fn==1:rows.append(tlv(WT['describe']['max_connections'],b'\x02'))
        if fn==2:rows.append(tlv(DC['min_clock_hz'],struct.pack('<I',1000000)))
        role=WE['pin_role']
        if fn==3:rows.append(tlv(DC['channel_group'],b'\x01\x02'+struct.pack('<BH',role['swdio'],2)+struct.pack('<BH',role['swclk'],3)))
        else:
            rows.append(tlv(DC['role_channels'],struct.pack('<BH',role['swdio'],0)+b'\x01'))
            if fn!=2:rows.append(tlv(DC['role_channels'],struct.pack('<BH',role['swclk'],0)+b'\x02'))
        return tuple(rows)

    def dispatch(self, fn, op, payload):
        if (fn!=4 and op in (WOP['connections'],WOP['holds'])) or (fn==4 and op==COP['streams']):return b'\0\0'+extension()
        raise Reject(R['no_resource'])  # declarations only: no simulated attach or target work


class OepDeclarationModel(ResourceModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms,boot_id);self.extension=OepDeclarations();self.ep.current_core=Core(self.ep,self.extension)
        CH=_reg.CORE.tlv['describe']['channels']
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=CH)+(tlv(CH,struct.pack('<H',4)),)

    def handle(self, request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn=struct.unpack_from('<H',request,3)[0]
            if fn==4:e.fixed={COP['open']:5,COP['read']:15,COP['marks']:8,COP['clear']:4,COP['mark']:5,COP['write']:6,COP['close']:4,COP['streams']:2};e.free={COP['read'],COP['marks'],COP['streams']}
            else:e.fixed={WOP['attach']:9,WOP['detach']:4,WOP['connections']:1,WOP['holds']:1};e.free={WOP['connections'],WOP['holds']}
        try:return super().handle(request)
        finally:e.fixed,e.free=fixed,free
