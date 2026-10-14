"""Finite logical wire scan fixture, with explicit identities and pin admission."""
import struct
from .virtual_attach_model import Attachments, AttachModel
from .virtual_core import Core, Reject, tlv


class Scans(Attachments):
    def __init__(self, ep):
        super().__init__(ep)
        self.budget=2;self.ids={};self.pin_modes={};self.scan_live=set();self.scan_pin_events=[]
    def describe(self, fn):
        return tuple(tlv(7,b'\x10\x0f') if fn<=3 and row[0]==7 else row for row in super().describe(fn))
    def request_tags(self,fn,op,data):
        if fn>3 or op!=16:return super().request_tags(fn,op,data)
        result={}
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);value=data[3:3+size]
            if not raw&127 or len(value)!=size:raise Reject(3)
            data=data[3+size:];tag=raw&127
            if tag in result:continue
            if tag!=1:
                if raw&128:raise Reject(11,bytes((raw,)))
                continue
            if size!=4:raise Reject(3)
            result[tag]=(raw,value)
        return result
    def dispatch_request(self,fn,op,payload,tags):
        if fn>3 or op!=16:return super().dispatch_request(fn,op,payload,tags)
        count=payload[0];pairs=[struct.unpack_from('<HH',payload,1+4*i) for i in range(count)]
        if not count or len(set(pairs))!=count:raise Reject(3)
        speed=struct.unpack('<I',tags[1][1])[0] if 1 in tags else 1000
        if speed<1000:raise Reject(11,bytes((tags[1][0],)))
        active={pin for row in self.connections.values() for pin in row['pins'] if pin!=65535}
        for pair in pairs:
            if pair not in self.pairs[fn]:raise Reject(11,b'\0')
            if active.intersection(pair):raise Reject(4,tlv(1,b'\x01'))
            if any(pin in self.pin_modes for pin in pair):raise Reject(4,tlv(1,b'\x05'))
        tried=min(count,self.budget);rows=[]
        for pair in pairs[:tried]:
            pins=set(pair)-{65535};self.scan_live.update(pins);self.scan_pin_events.append(('reserve',fn,pair))
            try:
                self.touch.append(('scan',fn,pair,min(speed,100000)))
                identity=self.ids.get((fn,pair))
                if identity is not None and (fn==3 or 2<=identity&15<15):
                    rows.append(struct.pack('<BHHI',2 if fn==3 else 1,*pair,identity))
            finally:
                self.scan_live.difference_update(pins);self.scan_pin_events.append(('idle',fn,pair))
        return bytes((tried,len(rows)))+b''.join(rows)+tlv(127,b'x')


class ScanModel(AttachModel):
    def install(self):self.extension=Scans(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        if len(request)>=6 and struct.unpack_from('<H',request,3)[0]==4:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free
        e.fixed={16:1,17:9,18:4,19:1};e.free={19}
        if len(request)>=11 and request[5]==16:e.fixed[16]=1+4*request[10]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def scan_budget(self,limit):
        if type(limit)is not int or not 1<=limit<=3:raise ValueError('finite scan budget must be 1..3')
        self.extension.budget=limit
    def scan_id(self,fn,pair,identity):
        if fn not in (1,2,3) or pair not in self.extension.pairs[fn]:raise ValueError('undeclared logical candidate')
        if identity is not None and (type(identity)is not int or not 0<=identity<=0xffffffff):raise ValueError('identity must be u32 or None')
        self.extension.ids[fn,pair]=identity
    def pin_mode(self,pin,mode):
        if type(pin)is not int or not 0<=pin<8 or mode not in ('disabled','output'):raise ValueError('invalid logical pin mode')
        self.extension.pin_modes[pin]=mode
    def scan_state(self):
        e=self.extension;state=self.attach_state()
        state.update(budget=e.budget,identities={str(key):value for key,value in e.ids.items()},pin_modes=dict(e.pin_modes),
                     scan_live=sorted(e.scan_live),scan_pin_events=list(e.scan_pin_events))
        return state
