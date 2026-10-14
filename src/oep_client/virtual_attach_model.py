"""Logical standard attach/console fixture; no electrical target operations."""
import struct
from .virtual_core import Core, Completed, Reject, tlv
from .virtual_dependency_model import Dependencies, DependencyModel
from .virtual_stream_model import Stream


class Attachments(Dependencies):
    functions=(1,2,3,4)
    names=(b'oep.wire.rvswd',b'oep.wire.swio',b'oep.wire.swd',b'oep.target.console')
    pairs={1:[(0,1),(2,3),(6,7)],2:[(0,65535),(2,65535),(6,65535)],3:[(4,5)]}
    mechanisms=(0,1)
    def __init__(self, ep):
        super().__init__(ep);self.connections={};self.meta={};self.touch=[];self.failure=False
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in zip(self.functions,self.names)]
        page=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==4:return (tlv(7,b'\x10\xff'),tlv(64,bytes(self.mechanisms)))
        rows=[tlv(7,b'\x10\x0e'),tlv(64,b'\x02'),tlv(4,struct.pack('<I',1000))]
        for index,pair in enumerate(self.pairs[fn]):
            members=[(1,pair[0])] if fn==2 else [(1,pair[0]),(2,pair[1])]
            rows.append(tlv(6,bytes((index,len(members)))+b''.join(struct.pack('<BH',*row) for row in members)))
        return tuple(rows)
    def request_tags(self,fn,op,data):
        known={1:4,3:4} if fn<=3 and op==17 else {}
        result={}
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);value=data[3:3+size]
            if not raw&127 or len(value)!=size:raise Reject(3)
            data=data[3+size:];tag=raw&127
            if tag in result:continue
            if tag not in known:
                if raw&128:raise Reject(11,bytes((raw,)))
                continue
            if size!=known[tag]:raise Reject(3)
            result[tag]=(raw,value)
        return result
    def close_stream(self,rid):
        super().close_stream(rid);del self.meta[rid]
    def close_connection(self,rid):
        for child,(parent,_) in list(self.streams.items()):
            if parent==rid:self.close_stream(child)
        del self.connections[rid];self.events.append(('connection',rid))
    def connection_page(self,fn,first):
        rows=[struct.pack('<HHHIBB',rid,*s['pins'],s['speed'],0,0) for rid,s in self.connections.items() if s['fn']==fn]
        return self.page(rows,first)
    def page(self,rows,first):
        page=bytearray(b'\0\0');index=first
        while index<len(rows) and len(page)+len(rows[index])+9<=self.ep.probe.max_frame:
            page.extend(rows[index]);page[1]+=1;index+=1
        page[0]=int(index<len(rows));return bytes(page)+tlv(127,b'x')
    def dispatch_request(self,fn,op,payload,tags):
        if fn<=3:
            if op==19:return self.connection_page(fn,payload[0])
            if op==18:
                rid=struct.unpack('<H',payload)[0];self.resource(rid,'connection')
                if self.connections[rid]['fn']!=fn:raise Reject(4,tlv(1,b'\x06'))
                self.close_connection(rid);return b''
            method=payload[0]
            if method not in ((0,) if fn==3 else (0,1)):raise Reject(11,b'\0')
            if 1 not in tags or 3 not in tags:raise Reject(3)
            speed=struct.unpack('<I',tags[1][1])[0];pair=struct.unpack('<HH',tags[3][1])
            if speed<1000:raise Reject(11,bytes((tags[1][0],)))
            if pair not in self.pairs[fn]:raise Reject(11,bytes((tags[3][0],)))
            active={pin for s in self.connections.values() for pin in s['pins'] if pin!=65535}
            if active.intersection(pair):raise Reject(4,tlv(1,b'\x01'))
            if sum(s['fn']==fn for s in self.connections.values())>=2:raise Reject(4,tlv(1,b'\x02'))
            if self.next_id>65535 or len(self.connections)+len(self.streams)>=8:raise Reject(4,tlv(1,b'\x02'))
            # All admission checks precede this logical stand-in for target access.
            self.touch.append((fn,pair))
            if self.failure:return Completed(b'\x02'+tlv(127,b'x'),1)
            rid=self.allocate();chosen=1000 if fn==2 else min(speed,100000)
            self.connections[rid]={'fn':fn,'pins':pair,'speed':chosen}
            flags=8 if method else 0
            return struct.pack('<HIBI',rid,0x200,flags,chosen)+(tlv(17,struct.pack('<I',0)) if method else b'')+tlv(127,b'x')
        if op==23:return self.page([struct.pack('<HHB',rid,parent,self.meta[rid]) for rid,(parent,_) in self.streams.items()],struct.unpack('<H',payload)[0])
        rid=struct.unpack_from('<H',payload)[0]
        if op==16:
            mechanism=payload[2]
            if mechanism not in self.mechanisms:raise Reject(11,b'\0')
            self.resource(rid,'connection')
            if self.connections[rid]['fn']==3 or any(parent==rid for parent,_ in self.streams.values()):raise Reject(4,tlv(1,b'\x06'))
            child=self.allocate();self.streams[child]=(rid,Stream(self.ep));self.meta[child]=mechanism
            return struct.pack('<HB',child,0)+tlv(127,b'x')
        self.resource(rid,'stream');s=self.streams[rid][1]
        if op==22:self.close_stream(rid);return b''
        if op==21:
            count=struct.unpack_from('<H',payload,2)[0];accepted=0 if self.meta[rid]==0 else min(count,4-len(s.tx));s.tx.extend(payload[4:4+accepted])
            return Completed(struct.pack('<H',accepted),0 if accepted==count else (2 if accepted else 1))
        return s.dispatch(1,op-1,payload[2:])


class AttachModel(DependencyModel):
    def __init__(self,now_ms=None,boot_id=None):
        super().__init__(now_ms,boot_id);self.install()
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',8)),)
    def install(self):self.extension=Attachments(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6 and struct.unpack_from('<H',request,3)[0]==4:
            e.fixed={16:3,17:13,18:6,19:2,20:3,21:4,22:2,23:2};e.free={17,18,23}
            if request[5]==21 and len(request)>=14:e.fixed[21]=4+struct.unpack_from('<H',request,12)[0]
        else:e.fixed={17:1,18:2,19:1};e.free={19}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def restart_fixture(self):
        if self.ep.current_core.holder is not None:raise ValueError('logical restart while owned')
        boot=(self.ep.boot_id+1)&0xffffffff;self.ep.reboot(boot);self.install();return {'scope':'logical-model','boot_id':boot}
    def fail_attach(self,value):
        if type(value)is not bool:raise ValueError('logical failure switch must be bool')
        self.extension.failure=value
    def attach_state(self):
        e=self.extension
        return {'connections':{rid:dict(row) for rid,row in e.connections.items()},'next_id':e.next_id,'touch':list(e.touch),'events':list(e.events),
                'streams':{rid:{'connection':parent,'mechanism':e.meta[rid],'position':s.position,'data':bytes(s.data),'tx':bytes(s.tx)} for rid,(parent,s) in e.streams.items()}}
