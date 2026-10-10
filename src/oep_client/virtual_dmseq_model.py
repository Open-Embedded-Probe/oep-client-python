"""Independent logical probe-side dmseq fixture; no target transport."""
import copy
import struct
from .virtual_mailbox_model import Mailboxes, MailboxModel
from .virtual_core import Core


def checksum(data):
    crc=0xff
    for byte in data:
        crc^=byte
        for _ in range(8):crc=((crc<<1)^7 if crc&128 else crc<<1)&255
    return crc


def initial():return {'synced':False,'last_s':0,'last_syn':False,'h':0,'pending':b''}


class Dmseq(Mailboxes):
    mechanisms=(0,1,2)
    def __init__(self,ep):super().__init__(ep);self.protocols={}
    def dispatch_request(self,fn,op,payload,tags):
        value=super().dispatch_request(fn,op,payload,tags)
        if fn==4 and op==16 and payload[2]==2:self.protocols[struct.unpack_from('<H',value)[0]]=initial()
        return value
    def close_stream(self,rid):super().close_stream(rid);self.protocols.pop(rid,None)
    def poll(self,rid):
        self.resource(rid,'stream')
        if self.meta[rid]!=2:return super().poll(rid)
        m=self.mailboxes[rid];p=self.protocols[rid];stream=self.streams[rid][1];m['events']=[]
        if m['debug_busy']:return
        status=0x100 if m['halted'] else 0;m['events'].append(('read',0x11,status))
        if status:return
        d0=m['data0'];m['events'].append(('read',4,d0))
        if not d0&128:return
        n=d0&7
        if n>=3:m['events'].append(('read',5,m['data1']))
        if n>6:return
        raw=struct.pack('<II',d0,m['data1'])
        if checksum(raw[:1+n])!=raw[1+n]:
            stream.mark(5,2);return
        s=(d0>>5)&1;a=(d0>>4)&1;syn=bool(d0&8)
        if d0&64:stream.mark(5,4)
        duplicate=p['synced'] and s==p['last_s'] and (not syn or p['last_syn'])
        if not duplicate:
            if p['synced'] and syn:stream.mark(2,2)
            if not p['synced'] or syn:p.update(synced=True,last_s=1-s,h=1-a,pending=b'')
            if s!=p['last_s']:stream.feed(raw[1:1+n]);p.update(last_s=s,last_syn=syn)
        if p['pending'] and a==p['h']:p.update(h=1-p['h'],pending=b'')
        if not p['pending']:p['pending']=bytes(stream.tx[:2]);del stream.tx[:len(p['pending'])]
        prefix=bytes(((s<<5)|(p['h']<<4)|len(p['pending']),))+p['pending']
        answer=int.from_bytes(prefix+bytes((checksum(prefix),)),'little')
        m['data0']=answer;m['events'].append(('write',4,answer))


class DmseqModel(MailboxModel):
    def install(self):self.extension=Dmseq(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def dmseq_state(self):
        state=self.mailbox_state();state['protocols']=copy.deepcopy(self.extension.protocols)
        for rid,(_,s) in self.extension.streams.items():state['streams'][rid]['marks']=list(s.marks)
        return state
    def reset_target(self,parent):
        super().reset_target(parent)
        for rid,(connection,_) in self.extension.streams.items():
            if connection==parent and rid in self.extension.protocols:self.extension.protocols[rid]=initial()
