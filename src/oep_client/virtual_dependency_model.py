"""Independent logical connection/stream dependencies; no target or pins."""
import struct
from .virtual_core import Core, Completed, Reject, tlv
from .virtual_resource_model import ResourceModel, Resources
from .virtual_stream_model import Stream


class Dependencies(Resources):
    functions=(1,2)
    fixed={16:0,17:2,18:2,19:2}
    free={19}
    stream_fixed={16:2,17:13,18:6,19:2,20:3,21:4,22:2,23:2}
    names=(b'io.github.open-embedded-probe.link',b'io.github.open-embedded-probe.resource-stream')

    def __init__(self, ep):
        self.ep=ep;self.next_id=1;self.connections=[];self.streams={};self.events=[]
        self.fixed=dict(type(self).fixed);self.free=set(type(self).free)

    def list_page(self, first, limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in zip(self.functions,self.names)]
        result=bytearray(b'\x02\0\0')
        for row in rows[first:]:
            if len(result)+len(row)>limit:break
            result.extend(row);result[2]+=1
        return bytes(result)

    def describe(self, fn):return (tlv(7,b'\x10\x0f' if fn==1 else b'\x10\xff'),)

    def allocate(self):
        if self.next_id>65535 or len(self.connections)+len(self.streams)>=8:raise Reject(4,tlv(1,b'\x02'))
        rid=self.next_id;self.next_id+=1;return rid

    def resource(self, rid, kind):
        desired=self.connections if kind=='connection' else self.streams
        if rid in desired:return
        if rid in self.connections or rid in self.streams:raise Reject(4,tlv(1,b'\x06'))
        raise Reject(10)

    def close_stream(self, rid):
        del self.streams[rid];self.events.append(('stream',rid))

    def close_connection(self, rid):
        for child,(parent,_) in list(self.streams.items()):
            if parent==rid:self.close_stream(child)
        self.connections.remove(rid);self.events.append(('connection',rid))

    def release(self):
        for parent in list(self.connections):self.close_connection(parent)

    def listing(self, group, first):
        rows=[struct.pack('<H',rid) for rid in self.connections] if group=='connection' else [struct.pack('<HH',rid,parent) for rid,(parent,_) in self.streams.items()]
        page=bytearray(b'\0\0');index=first
        while index<len(rows) and len(page)+len(rows[index])+5+4<=self.ep.probe.max_frame:
            page.extend(rows[index]);page[1]+=1;index+=1
        page[0]=int(index<len(rows));return bytes(page)+tlv(0x7f,b'x')

    def dispatch(self, fn, op, payload):
        if fn==1:
            if op==16:
                rid=self.allocate();self.connections.append(rid);return struct.pack('<H',rid)
            rid=struct.unpack('<H',payload)[0]
            if op==19:return self.listing('connection',rid)
            self.resource(rid,'connection')
            if op==17:self.close_connection(rid)
            return b''
        rid=struct.unpack_from('<H',payload)[0]
        if op==23:return self.listing('stream',rid)
        if op==16:
            self.resource(rid,'connection');child=self.allocate();self.streams[child]=(rid,Stream(self.ep));return struct.pack('<H',child)
        self.resource(rid,'stream');s=self.streams[rid][1]
        if op==22:self.close_stream(rid);return b''
        if op==21:
            count=struct.unpack_from('<H',payload,2)[0];accepted=min(count,4-len(s.tx));s.tx.extend(payload[4:4+accepted])
            return Completed(struct.pack('<H',accepted),0 if accepted==count else (2 if accepted else 1))
        return s.dispatch(1,op-1,payload[2:])


class DependencyModel(ResourceModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms,boot_id);self.extension=Dependencies(self.ep);self.ep.current_core=Core(self.ep,self.extension)

    def handle(self, request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6 and struct.unpack_from('<H',request,3)[0]==2:
            e.fixed=dict(e.stream_fixed);e.free={17,18,23}
            if request[5]==21 and len(request)>=14:e.fixed[21]=4+struct.unpack_from('<H',request,12)[0]
        try:return super().handle(request)
        finally:e.fixed,e.free=fixed,free

    def restart_fixture(self):
        if self.ep.current_core.holder is not None:raise ValueError('logical restart while owned')
        boot=(self.ep.boot_id+1)&0xffffffff;self.ep.reboot(boot)
        self.extension=Dependencies(self.ep);self.ep.current_core=Core(self.ep,self.extension)
        return {'scope':'logical-model','boot_id':boot}

    def feed(self, rid, data):self.extension.resource(rid,'stream');self.extension.streams[rid][1].feed(data)

    def reset_target(self, rid):
        self.extension.resource(rid,'connection')
        for parent,s in self.extension.streams.values():
            if parent==rid:s.mark(2,1)

    def break_connection(self, rid):self.extension.resource(rid,'connection');self.extension.close_connection(rid)

    def dependency_state(self):
        e=self.extension
        return {'boot_id':self.ep.boot_id,'next_id':e.next_id,'connections':list(e.connections),'events':list(e.events),
                'streams':{rid:{'connection':parent,'position':s.position,'data':bytes(s.data),'tx':bytes(s.tx),
                                'marks':list(s.marks),'next_serial':s.serial} for rid,(parent,s) in e.streams.items()}}
