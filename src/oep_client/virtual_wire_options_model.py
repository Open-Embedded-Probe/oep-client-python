"""Logical wire attach options and transient reset-pin fixture; no electricity."""
import copy
import struct
from .virtual_attach_model import Attachments, AttachModel
from .virtual_core import Core, Completed, Reject, tlv


class WireOptions(Attachments):
    pairs={**Attachments.pairs,3:[(4,5),(2,3)]}
    def __init__(self,ep):
        super().__init__(ep);self.reset_supported=True;self.behavior='ok';self.clock_ms=0;self.wire_events=[];self.targets={};self.pin_modes={i:'free' for i in range(8)};self.pin_levels={i:dict(mode='free',level=None) for i in range(8)}
    def describe(self,fn):
        rows=tuple(row for row in super().describe(fn) if row[0]!=1)        # its own reset line (channel 6) only
        return rows+(tlv(1,b'\x03\0\0\x40'),) if fn<=3 and self.reset_supported else rows
    def request_tags(self,fn,op,data):
        if fn>3 or op!=17:return super().request_tags(fn,op,data)
        known={5:4}
        if fn==1:known[4]=1
        if fn==3:known[2]=4
        result={}
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);value=data[3:3+size];tag=raw&127
            if not tag or len(value)!=size:raise Reject(3)
            data=data[3+size:]
            if tag not in known:
                if raw&128:raise Reject(11,bytes((raw,)))
                continue
            if size!=known[tag]:raise Reject(3)
            if tag in result:                                  # a later copy is checked too (core §2.3); the first is used
                if tag==4 and value[0] not in (0,1):raise Reject(11,bytes((raw,)))
                if tag==5:
                    channel,hold=struct.unpack('<HH',value)
                    if not self.reset_supported or channel!=6 or hold>self.ep.max_op_ms:raise Reject(11,bytes((raw,)))
                continue
            result[tag]=(raw,value)
        return result
    def close_connection(self,rid):
        pins=self.connections[rid]['pins'];super().close_connection(rid)
        for pin in pins:
            if pin!=65535:self.pin_levels[pin]=dict(mode='free',level=None)
        self.wire_events.append(('wire-close',rid))
    def connection_page(self,fn,first):
        rows=[];selected=[]
        for rid,s in self.connections.items():
            if s['fn']!=fn:continue
            tid=struct.pack('<I',0x2ba01477) if fn==3 else b''            # swd: the DPIDR read at attach (scheme 2)
            if s['targetsel'] is not None:selected.append((len(rows),s['targetsel']))
            rows.append(struct.pack('<IHHIBB',rid,*s['pins'],s['speed'],2 if fn==3 else 0,len(tid))+tid)
        page=bytearray(b'\0\0');index=first;tail=b''
        while index<len(rows):
            extra=b''.join(tlv(1,struct.pack('<BI',index-first,t)) for i,t in selected if i==index)
            if len(page)+len(tail)+len(rows[index])+len(extra)+9>self.ep.probe.max_frame:break
            page.extend(rows[index]);tail+=extra;page[1]+=1;index+=1
        page[0]=int(index<len(rows));return bytes(page)+tail+tlv(127,b'x')
    def dispatch_request(self,fn,op,payload,tags):
        if fn>3 or op!=17:return super().dispatch_request(fn,op,payload,tags)
        method,swdio,swclk,speed=struct.unpack_from('<BHHI',payload);pair=(swdio,swclk);idle=tags[4][1][0] if 4 in tags else 0
        if not speed:raise Reject(3)
        if method not in ((0,) if fn==3 else (0,1)):raise Reject(11,b'\0')
        if speed<1000 or pair not in self.pairs[fn]:raise Reject(11,b'\0')
        if idle not in (0,1):raise Reject(11,bytes((tags[4][0],)))
        active={pin for s in self.connections.values() for pin in s['pins'] if pin!=65535}
        if active.intersection(pair):raise Reject(4,tlv(1,b'\x01'))
        for pin in pair:
            if pin==65535:continue
            if self.pin_modes[pin]=='reserved':raise Reject(4,tlv(1,b'\x01'))
            if self.pin_modes[pin]!='free':raise Reject(4,tlv(1,b'\x05'))
        reset=struct.unpack('<HH',tags[5][1]) if 5 in tags else None
        if reset:
            channel,hold=reset
            if not self.reset_supported or channel!=6 or hold>self.ep.max_op_ms:raise Reject(11,bytes((tags[5][0],)))
            if channel in active or channel in pair or self.pin_modes[channel]=='reserved':raise Reject(4,tlv(1,b'\x01'))
            if self.pin_modes[channel]!='free':raise Reject(4,tlv(1,b'\x05'))
        if sum(s['fn']==fn for s in self.connections.values())>=2 or len(self.connections)+len(self.streams)>=8:raise Reject(4,tlv(1,b'\x02'))
        targetsel=struct.unpack('<I',tags[2][1])[0] if 2 in tags else None;key=(fn,*pair,targetsel);start=self.clock_ms
        self.touch.append((fn,pair));flags=8 if method else 0
        if reset:
            channel,hold=reset;self.targets.setdefault(key,dict(halted=False,reset_count=0))['reset_count']+=1
            self.pin_levels[channel]=dict(mode='open-drain',level=0);self.wire_events.append(('reset-assert',channel,'open-drain-low',self.clock_ms));self.clock_ms+=hold
            self.pin_levels[channel]=dict(mode='free',level=None);self.wire_events.append(('reset-release',channel,'free',self.clock_ms))
            failures=5 if self.behavior=='dm-line' else 2 if self.behavior=='dm-recover' else 0;attempt=0;responded=False
            while self.clock_ms-start<self.ep.max_op_ms:
                line=attempt<failures;self.wire_events.append(('reset-read',fn,2 if line else 0,self.clock_ms,1000));attempt+=1
                if not line:responded=True;break
                self.clock_ms+=1
            if not responded:return Completed(b'\x02'+tlv(127,b'x'),1)
            if fn!=3:flags|=1;self.wire_events.append(('ack-havereset',fn))
        if fn==3 and self.behavior=='dormant':flags|=4
        rid=self.allocate();chosen=1000 if fn==2 else min(speed,100000)
        self.connections[rid]=dict(fn=fn,pins=pair,speed=chosen,idle_clock=idle,targetsel=targetsel)
        self.targets.setdefault(key,dict(halted=False,reset_count=0))['halted']=bool(method)
        levels=(1,1-idle) if fn==1 else (1,) if fn==2 else (0,1)
        for pin,level in zip((pin for pin in pair if pin!=65535),levels):self.pin_levels[pin]=dict(mode='wire',level=level)
        identity=0x2ba01477 if fn==3 else 0x302 if method else 0xc02
        return struct.pack('<IIBI',rid,identity,flags,chosen)+(tlv(17,struct.pack('<I',0x1000)) if method else b'')+tlv(127,b'x')


