"""Logical standard attach/console fixture; no electrical target operations."""
import struct
from .virtual_core import Core, Completed, Reject, tlv
from .virtual_dependency_model import Dependencies, DependencyModel
from .virtual_stream_model import Stream


DPIDR=0x2ba01477                 # what the logical swd target's DPIDR reads (attach id, connections tid)


class Attachments(Dependencies):
    functions=(1,2,3,4)
    names=(b'oep.wire.rvswd',b'oep.wire.swio',b'oep.wire.swd',b'oep.target.console')
    pairs={1:[(0,1),(2,3),(6,7)],2:[(0,65535),(2,65535),(6,65535)],3:[(4,5)]}
    mechanisms=(0,1)
    reset_channel=8
    attach_halt=False            # rvswd / swio method 1: offered only with features bit0 attach_halt (debug §3)
    def methods(self,fn):return (0,1) if fn in (1,2) and self.attach_halt else (0,)
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
        if fn==4:return (tlv(7,b'\x10\xff'),tlv(64,bytes((len(self.mechanisms),*self.mechanisms))))
        rows=[tlv(7,b'\x10\x0e'),tlv(64,b'\x02'),tlv(4,struct.pack('<I',1000))]
        for index,pair in enumerate(self.pairs[fn]):
            members=[(1,pair[0])] if fn==2 else [(1,pair[0]),(2,pair[1])]
            rows.append(tlv(6,bytes((index,len(members)))+b''.join(struct.pack('<BH',*row) for row in members)))
        rows.append(tlv(1,struct.pack('<BH',3,self.reset_channel)+b'\x01'))   # the reset line: role 3 (debug §3)
        if fn in (1,2) and self.attach_halt:rows.append(tlv(5,struct.pack('<I',1)))   # features bit0 attach_halt
        return tuple(rows)
    def request_tags(self,fn,op,data):
        known={5:4} if fn<=3 and op==17 else {}
        result={}
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);value=data[3:3+size]
            if not raw&127 or len(value)!=size:raise Reject(3)
            data=data[3+size:];tag=raw&127
            if tag not in known:
                if raw&128:raise Reject(11,bytes((raw,)))
                continue
            if size!=known[tag]:raise Reject(3)                 # every copy is checked; the first is used
            channel,hold=struct.unpack('<HH',value)
            # not the reset line, or a hold past max_op_ms (debug §3, core §7.5): the tag as received, every copy
            if channel!=self.reset_channel or hold>self.ep.max_op_ms:raise Reject(11,bytes((raw,)))
            result.setdefault(tag,(raw,value))
        return result
    def close_stream(self,rid):
        super().close_stream(rid);del self.meta[rid]
    def close_connection(self,rid):
        for child,(parent,_) in list(self.streams.items()):
            if parent==rid:self.close_stream(child)
        del self.connections[rid];self.events.append(('connection',rid))
    def connection_page(self,fn,first):
        # tid: swd scheme 2, length 4, the DPIDR read at attach; the RISC-V wires read no target_id here (debug §2.1)
        rows=[struct.pack('<IHHIBB',rid,*s['pins'],s['speed'],*((2,4) if fn==3 else (0,0)))+(struct.pack('<I',DPIDR) if fn==3 else b'')
              for rid,s in self.connections.items() if s['fn']==fn]
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
                rid=struct.unpack('<I',payload)[0];self.resource(rid,'connection')
                if self.connections[rid]['fn']!=fn:raise Reject(4,tlv(1,b'\x06'))
                self.close_connection(rid);return b''
            method,swdio,swclk,speed=struct.unpack_from('<BHHI',payload);pair=(swdio,swclk)
            if not speed:raise Reject(3)                                  # max_speed 0 (debug §1)
            if method not in self.methods(fn):raise Reject(11,b'\0')    # method 1 without attach_halt too (debug §3)
            if speed<1000:raise Reject(11,b'\0')                         # below min_clock_hz: the fixed part
            if pair not in self.pairs[fn]:raise Reject(11,b'\0')
            active={pin for s in self.connections.values() for pin in s['pins'] if pin!=65535}
            if active.intersection(pair):raise Reject(4,tlv(1,b'\x01'))
            if sum(s['fn']==fn for s in self.connections.values())>=2:raise Reject(4,tlv(1,b'\x02'))
            if len(self.connections)+len(self.streams)>=8:raise Reject(4,tlv(1,b'\x02'))
            # All admission checks precede this logical stand-in for target access.
            hold=struct.unpack_from('<H',tags[5][1],2)[0] if 5 in tags else None
            self.touch.append((fn,pair,hold))
            if self.failure:return Completed(b'\x02'+tlv(127,b'x'),1)
            rid=self.allocate();chosen=1000 if fn==2 else min(speed,100000)
            self.connections[rid]={'fn':fn,'pins':pair,'speed':chosen}
            flags=8 if method else 0
            return struct.pack('<IIBI',rid,DPIDR if fn==3 else 0x200,flags,chosen)+(tlv(17,struct.pack('<I',0)) if method else b'')+tlv(127,b'x')
        if op==23:return self.page([struct.pack('<IIB',rid,parent,self.meta[rid]) for rid,(parent,_) in self.streams.items()],struct.unpack('<H',payload)[0])
        rid=struct.unpack_from('<I',payload)[0]
        if op==16:
            mechanism=payload[4]
            if mechanism not in self.mechanisms:raise Reject(11,b'\0')
            self.resource(rid,'connection')
            if self.connections[rid]['fn']==3 or any(parent==rid for parent,_ in self.streams.values()):raise Reject(4,tlv(1,b'\x06'))
            child=self.allocate();self.streams[child]=(rid,Stream(self.ep));self.meta[child]=mechanism
            return struct.pack('<I',child)+tlv(127,b'x')
        self.resource(rid,'stream');s=self.streams[rid][1]
        if op==22:self.close_stream(rid);return b''
        if op==21:
            if self.meta[rid]==0:raise Reject(11,b'\0')                     # an SDI stream takes no write (console §1)
            count=struct.unpack_from('<H',payload,4)[0];accepted=min(count,4-len(s.tx));s.tx.extend(payload[6:6+accepted])
            return Completed(struct.pack('<H',accepted),0 if accepted==count else (2 if accepted else 1))
        return s.dispatch(1,op-1,payload[4:])


class AttachModel(DependencyModel):
    def __init__(self,now_ms=None,boot_id=None):
        super().__init__(now_ms,boot_id);self.install()
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',9)),)   # 8: the reset line
    def install(self):self.extension=Attachments(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6 and struct.unpack_from('<H',request,3)[0]==4:
            e.fixed={16:5,17:15,18:8,19:4,20:5,21:6,22:4,23:2};e.free={17,18,23}
            if request[5]==21 and len(request)>=16:e.fixed[21]=6+struct.unpack_from('<H',request,14)[0]
        else:e.fixed={17:9,18:4,19:1};e.free={19}
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
