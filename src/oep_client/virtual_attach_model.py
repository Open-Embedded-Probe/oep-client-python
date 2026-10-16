"""Logical standard attach/console fixture; no electrical target operations."""
import struct
from .virtual_core import Core, Completed, Reject, tlv
from .virtual_dependency_model import Dependencies, DependencyModel
from .virtual_stream_model import Stream
from . import registry as _reg
from .catalog import pack_ops
from .virtual_names import R, DC, cause, extension

# the wire family's and the console's numbers by name (debug §1-§3, console §1-§3)
WOP, WT, WE = _reg.WIRE_RVSWD.op, _reg.WIRE_RVSWD.tlv, _reg.WIRE_RVSWD.enum
COP, CD = _reg.TARGET_CONSOLE.op, _reg.TARGET_CONSOLE.tlv["describe"]
SDI = "sdi"


DPIDR=0x2ba01477                 # what the logical swd target's DPIDR reads (attach id, connections tid)


class Attachments(Dependencies):
    functions=(1,2,3,4)
    names=(b'oep.wire.rvswd',b'oep.wire.swio',b'oep.wire.swd',b'oep.target.console')
    pairs={1:[(0,1),(2,3),(6,7)],2:[(0,65535),(2,65535),(6,65535)],3:[(4,5)]}
    mechanisms=("sdi","dmdata")   # a named list (core §7.7): number = place from 1
    reset_channel=8
    attach_halt=False            # rvswd / swio method 1: offered only with features bit0 attach_halt (debug §3)
    def methods(self,fn):return tuple(WE['attach_method'].values()) if fn in (1,2) and self.attach_halt else (WE['attach_method']['run'],)
    def mechanism_numbers(self):return {i+1:name for i,name in enumerate(self.mechanisms)}
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
        if fn==4:return (tlv(DC['ops'],pack_ops(set(COP.values()))),*(tlv(CD['mechanism'],name.encode()) for name in self.mechanisms))
        rows=[tlv(DC['ops'],pack_ops({WOP['attach'],WOP['detach'],WOP['connections'],WOP['holds']})),tlv(WT['describe']['max_connections'],b'\x02'),tlv(DC['min_clock_hz'],struct.pack('<I',1000))]
        for index,pair in enumerate(self.pairs[fn]):
            members=[(WE['pin_role']['swdio'],pair[0])] if fn==2 else [(WE['pin_role']['swdio'],pair[0]),(WE['pin_role']['swclk'],pair[1])]
            rows.append(tlv(DC['channel_group'],bytes((index,len(members)))+b''.join(struct.pack('<BH',*row) for row in members)))
        rows.append(tlv(DC['role_channels'],struct.pack('<BH',WE['pin_role']['reset'],self.reset_channel)+b'\x01'))   # the reset line (debug §3)
        if fn in (1,2) and self.attach_halt:rows.append(tlv(DC['features'],struct.pack('<I',WE['features']['attach_halt'])))
        return tuple(rows)
    def request_tags(self,fn,op,data):
        known={WT['attach']['reset']:4} if fn<=3 and op==WOP['attach'] else {}
        result={}
        while data:
            if len(data)<3:raise Reject(R['malformed'])
            raw,size=struct.unpack_from('<BH',data);value=data[3:3+size]
            if not raw&127 or len(value)!=size:raise Reject(R['malformed'])
            data=data[3+size:];tag=raw&127
            if tag not in known:
                if raw&128:raise Reject(R['unsupported'],bytes((raw,)))
                continue
            if size!=known[tag]:raise Reject(R['malformed'])                 # every copy is checked; the first is used
            channel,hold=struct.unpack('<HH',value)
            # not the reset line, or a hold past max_op_ms (debug §3, core §7.5): the tag as received, every copy
            if channel!=self.reset_channel or hold>self.ep.max_op_ms:raise Reject(R['unsupported'],bytes((raw,)))
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
        page[0]=int(index<len(rows));return bytes(page)+extension()
    def dispatch_request(self,fn,op,payload,tags):
        if fn<=3:
            if op==WOP['connections']:return self.connection_page(fn,payload[0])
            if op==WOP['holds']:return b'\0\0'+extension()   # no port held in this model (debug §2.2)
            if op==WOP['detach']:
                rid=struct.unpack('<I',payload)[0];self.resource(rid,'connection')
                if self.connections[rid]['fn']!=fn:raise Reject(R['unavailable'],cause('wrong_state'))
                self.close_connection(rid);return b''
            method,swdio,swclk,speed=struct.unpack_from('<BHHI',payload);pair=(swdio,swclk)
            if not speed:raise Reject(R['malformed'])                     # max_speed 0 (debug §1)
            if method not in self.methods(fn):raise Reject(R['unsupported'],b'\0')   # method halt without attach_halt too (debug §3)
            if speed<1000:raise Reject(R['unsupported'],b'\0')            # below min_clock_hz: the fixed part
            if pair not in self.pairs[fn]:raise Reject(R['unsupported'],b'\0')
            active={pin for s in self.connections.values() for pin in s['pins'] if pin!=65535}
            if active.intersection(pair):raise Reject(R['unavailable'],cause('pin_in_use'))
            if sum(s['fn']==fn for s in self.connections.values())>=2:raise Reject(R['unavailable'],cause('limit'))
            if len(self.connections)+len(self.streams)>=8:raise Reject(R['unavailable'],cause('limit'))
            # All admission checks precede this logical stand-in for target access.
            hold=struct.unpack_from('<H',tags[WT['attach']['reset']][1],2)[0] if WT['attach']['reset'] in tags else None
            self.touch.append((fn,pair,hold))
            if self.failure:return Completed(bytes((_reg.STATUS['line'],))+extension(),_reg.OUTCOMES['failed'])
            rid=self.allocate();chosen=1000 if fn==2 else min(speed,100000)
            self.connections[rid]={'fn':fn,'pins':pair,'speed':chosen}
            flags=WE['attach_flags']['halted'] if method else 0
            return struct.pack('<IIBI',rid,DPIDR if fn==3 else 0x200,flags,chosen)+(tlv(WT['attach_answer']['dpc'],struct.pack('<I',0)) if method else b'')+extension()
        if op==COP['streams']:return self.page([struct.pack('<IIB',rid,parent,self.meta[rid]) for rid,(parent,_) in self.streams.items()],struct.unpack('<H',payload)[0])
        rid=struct.unpack_from('<I',payload)[0]
        if op==COP['open']:
            mechanism=payload[4]
            if mechanism not in self.mechanism_numbers():raise Reject(R['unsupported'],b'\0')
            self.resource(rid,'connection')
            if self.connections[rid]['fn']==3 or any(parent==rid for parent,_ in self.streams.values()):raise Reject(R['unavailable'],cause('wrong_state'))
            child=self.allocate();self.streams[child]=(rid,Stream(self.ep));self.meta[child]=mechanism
            return struct.pack('<I',child)+extension()
        self.resource(rid,'stream');s=self.streams[rid][1]
        if op==COP['close']:self.close_stream(rid);return b''
        if op==COP['write']:
            if self.mechanism_numbers()[self.meta[rid]]==SDI:raise Reject(R['unsupported'],b'\0')   # an SDI stream takes no write (console §1)
            count=struct.unpack_from('<H',payload,4)[0];accepted=min(count,4-len(s.tx));s.tx.extend(payload[6:6+accepted])
            O=_reg.OUTCOMES
            return Completed(struct.pack('<H',accepted),O['success'] if accepted==count else (O['partial'] if accepted else O['failed']))
        name=next(k for k,v in COP.items() if v==op)                # the console op by name: the stream's own op
        return s.dispatch(1,Stream.OPS[name],payload[4:])


class AttachModel(DependencyModel):
    def __init__(self,now_ms=None,boot_id=None):
        super().__init__(now_ms,boot_id);self.install()
        CH=_reg.CORE.tlv['describe']['channels']
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=CH)+(tlv(CH,struct.pack('<H',9)),)   # 8: the reset line
    def install(self):self.extension=Attachments(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6 and struct.unpack_from('<H',request,3)[0]==4:
            e.fixed={COP['open']:5,COP['read']:15,COP['marks']:8,COP['clear']:4,COP['mark']:5,COP['write']:6,COP['close']:4,COP['streams']:2};e.free={COP['read'],COP['marks'],COP['streams']}
            if request[5]==COP['write'] and len(request)>=16:e.fixed[COP['write']]=6+struct.unpack_from('<H',request,14)[0]
        else:e.fixed={WOP['attach']:9,WOP['detach']:4,WOP['connections']:1,WOP['holds']:1};e.free={WOP['connections'],WOP['holds']}
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
