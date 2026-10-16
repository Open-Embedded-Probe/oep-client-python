"""Logical persistent-fn positioned-stream fixture, without physical UART."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, Reject, tlv
from . import registry as _reg
from .catalog import pack_ops
from .virtual_names import R, DC, extension

MARK = _reg.COMMON.enum["mark_kind"]                     # lost, clear, host (common §1.3)
LOST = _reg.COMMON.enum["mark_detail_lost"]
FROM = _reg.COMMON.enum["read_from"]                     # position, oldest, now, last_mark (common §1.2)


class Stream(Resources):
    functions=(1,)
    name=b'io.github.open-embedded-probe.stream'
    # this test interface's own ops (its name's definition, core §12 rule 7): the common §1 stream ops
    OPS=dict(read=0x10,marks=0x11,clear=0x12,mark=0x13,write=0x14)
    fixed={OPS['read']:11,OPS['marks']:4,OPS['clear']:0,OPS['mark']:1,OPS['write']:2}
    free={OPS['read'],OPS['marks']}
    capacity=64
    mark_capacity=5

    def __init__(self, ep, position=0, serial=0):
        self.ep=ep;self.position=position;self.data=bytearray();self.marks=[];self.serial=serial;self.tx=bytearray()
        self.fixed=dict(type(self).fixed)

    def release(self):pass  # persistent fn: end never resets byte position or mark serial

    def describe(self, fn):return (tlv(DC['ops'],pack_ops(set(self.OPS.values()))),)

    def mark(self, kind, detail):
        self.marks.append((self.serial,self.position,kind,self.ep.now_ns(),detail))
        self.serial=(self.serial+1)&0xffffffff
        self.marks=self.marks[-self.mark_capacity:]

    def feed(self, data):
        if self.position+len(data)>0xffffffffffffffff:raise ValueError('position never wraps')
        self.data.extend(data);self.position+=len(data)
        if len(self.data)>self.capacity:
            del self.data[:-self.capacity];self.mark(MARK['lost'],LOST['overflow'])   # common §1.3

    def dispatch(self, fn, op, payload):
        suffix=extension(b'io.x',b'')       # a short named extension the reader skips (core §2.2), keeping the fixture's page geometry
        if op==self.OPS['read']:
            mode,arg,maximum=struct.unpack('<BQH',payload)
            oldest=self.position-len(self.data)
            if mode==FROM['position']:start=arg
            elif mode==FROM['oldest']:start=oldest
            elif mode==FROM['now']:start=self.position
            elif mode==FROM['last_mark']:
                selected=[row for row in self.marks if not arg or row[2]==arg]
                start=selected[-1][1] if selected else self.position
            else:raise Reject(R['unsupported'],b'\0')
            gap=start<oldest;start=min(self.position,max(oldest,start))
            size=min(maximum,self.position-start,self.ep.probe.max_frame-16-len(suffix))
            data=bytes(self.data[start-oldest:start-oldest+size])
            flags=int(start+size<self.position)|(int(gap)<<1)
            return struct.pack('<QBH',start,flags,len(data))+data+suffix
        if op==self.OPS['marks']:
            first=struct.unpack('<I',payload)[0]
            if first==self.serial or not self.marks:rows=[]
            else:
                index=next((i for i,row in enumerate(self.marks) if row[0]==first),0)
                rows=self.marks[index:]
            count=min(len(rows),(self.ep.probe.max_frame-7-len(suffix))//22)
            return bytes((int(count<len(rows)),count))+b''.join(struct.pack('<IQBQB',*row) for row in rows[:count])+suffix
        if op==self.OPS['clear']:self.data.clear();self.mark(MARK['clear'],0);return b''
        if op==self.OPS['mark']:self.mark(MARK['host'],payload[0]);return b''
        count=struct.unpack_from('<H',payload)[0];self.tx.extend(payload[2:]);return struct.pack('<H',count)


class StreamModel(ResourceModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms,boot_id)
        self.extension=Stream(self.ep);self.ep.current_core=Core(self.ep,self.extension)

    def handle(self, request):
        # Counted write payload before TLVs. This logical fixture executes serially.
        W=Stream.OPS['write']
        write=len(request)>=12 and struct.unpack_from('<HB',request,3)==(1,W)
        if write:self.extension.fixed[W]=2+struct.unpack_from('<H',request,10)[0]
        try:return super().handle(request)
        finally:self.extension.fixed[W]=2

    def restart_fixture(self, position, serial):
        if self.ep.current_core.holder is not None:raise ValueError('fixture reset while owned')
        if type(position) is not int or not 0<=position<=0xffffffffffffffff or type(serial) is not int or not 0<=serial<=0xffffffff:
            raise ValueError('explicit u64/u32 seed required')
        boot=(self.ep.boot_id+1)&0xffffffff
        self.ep.reboot(boot)
        self.extension=Stream(self.ep,position,serial);self.ep.current_core=Core(self.ep,self.extension)
        return {'scope':'logical-model','boot_id':boot,'position':position,'next_serial':serial}

    def feed(self, data):self.extension.feed(data)
    def mark_external(self, kind, detail):self.extension.mark(kind,detail)
    def stream_state(self):
        s=self.extension
        return {'position':s.position,'data':bytes(s.data),'marks':list(s.marks),'next_serial':s.serial,'tx':bytes(s.tx),'boot_id':self.ep.boot_id}
