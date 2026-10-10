"""Logical persistent-fn positioned-stream fixture, without physical UART."""
import struct
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core, Reject, tlv


class Stream(Resources):
    functions=(1,)
    name=b'io.github.open-embedded-probe.stream'
    fixed={16:11,17:4,18:0,19:1,20:2}
    free={16,17}
    capacity=64
    mark_capacity=5

    def __init__(self, ep, position=0, serial=0):
        self.ep=ep;self.position=position;self.data=bytearray();self.marks=[];self.serial=serial;self.tx=bytearray()
        self.fixed=dict(type(self).fixed)

    def release(self):pass  # persistent fn: end never resets byte position or mark serial

    def describe(self, fn):return (tlv(7,b'\x10\x1f'),)

    def mark(self, kind, detail):
        self.marks.append((self.serial,self.position,kind,self.ep.now_ns(),detail))
        self.serial=(self.serial+1)&0xffffffff
        self.marks=self.marks[-self.mark_capacity:]

    def feed(self, data):
        if self.position+len(data)>0xffffffffffffffff:raise ValueError('position never wraps')
        self.data.extend(data);self.position+=len(data)
        if len(self.data)>self.capacity:
            del self.data[:-self.capacity];self.mark(5,1)

    def dispatch(self, fn, op, payload):
        suffix=tlv(0x7f,b'x')
        if op==16:
            mode,arg,maximum=struct.unpack('<BQH',payload)
            oldest=self.position-len(self.data)
            if mode==0:start=arg
            elif mode==1:start=oldest
            elif mode==2:start=self.position
            elif mode==3:
                selected=[row for row in self.marks if not arg or row[2]==arg]
                start=selected[-1][1] if selected else self.position
            else:raise Reject(11,b'\0')
            gap=start<oldest;start=min(self.position,max(oldest,start))
            size=min(maximum,self.position-start,self.ep.probe.max_frame-16-len(suffix))
            data=bytes(self.data[start-oldest:start-oldest+size])
            flags=int(start+size<self.position)|(int(gap)<<1)
            return struct.pack('<QBH',start,flags,len(data))+data+suffix
        if op==17:
            first=struct.unpack('<I',payload)[0]
            if first==self.serial or not self.marks:rows=[]
            else:
                index=next((i for i,row in enumerate(self.marks) if row[0]==first),0)
                rows=self.marks[index:]
            count=min(len(rows),(self.ep.probe.max_frame-7-len(suffix))//22)
            return bytes((int(count<len(rows)),count))+b''.join(struct.pack('<IQBQB',*row) for row in rows[:count])+suffix
        if op==18:self.data.clear();self.mark(6,0);return b''
        if op==19:self.mark(7,payload[0]);return b''
        count=struct.unpack_from('<H',payload)[0];self.tx.extend(payload[2:]);return struct.pack('<H',count)


class StreamModel(ResourceModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms,boot_id)
        self.extension=Stream(self.ep);self.ep.current_core=Core(self.ep,self.extension)

    def handle(self, request):
        # Counted write payload before TLVs. This logical fixture executes serially.
        write=len(request)>=12 and struct.unpack_from('<HB',request,3)==(1,20)
        if write:self.extension.fixed[20]=2+struct.unpack_from('<H',request,10)[0]
        try:return super().handle(request)
        finally:self.extension.fixed[20]=2

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
