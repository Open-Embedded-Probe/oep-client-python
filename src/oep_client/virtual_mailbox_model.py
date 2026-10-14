"""Logical SDI/DMDATA mailbox fixture; no target or DMI transport is opened."""
import copy
import struct
from .virtual_attach_model import Attachments, AttachModel
from .virtual_core import Core


class Mailboxes(Attachments):
    def __init__(self,ep):super().__init__(ep);self.mailboxes={}
    def close_stream(self,rid):
        super().close_stream(rid);self.mailboxes.pop(rid,None)
    def dispatch_request(self,fn,op,payload,tags):
        value=super().dispatch_request(fn,op,payload,tags)
        if fn==4 and op==16:
            rid=struct.unpack_from('<I',value)[0]
            self.mailboxes[rid]={'data0':0,'data1':0,'halted':False,'debug_busy':False,'events':[]}
        return value
    def poll(self,rid):
        self.resource(rid,'stream');m=self.mailboxes[rid];s=self.streams[rid][1]
        # Each adapter snapshot records this poll; avoid quadratic history copies.
        m['events']=[]
        if m['debug_busy']:return
        status=0x100 if m['halted'] else 0;m['events'].append(('read',0x11,status))
        if status:return
        d0,d1=m['data0'],m['data1'];m['events'].append(('read',4,d0));length=d0&255
        if self.meta[rid]==0:
            if not length:return
            if length>7:m['events'].append(('write',4,0));m['data0']=0;return   # not a frame: cleared (console §3.1)
            m['events'].append(('read',5,d1));s.feed((d0.to_bytes(4,'little')[1:]+d1.to_bytes(4,'little'))[:length]);answer=0
        else:
            if not length&128:return
            size=(length&63)-4
            if not 0<=size<=7:answer=0
            else:
                if size>=4:m['events'].append(('read',5,d1))
                s.feed((d0.to_bytes(4,'little')[1:]+d1.to_bytes(4,'little'))[:size])
                queued=bytes(s.tx[:3]);answer=(len(queued)+4)|(int.from_bytes(queued,'little')<<8) if queued else 0
                del s.tx[:len(queued)]
        m['events'].append(('write',4,answer));m['data0']=answer


class MailboxModel(AttachModel):
    def install(self):self.extension=Mailboxes(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def mailbox_state(self):
        state=self.attach_state();state['mailboxes']=copy.deepcopy(self.extension.mailboxes);return state
    def mailbox_frame(self,rid,data0,data1):
        m=self.extension.mailboxes.get(rid)
        if m is None or any(type(v)is not int or not 0<=v<=0xffffffff for v in (data0,data1)):raise ValueError('live mailbox and u32 words required')
        m.update(data0=data0,data1=data1)
    def mailbox_poll(self,rid):self.extension.poll(rid)
    def mailbox_halted(self,rid,value):self.mailbox_switch(rid,'halted',value)
    def mailbox_debug_busy(self,rid,value):self.mailbox_switch(rid,'debug_busy',value)
    def mailbox_switch(self,rid,key,value):
        if rid not in self.extension.mailboxes or type(value)is not bool:raise ValueError('live mailbox and bool switch required')
        self.extension.mailboxes[rid][key]=value
