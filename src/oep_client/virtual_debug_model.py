"""Logical mandatory riscv-dm fixture with per-connection mailbox exclusion."""
import copy
import struct
from .virtual_dmseq_model import Dmseq, DmseqModel
from .virtual_core import Core, Completed, Reject, tlv


HARTSEL=(0x3ff<<6)|(0x3ff<<16)

SIZES={1:6,2:2,3:12,4:5,5:14}

def fixed_dmi(payload):
    if len(payload)<6:return 6
    count=struct.unpack_from('<H',payload,4)[0];cursor=6
    for _ in range(count):
        if cursor>=len(payload):return cursor+1
        size=SIZES.get(payload[cursor])
        if size is None:return len(payload)
        cursor+=size
    return cursor


class Debug(Dmseq):
    functions=(1,2,3,4,5)
    names=Dmseq.names+(b'oep.target.riscv-dm',)
    def __init__(self,ep):super().__init__(ep);self.targets={};self.access=[];self.arbitration=[];self.failure_at=None;self.failure_status=2;self.hart_failure=False
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in zip(self.functions,self.names)]
        page=bytearray(struct.pack('<HB',len(rows),0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):return (tlv(7,b'\x10\x07'),) if fn==5 else super().describe(fn)
    def close_connection(self,rid):super().close_connection(rid);self.targets.pop(rid,None)
    def readreg(self,parent,address):
        if address==0x11:return 0x300 if self.targets[parent]['halted'] else 0xc00
        return self.targets[parent]['registers'].get(address,0)
    def writereg(self,parent,address,value):
        self.targets[parent]['registers'][address]=value
        for rid,(connection,_) in self.streams.items():
            if connection==parent and address in (4,5):self.mailboxes[rid]['data0' if address==4 else 'data1']=value
    def poll(self,rid):
        parent=self.streams[rid][0];m=self.mailboxes[rid];m['halted']=self.targets[parent]['halted'];super().poll(rid)
        if m['events'] and m['events'][0][:2]==('read',0x11):m['events'][0]=('read',0x11,self.readreg(parent,0x11))
        self.targets[parent]['registers'].update({4:m['data0'],5:m['data1']})
    def interleave(self,parent):
        for rid,(connection,_) in self.streams.items():
            if connection==parent:self.poll(rid);self.arbitration.append({'connection':connection,'stream':rid,'events':list(self.mailboxes[rid]['events'])})
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=5:
            value=super().dispatch_request(fn,op,payload,tags)
            if fn<=3 and op==17 and len(value)>=13 and getattr(value,'outcome',0)==0:
                parent=struct.unpack_from('<I',value)[0];self.targets[parent]={'registers':{},'halted':False,'haltreq':False}
            return value
        parent=struct.unpack_from('<I',payload)[0];self.resource(parent,'connection')
        if self.connections[parent]['fn']==3:raise Reject(4,tlv(1,b'\x06'))
        target=self.targets[parent]
        if op in (17,18):
            control=target['registers'].get(0x10,0)&~HARTSEL
            self.writereg(parent,0x10,control&~0x80000000)
            if op==17:
                # halt (debug §4 table, §4.2): allhalted seen, then haltreq lowered; already halted: lowered, ok
                target['haltreq']=False
                if not target['halted']:
                    self.access.append(('halt',parent))
                    if self.hart_failure:
                        target['haltreq']=False;self.writereg(parent,0x10,control&~0x80000000);return Completed(b'\x04'+tlv(127,b'x'),1)
                    target.update(halted=True,haltreq=False);self.writereg(parent,0x10,control&~0x80000000)
            else:
                target['haltreq']=False;self.access.append(('resume',parent))
                if self.hart_failure:return Completed(b'\x05'+tlv(127,b'x'),1)
                target['halted']=False
            return b'\0'+tlv(127,b'x')
        count=struct.unpack_from('<H',payload,4)[0];cursor=6;steps=[];time_sum=0
        for _ in range(count):
            if cursor>=len(payload):raise Reject(3)
            if payload[cursor] not in SIZES:raise Reject(11,b'\0')            # an unknown kind: unsupported (debug §4.1)
            size=SIZES[payload[cursor]];row=payload[cursor:cursor+size]
            if len(row)!=size:raise Reject(3)
            if row[0] in (4,5):time_sum+=struct.unpack_from('<I',row,size-4)[0]
            steps.append(row);cursor+=size
        if cursor!=len(payload):raise Reject(3)
        if time_sum>self.ep.max_op_ms*1000:raise Reject(11,b'\0')
        max_values=sum(row[0] in (2,3,5) for row in steps)
        if 10+4*max_values>self.ep.probe.max_frame:raise Reject(11,b'\0')
        related=[rid for rid,(connection,_) in self.streams.items() if connection==parent]
        for rid in related:self.mailboxes[rid]['debug_busy']=True
        values=[];done=0;status=0;elapsed=0
        try:
            for index,row in enumerate(steps):
                self.interleave(parent)
                if elapsed>=self.ep.max_op_ms*1000:status=4;break
                if index==self.failure_at:
                    status=self.failure_status
                    if status==4 and row[0] in (3,5):
                        value=self.readreg(parent,row[1]);values.append(value);self.access.append(('dmi-read',parent,row[1],value))
                    break
                kind=row[0]
                if kind==1:
                    value=struct.unpack_from('<I',row,2)[0];self.writereg(parent,row[1],value);self.access.append(('dmi-write',parent,row[1],value));elapsed+=10
                elif kind==2:
                    value=self.readreg(parent,row[1]);values.append(value);self.access.append(('dmi-read',parent,row[1],value));elapsed+=10
                elif kind==4:elapsed+=struct.unpack_from('<I',row,1)[0]
                else:
                    mask,wanted=struct.unpack_from('<II',row,2);limit=struct.unpack_from('<H' if kind==3 else '<I',row,10)[0]
                    attempts=max(1,limit) if kind==3 else max(1,(limit+9)//10)
                    matched=False
                    for _ in range(attempts):
                        value=self.readreg(parent,row[1]);self.access.append(('dmi-read',parent,row[1],value));elapsed+=10
                        if value&mask==wanted:matched=True;break
                        if elapsed>=self.ep.max_op_ms*1000:break
                    values.append(value)
                    if not matched:status=4;break
                done+=1
            body=struct.pack('<HBH',done,status,len(values))+b''.join(struct.pack('<I',v) for v in values)
            extra=tlv(127,b'x')
            if 5+len(body)+len(extra)<=self.ep.probe.max_frame:body+=extra
            return Completed(body,0 if not status else (2 if done else 1))
        finally:
            for rid in related:self.mailboxes[rid]['debug_busy']=False


class DebugModel(DmseqModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.ep.max_op_ms=1
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=77)+(tlv(77,struct.pack('<I',1)),)
    def install(self):self.extension=Debug(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        if len(request)<6 or struct.unpack_from('<H',request,3)[0]!=5:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free;e.fixed={16:fixed_dmi(request[10:]),17:4,18:4};e.free=set()
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def debug_state(self):
        state=self.dmseq_state();e=self.extension;state.update(targets=copy.deepcopy(e.targets),access=list(e.access),arbitration=copy.deepcopy(e.arbitration),failure_at=e.failure_at,failure_status=e.failure_status,hart_failure=e.hart_failure,max_op_ms=self.ep.max_op_ms,read_cost_us=10)
        return state
    def debug_failure(self,index,status):
        if index is not None and (type(index)is not int or not 0<=index<=65535):raise ValueError('failure index must be u16 or None')
        if type(status)is not int or status not in (1,2,3,4,5):raise ValueError('standard failure status required')
        self.extension.failure_at=index;self.extension.failure_status=status
    def debug_registers(self,parent,values):
        if parent not in self.extension.targets or not isinstance(values,dict) or any(type(k)is not int or not 0<=k<=255 or type(v)is not int or not 0<=v<=0xffffffff for k,v in values.items()):raise ValueError('live parent and explicit u8/u32 register values required')
        for address,value in values.items():self.extension.writereg(parent,address,value)
    def mailbox_frame(self,rid,data0,data1):
        super().mailbox_frame(rid,data0,data1);parent=self.extension.streams[rid][0];self.extension.targets[parent]['registers'].update({4:data0,5:data1})
    def debug_console_poll(self,rid):self.extension.poll(rid)
    def debug_hart_failure(self,value):
        if type(value)is not bool:raise ValueError('logical hart failure switch must be bool')
        self.extension.hart_failure=value
