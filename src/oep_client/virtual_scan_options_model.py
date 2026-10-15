"""Logical finite scan selector and idle-level fixture; no target discovery."""
import copy
import struct
from .virtual_wire_options_model import WireOptions, WireOptionsModel
from .virtual_core import Core, Reject, tlv


class ScanOptions(WireOptions):
    def __init__(self,ep):super().__init__(ep);self.scan_limit=2;self.identities={};self.scan_live=set();self.scan_events=[]
    def describe(self,fn):return tuple(tlv(7,b'\x10\x0f') if fn<=3 and row[0]==7 else row for row in super().describe(fn))
    def request_tags(self,fn,op,data):
        if fn>3 or op!=16:return super().request_tags(fn,op,data)
        known={1:4}
        if fn==1:known[4]=1
        if fn==3:known[6]=4
        result={}
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);value=data[3:3+size];tag=raw&127
            if not tag or len(value)!=size:raise Reject(3)
            data=data[3+size:]
            if tag not in known:
                if raw&128:raise Reject(11,bytes((raw,)))
                continue
            if size!=known[tag]:raise Reject(3)                 # every copy's length is checked (core §2.3)
            if tag==1 and not struct.unpack('<I',value)[0]:raise Reject(3)   # max_speed 0 (debug §1)
            if tag in result and (tag==1 and struct.unpack('<I',value)[0]<1000 or tag==4 and value[0] not in (0,1)):
                raise Reject(11,bytes((raw,)))                  # a later copy's unhandled value (core §2.3)
            result.setdefault(tag,(raw,value))
        return result
    def dispatch_request(self,fn,op,payload,tags):
        if fn>3 or op!=16:return super().dispatch_request(fn,op,payload,tags)
        count=payload[0];pairs=[struct.unpack_from('<HH',payload,1+4*i) for i in range(count)]
        if not count or len(set(pairs))!=count:raise Reject(3)
        speed=struct.unpack('<I',tags[1][1])[0] if 1 in tags else 1000;idle=tags[4][1][0] if 4 in tags else 0;selector=struct.unpack('<I',tags[6][1])[0] if 6 in tags else None
        if speed<1000:raise Reject(11,bytes((tags[1][0],)))
        if idle not in (0,1):raise Reject(11,bytes((tags[4][0],)))
        active={pin for s in self.connections.values() for pin in s['pins'] if pin!=65535}
        for pair in pairs:
            if pair not in self.pairs[fn]:raise Reject(11,b'\0')
            if active.intersection(pair) or any(self.pin_modes[p]=='reserved' for p in pair if p!=65535):raise Reject(4,tlv(1,b'\x01'))
            if any(self.pin_modes[p]!='free' for p in pair if p!=65535):raise Reject(4,tlv(1,b'\x05'))
        tried=min(count,self.scan_limit);rows=[];levels=[1,1-idle] if fn==1 else [1] if fn==2 else [0,1]
        for pair in pairs[:tried]:
            pins=[p for p in pair if p!=65535];self.scan_live.update(pins);self.scan_events.append(('reserve',fn,pair,selector))
            try:
                for pin,level in zip(pins,levels):self.pin_levels[pin]=dict(mode='wire',level=level)
                self.scan_events.append(('drive',fn,pair,list(levels)))
                if selector is not None:self.scan_events.append(('select',fn,selector))
                identity=self.identities.get((fn,pair,selector));chosen=min(speed,100000);self.scan_events.append(('read',fn,pair,selector,chosen,identity,[self.pin_levels[p]['level'] for p in pins]));self.clock_ms+=1
                if identity is not None and (fn==3 or 2<=identity&15<15):rows.append(struct.pack('<BHHI',2 if fn==3 else 1,*pair,identity))
            finally:
                for pin in pins:self.pin_levels[pin]=dict(mode='free',level=None)
                self.scan_live.difference_update(pins);self.scan_events.append(('release',fn,pair))
        return bytes((tried,len(rows)))+b''.join(rows)+tlv(127,b'x')


class ScanOptionsModel(WireOptionsModel):
    def install(self):self.extension=ScanOptions(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        if len(request)>=6 and struct.unpack_from('<H',request,3)[0]==4:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free;e.fixed={16:1,17:9,18:4,19:1};e.free={19}
        if len(request)>=11 and request[5]==16:e.fixed[16]=1+4*request[10]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def scan_options_state(self):
        state=self.wire_options_state();e=self.extension;state.update(scan_limit=e.scan_limit,identities=copy.deepcopy(e.identities),scan_live=sorted(e.scan_live),scan_events=copy.deepcopy(e.scan_events));return state
    def scan_limit(self,count):
        if type(count)is not int or not 1<=count<=3:raise ValueError('explicit finite scan limit 1..3 required')
        self.extension.scan_limit=count
    def scan_identity(self,fn,pair,selector,value):
        if type(fn)is not int or fn not in (1,2,3) or type(pair)is not tuple or len(pair)!=2 or any(type(p)is not int for p in pair) or pair not in self.extension.pairs[fn] or selector is not None and (fn!=3 or type(selector)is not int or not 0<=selector<=0xffffffff) or value is not None and (type(value)is not int or not 0<=value<=0xffffffff):raise ValueError('declared candidate, explicit SWD selector and u32 identity/None required')
        self.extension.identities[fn,pair,selector]=value
