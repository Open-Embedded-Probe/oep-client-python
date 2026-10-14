"""Current probe.config items (label/idle/disable), change generation and RAM-backed saving, composed with live GPIO/plan."""
import copy
import struct
from .virtual_core import Core,Reject,tail,tlv
from .virtual_gpio_model import Gpio,GpioModel


LABEL,IDLE,DISABLE=2,3,7
DECLARED=(LABEL,IDLE,DISABLE)
FIXED={16:2,17:0,18:0,19:0,20:0,21:0};FREE={16,21}          # set/unset parse their own count(u8) in request_tags


def encoded(saved):
    # A saved container as JSON-friendly rows; a torn container is shown as its repr.
    if saved is None:return None
    try:return [[t,v.hex()] for t,v in saved]
    except (TypeError,ValueError,AttributeError):return repr(saved)


def unavailable(cause,ch=None):return Reject(4,tlv(1,bytes((cause,)))+(b'' if ch is None else tlv(2,struct.pack('<H',ch))))


class ConfigExtension(Gpio):
    functions=(1,2,3,4)
    def __init__(self,model):
        super().__init__();self.model=model;self.items={};self.generation=1
        self.functions=tuple(model.catalog)
        self.idle={ch:0 for ch in range(8)};self.pins=dict(self.idle)
        self.restore_saved();self.apply()
    def form(self,tag,value):
        # A known item's channel; another length (or a label that is not text) is malformed (§1).
        ok=3<=len(value)<=34 if tag==LABEL else len(value)==(3 if tag==IDLE else 2)
        if ok and tag==LABEL:
            try:ok=not any(c<32 or c==127 for c in value[2:]) and bool(value[2:].decode('utf-8'))
            except UnicodeDecodeError:ok=False
        if not ok:raise Reject(3)
        return int.from_bytes(value[:2],'little')
    def refused(self,tag,ch,value):return ch>=self.model.channels or tag==IDLE and value[2]>4
    def restore_saved(self):
        # The RAM copy is a fixture container, not a prescribed storage serialization (§3).
        m=self.model;m.storage_state=m.storage_reason=m.saved_generation=0
        if m.saved is None:return
        if not isinstance(m.saved,list) or not all(type(r) is tuple and len(r)==2 and type(r[0]) is int and 1<=r[0]<=127 and type(r[1]) is bytes and len(r[1])<=65535 for r in m.saved):
            m.storage_state,m.storage_reason=2,1;return               # the container itself: nothing applied
        bad=short=False;rows=[]
        for tag,value in m.saved:
            try:ch=self.form(tag,value) if tag in DECLARED else None
            except Reject:ch=None
            if ch is None or self.refused(tag,ch,value):bad=True;continue
            rows.append((tag,ch,value))
        keys=[(t,ch) for t,ch,v in rows];twice={k for k in keys if keys.count(k)>1}
        bad|=bool(twice);order={DISABLE:0,IDLE:1,LABEL:2}
        for tag,ch,value in sorted((r for r in rows if r[:2] not in twice),key=lambda r:(order[r[0]],r[1])):
            if tag==IDLE and (DISABLE,ch) in self.items:bad=True;continue   # disable wins over idle on one channel
            if m.hold_max is not None and len(self.items)>=m.hold_max:short=True;continue
            self.items[tag,ch]=value
        m.storage_state,m.storage_reason=(3,2) if bad else (3,3) if short else (1,0)
        m.saved_generation=int(not bad and not short)
    def list_page(self,first,limit):
        rows=[]
        for fn,(name,instance,revision) in sorted(self.model.catalog.items()):
            n=name.encode('ascii');rows.append(struct.pack('<HHBB',fn,instance,revision,len(n))+n)
        out=bytearray(struct.pack('<HB',len(rows),0))
        for row in rows[first:]:
            if len(out)+len(row)>limit:break
            out.extend(row);out[2]+=1
        return bytes(out)
    def describe(self,fn):
        if fn==4:return (tlv(7,b'\x10\x3f'),tlv(64,struct.pack('<I',64)),tlv(65,bytes(DECLARED)))
        return super().describe(fn)
    def request_tags(self,fn,op,data):
        if fn!=4:return super().request_tags(fn,op,data)
        if op not in (17,20):tail(data);return []
        if not data:raise Reject(3)
        n,data=data[0],data[1:];rows=[]
        for _ in range(n):
            if len(data)<3:raise Reject(3)
            if op==20:raw,value=struct.unpack_from('<BH',data);data=data[3:]
            else:
                raw,size=struct.unpack_from('<BH',data);value,data=data[3:3+size],data[3+size:]
                if len(value)!=size:raise Reject(3)
            if not raw&127:raise Reject(3)                               # item number 0
            rows.append((raw,value))
        tail(data);return rows                                           # the request's own TLV tail, not items
    def busy(self):return {ch for pins in self.plans.values() for ch in pins}
    def apply(self):
        self.idle={ch:0 for ch in range(8)}
        for (tag,ch),value in self.items.items():
            if tag==IDLE and ch in self.idle:self.idle[ch]=value[2]
        live=self.busy()
        for ch in self.pins:
            if (DISABLE,ch) in self.items:self.pins[ch]=None           # never driven or set up
            elif ch not in live:self.pins[ch]=self.idle[ch]
    def restore(self,ch):
        if (DISABLE,ch) not in self.items:super().restore(ch)
    def canonical(self):return [tlv(tag,value) for (tag,ch),value in sorted(self.items.items())]
    def change(self,new):
        if new==self.items:return                                        # the same contents: generation and pins kept
        if self.generation==0xffffffff:raise unavailable(2)
        if self.model.hold_max is not None and len(new)>self.model.hold_max:raise unavailable(2)
        disabled={ch for tag,ch in new if tag==DISABLE};idles={ch for tag,ch in new if tag==IDLE};live=self.busy()
        for ch in sorted(disabled):
            if (DISABLE,ch) not in self.items and ch in live:raise unavailable(1,ch)
        if disabled&idles:raise unavailable(5,min(disabled&idles))
        self.items=new;self.generation+=1;self.apply()
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=4:
            channels=[int.from_bytes(v[3:5],'little') for raw,v in tags] if fn==1 and op==16 else [int.from_bytes(payload[1+i*(3 if op==16 else 2):3+i*(3 if op==16 else 2)],'little') for i in range(payload[0])] if fn in (2,3) else []
            for ch in channels:
                if (DISABLE,ch) in self.items:raise unavailable(5,ch)
            return super().dispatch_request(fn,op,payload,tags)
        m=self.model
        if op==16:
            first=int.from_bytes(payload,'little');out=bytearray();count=0
            for row in self.canonical()[first:]:
                if len(out)+len(row)>m.ep.probe.max_frame-11:break
                out.extend(row);count+=1
            return struct.pack('<BIB',int(first+count<len(self.items)),self.generation,count)+bytes(out)
        if op==17:
            new=dict(self.items);seen=set();undeclared=None
            for raw,value in tags:
                if raw&127 not in DECLARED:
                    undeclared=raw if undeclared is None else undeclared;continue
                key=(raw&127,self.form(raw&127,value))
                if key in seen:raise Reject(3)
                seen.add(key);new[key]=value
            if undeclared is not None:raise Reject(11,bytes((undeclared,)))
            for raw,value in tags:
                if self.refused(raw&127,int.from_bytes(value[:2],'little'),value):raise Reject(11,bytes((raw,)))
            self.change(new);return struct.pack('<I',self.generation)
        if op==20:
            keys=[(raw&127,ch) for raw,ch in tags]
            if len(set(keys))!=len(keys):raise Reject(3)
            undeclared=next((raw for raw,ch in tags if raw&127 not in DECLARED),None)
            if undeclared is not None:raise Reject(11,bytes((undeclared,)))
            new=dict(self.items)
            for key in keys:new.pop(key,None)
            self.change(new);return struct.pack('<I',self.generation)
        if op==18:
            if sum(map(len,self.canonical()))>64:raise unavailable(3)
            m.persist_config([(tag,value) for (tag,ch),value in sorted(self.items.items())])
            m.storage_state,m.storage_reason,m.saved_generation=1,0,self.generation;return struct.pack('<I',self.generation)
        if op==19:
            m.erase_config();return b''
        return struct.pack('<BIB',m.storage_state,m.saved_generation,m.storage_reason)