class WireOptionsModel(AttachModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.ep.max_op_ms=5;self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=77)+(tlv(77,struct.pack('<I',5)),)
    def install(self):self.extension=WireOptions(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def wire_options_state(self):
        state=self.attach_state();e=self.extension;state.update(reset_supported=e.reset_supported,wire_behavior=e.behavior,clock_ms=e.clock_ms,wire_events=copy.deepcopy(e.wire_events),targets=copy.deepcopy(e.targets),pin_modes=dict(e.pin_modes),pin_levels=copy.deepcopy(e.pin_levels),max_op_ms=self.ep.max_op_ms);return state
    def wire_behavior(self,value):
        if value not in ('ok','dm-recover','dm-line','dormant'):raise ValueError('explicit logical wire behavior required')
        self.extension.behavior=value
    def wire_reset_support(self,value):
        if type(value)is not bool:raise ValueError('reset support must be bool')
        self.extension.reset_supported=value
    def wire_pin(self,channel,mode):
        e=self.extension
        if type(channel)is not int or not 0<=channel<8 or mode not in ('free','disabled','output','reserved') or any(channel in s['pins'] for s in e.connections.values()):raise ValueError('unowned channel and explicit pin mode required')
        e.pin_modes[channel]=mode;e.pin_levels[channel]=dict(mode=mode,level=1 if mode=='output' else None)
