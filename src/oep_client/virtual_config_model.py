"""Current config subset and RAM-backed persistence, composed with live GPIO/plan."""
import copy
import struct
import zlib
from .virtual_core import Core,Reject,tail,tlv
from .virtual_gpio_model import Gpio,GpioModel


class ConfigExtension(Gpio):
    functions=(1,2,3,4)
    def __init__(self,model):
        super().__init__();self.model=model;self.items={}
        self.functions=tuple(model.catalog)
        self.idle={ch:(0,0) for ch in range(8)};self.pins=dict(self.idle)
        model.storage_reason=0
        if model.saved is not None:self.restore_saved()
    def restore_saved(self):
        # The RAM blob is a fixture format, not a prescribed storage serialization.
        m=self.model
        try:
            if not isinstance(m.saved,dict) or not isinstance(m.saved_identities,dict):raise ValueError()
            referenced=set()
            for (tag,key),value in m.saved.items():
                if type(tag) is not int or tag not in (1,2,3,7) or type(key) is not bytes or type(value) is not bytes:raise ValueError()
                size=5 if tag==1 else 4 if tag==3 else 2 if tag==7 else None
                if size is not None and len(value)!=size:raise ValueError()
                if tag==2:
                    if not 3<=len(value)<=34:raise ValueError()
                    value[2:].decode('utf-8')
                if key!=(value if tag==1 else value[:2]):raise ValueError()
                if tag==1:referenced.add(int.from_bytes(value[:2],'little'))
            if set(m.saved_identities)!=referenced:raise ValueError()
            for fn,identity in m.saved_identities.items():
                if type(fn) is not int or not 1<=fn<=65535 or type(identity) is not tuple or len(identity)!=3:raise ValueError()
                name,instance,revision=identity
                if type(name) is not str or not name or type(instance) is not int or not 0<=instance<=65535 or type(revision) is not int or not 1<=revision<=255:raise ValueError()
        except (ValueError,TypeError):
            m.storage_reason=1;m.saved_hash=0;return
        remap={}
        for old,identity in m.saved_identities.items():
            matches=[fn for fn,row in m.catalog.items() if row==identity]
            if len(matches)!=1:m.storage_reason=2;m.saved_hash=0;return
            remap[old]=matches[0]
        candidate={}
        for (tag,key),value in m.saved.items():
            if tag==1:value=struct.pack('<H',remap[int.from_bytes(value[:2],'little')])+value[2:];key=value
            candidate[tag,key]=value
        try:self.validate(candidate)
        except Reject:
            m.storage_reason=3;m.saved_hash=0;return
        self.items=candidate;self.apply();m.saved_hash=self.hash()
    def list_page(self,first,limit):
        rows=[]
        for fn,(name,instance,revision) in sorted(self.model.catalog.items()):
            n=name.encode('ascii');rows.append(struct.pack('<HHBBB',fn,instance,revision,0,len(n))+n)
        out=bytearray(struct.pack('<HB',len(rows),0))
        for row in rows[first:]:
            if len(out)+len(row)>limit:break
            out.extend(row);out[2]+=1
        return bytes(out)
    def describe(self,fn):
        if fn==4:return (tlv(7,b'\x10\x3f'),tlv(64,struct.pack('<I',64)),tlv(65,b'\1\2\3\7'),tlv(66,b'\0'))
        return super().describe(fn)
    def request_tags(self,fn,op,data):
        if fn!=4:return super().request_tags(fn,op,data)
        if op!=17:return tail(data)
        rows=[]
        while data:
            if len(data)<3:raise Reject(3)
            raw,n=struct.unpack_from('<BH',data);value,data=data[3:3+n],data[3+n:]
            if raw&127==0 or len(value)!=n:raise Reject(3)
            if raw&127 not in (1,2,3,7):raise Reject(11,bytes((raw,)))
            rows.append((raw,value))
        return rows
    def key(self,tag,value):
        if tag==1:
            if len(value)!=5:raise Reject(3)
            fn,role,ch=struct.unpack('<HBH',value)
            if fn not in (2,3) or self.model.catalog.get(fn,('',0,0))[0]!='oep.fixture.gpio' or role!=1 or ch>=8:raise Reject(11,bytes((tag,)))
            return value
        if len(value)<2:raise Reject(3)
        ch=int.from_bytes(value[:2],'little')
        if ch>=8:raise Reject(11,bytes((tag,)))
        if tag==2:
            if not 1<=len(value[2:])<=32:raise Reject(3)
            try:value[2:].decode('utf-8')
            except UnicodeDecodeError:raise Reject(3) from None
        if tag==3:
            if len(value)!=4:raise Reject(3)
            if value[2]>4 or value[3] not in (0,1,255) and value[2] in (3,4):raise Reject(11,bytes((tag,)))
        if tag==7 and len(value)!=2:raise Reject(3)
        return value[:2]
    def validate(self,items):
        for (tag,key),value in items.items():
            if self.key(tag,value)!=key:raise Reject(3)
        disabled={key for tag,key in items if tag==7}
        if any((3,ch) in items for ch in disabled):raise Reject(3)
        live={ch for pins in self.plans.values() for ch in pins}
        if any(int.from_bytes(ch,'little') in live for ch in disabled):raise Reject(4,tlv(1,b'\1'))
    def apply(self):
        self.idle={ch:(0,0) for ch in range(8)}
        for (tag,key),value in self.items.items():
            if tag==3:self.idle[int.from_bytes(key,'little')]=(value[2],0 if value[3]==255 else value[3])
        live={ch for pins in self.plans.values() for ch in pins}
        for ch in self.pins:
            if ch not in live and (7,ch.to_bytes(2,'little')) not in self.items:self.pins[ch]=self.idle[ch]
    def restore(self,ch):
        if (7,ch.to_bytes(2,'little')) not in self.items:super().restore(ch)
    def canonical(self):
        def order(row):
            (tag,key),value=row
            return (tag,*struct.unpack('<HBH',value)) if tag==1 else (tag,int.from_bytes(key,'little'))
        return [tlv(tag,value) for (tag,key),value in sorted(self.items.items(),key=order)]
    def hash(self):return zlib.crc32(b''.join(self.canonical()))
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=4:
            channels=[int.from_bytes(v[3:5],'little') for raw,v in tags] if fn==1 and op==16 else [int.from_bytes(payload[1+i*(3 if op==16 else 2):3+i*(3 if op==16 else 2)],'little') for i in range(payload[0])] if fn in (2,3) else []
            for ch in channels:
                if (7,ch.to_bytes(2,'little')) in self.items:raise Reject(4,tlv(1,b'\5')+tlv(2,struct.pack('<H',ch)))
            return super().dispatch_request(fn,op,payload,tags)
        if op==16:
            first=int.from_bytes(payload,'little');out=bytearray(b'\0'+struct.pack('<I',self.hash()));rows=self.canonical();index=first
            for row in rows[first:]:
                if len(out)+len(row)>self.model.ep.probe.max_frame-5:break
                out.extend(row);index+=1
            out[0]=int(index<len(rows));return bytes(out)
        if op==17:
            new=dict(self.items);seen=set();replaced=set()
            for raw,value in tags:
                tag=raw&127
                try:key=self.key(tag,value)
                except Reject as e:
                    if e.reason==11:raise Reject(11,bytes((raw,))) from None
                    raise
                item=(tag,key)
                if item in seen:raise Reject(3)
                seen.add(item)
                if tag==1:replaced.add(value[:2])
            new={k:v for k,v in new.items() if not(k[0]==1 and v[:2] in replaced)}
            for raw,value in tags:new[(raw&127,self.key(raw&127,value))]=value
            self.validate(new);self.items=new;self.apply();return struct.pack('<I',self.hash())
        if op==18:
            if sum(map(len,self.canonical()))>64:raise Reject(4,tlv(1,b'\3'))
            identities={int.from_bytes(v[:2],'little'):self.model.catalog[int.from_bytes(v[:2],'little')] for (tag,key),v in self.items.items() if tag==1}
            self.model.persist_config(self.items,identities)
            self.model.storage_reason=0;self.model.saved_hash=self.hash();return struct.pack('<I',self.hash())
        if op==19:
            self.model.erase_config();return b''
        if op==20:
            n=payload[0];at=1;new=dict(self.items);seen=set()
            for _ in range(n):
                size,tag=payload[at:at+2];key=payload[at+2:at+2+size];at+=2+size
                if tag not in (1,2,3,7):raise Reject(11,bytes((tag,)))
                if size!=2 or (tag,key) in seen:raise Reject(3)
                seen.add((tag,key))
                if tag==1:new={k:v for k,v in new.items() if not(k[0]==1 and v[:2]==key)}
                else:new.pop((tag,key),None)
            self.validate(new);self.items=new;self.apply();return struct.pack('<I',self.hash())
        return struct.pack('<BIB',2 if self.model.storage_reason else int(self.model.saved is not None),self.model.saved_hash,self.model.storage_reason)