class ConfigModel(GpioModel):
    channels=8
    def __init__(self,*,boot_id=17):
        self.saved=None;self.storage_state=self.storage_reason=self.saved_generation=0;self.hold_max=None
        self.catalog={1:('oep.probe.plan',0,1),2:('oep.fixture.gpio',0,1),3:('oep.fixture.gpio',1,1),4:('oep.probe.config',0,1)}
        super().__init__(lambda:0,boot_id=boot_id);self.ep.probe.max_frame=256
    def install(self):self.extension=ConfigExtension(self);self.ep.current_core=Core(self.ep,self.extension)
    def persist_config(self,items):
        # The same contents already saved are not written again (§3).
        same=self.saved is not None and self.storage_state==1
        try:same=same and sorted(self.saved)==sorted(items)
        except TypeError:same=False
        if not same:self.write_config(items)
    def write_config(self,items):self.saved=copy.deepcopy(list(items))
    def erase_config(self):
        self.saved=None;self.storage_state=self.storage_reason=self.saved_generation=0
    def handle(self,request):
        if len(request)<6 or int.from_bytes(request[3:5],'little')!=4:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free
        e.fixed,e.free=FIXED,FREE
        try:return self.ep.current_core.handle(request,0)
        finally:e.fixed,e.free=fixed,free
    def config_state(self):
        e=self.extension
        return dict(items={f'{tag}:{ch}':value.hex() for (tag,ch),value in sorted(e.items.items())},generation=e.generation,
                    storage=[self.storage_state,self.saved_generation,self.storage_reason],
                    saved=encoded(self.saved),
                    gpio=copy.deepcopy(self.gpio_state()))