class ConfigModel(GpioModel):
    def __init__(self,*,boot_id=17):
        self.saved=None;self.saved_hash=0;self.saved_identities={};self.storage_reason=0
        self.catalog={1:('oep.probe.plan',0,1),2:('oep.fixture.gpio',0,1),3:('oep.fixture.gpio',1,1),4:('oep.probe.config',0,1)}
        super().__init__(lambda:0,boot_id=boot_id);self.ep.probe.max_frame=256
    def install(self):self.extension=ConfigExtension(self);self.ep.current_core=Core(self.ep,self.extension)
    def persist_config(self,items,identities):
        def content(rows,names):
            result={}
            for (tag,key),value in rows.items():
                if tag==1:
                    fn,role,ch=struct.unpack('<HBH',value);result[tag,names[fn],role,ch]=b''
                else:result[tag,key]=value
            return result
        same=False
        if self.saved is not None and not self.storage_reason:
            try:same=content(self.saved,self.saved_identities)==content(items,identities)
            except (KeyError,TypeError,ValueError,struct.error):pass
        if not same:self.write_config(items,identities)
    def write_config(self,items,identities):
        self.saved=copy.deepcopy(items);self.saved_identities=copy.deepcopy(identities)
    def erase_config(self):
        self.saved=None;self.saved_identities={};self.storage_reason=0;self.saved_hash=0
    def handle(self,request):
        if len(request)<6 or int.from_bytes(request[3:5],'little')!=4:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free;size=1
        if request[5]==20 and len(request)>10:
            for _ in range(request[10]):
                if 10+size>=len(request):size+=1;break
                size+=2+request[10+size]
        e.fixed={16:2,17:0,18:0,19:0,20:size,21:0};e.free={16,21}
        try:return self.ep.current_core.handle(request,0)
        finally:e.fixed,e.free=fixed,free
    def config_state(self):
        return dict(items={f'{tag}:{key.hex()}':value.hex() for (tag,key),value in self.extension.items.items()},hash=self.extension.hash(),saved_hash=self.saved_hash,storage_reason=self.storage_reason,
                    saved=None if self.saved is None else {f'{tag}:{key.hex()}':value.hex() for (tag,key),value in self.saved.items()},gpio=copy.deepcopy(self.gpio_state()))
