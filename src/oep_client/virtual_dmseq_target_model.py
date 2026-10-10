"""Target-side dmseq simulator with explicit logical time; no device transport."""
import copy
from .virtual_dmseq_model import DmseqModel, checksum


class Target:
    short_ms=20
    long_ms=1000
    input_capacity=2
    output_capacity=6
    def __init__(self,s,a,data):
        self.s=s;self.a=a;self.syn=True;self.to=False;self.payload=data
        self.now=0;self.deadline=self.short_ms;self.last_emit=0;self.data0=0;self.data1=0xdeadbeef
        self.input=bytearray();self.output=bytearray();self.events=[];self.publish()
    def publish(self):
        head=bytes((128|(self.s<<5)|(self.a<<4)|(int(self.syn)<<3)|(int(self.to)<<6)|len(self.payload),))+self.payload
        raw=(head+bytes((checksum(head),))).ljust(8,b'\0')
        if len(self.payload)>=3:self.data1=int.from_bytes(raw[4:],'little');self.events.append(('write',5,self.data1))
        self.word=int.from_bytes(raw[:4],'little');self.data0=self.word;self.events.append(('write',4,self.word));self.last_emit=self.now
    def service(self,advance):
        self.now+=advance;self.events=[('read',4,self.data0)];word=self.data0
        if word and not word&128:
            n=word&7;raw=word.to_bytes(4,'little')
            if n<=2 and checksum(raw[:1+n])==raw[1+n] and (word>>5)&1==self.s:
                h=(word>>4)&1
                if n and h!=self.a and len(self.input)+n<=self.input_capacity:self.input.extend(raw[1:1+n]);self.a=h
                self.s^=1;self.syn=False;self.to=False
                self.payload=bytes(self.output[:6]);del self.output[:len(self.payload)]
                self.deadline=self.now+self.long_ms;self.publish();return
        if not self.to and self.now>=self.deadline:self.to=True;self.publish()
        elif word and word!=self.word:self.publish()
        elif (not word or self.to) and self.now-self.last_emit>=self.short_ms:self.publish()
    def write_output(self,data):
        if self.to:return 0
        count=min(len(data),self.output_capacity-len(self.output));self.output.extend(data[:count]);return count
    def read_input(self,count):
        value=bytes(self.input[:count]);del self.input[:len(value)];return value
    def state(self):
        return {key:bytes(value) if isinstance(value,bytearray) else copy.deepcopy(value) for key,value in vars(self).items()}


class TargetDmseqModel(DmseqModel):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.target=None
    def target_begin(self,s,a,data):
        if type(s)is not int or s not in (0,1) or type(a)is not int or a not in (0,1) or not isinstance(data,bytes) or len(data)>6:raise ValueError('explicit S/A bits and at most six bytes required')
        self.target=Target(s,a,data)
    def target_state(self):
        if self.target is None:raise ValueError('target fixture not begun')
        return self.target.state()
    def target_host_word(self,word):
        if self.target is None or type(word)is not int or not 0<=word<=0xffffffff:raise ValueError('begun fixture and u32 host word required')
        self.target.data0=word
    def target_service(self,advance):
        if self.target is None or type(advance)is not int or advance<0:raise ValueError('begun fixture and nonnegative logical advance required')
        self.target.service(advance)
    def target_write_output(self,data):
        if self.target is None or not isinstance(data,bytes):raise ValueError('begun fixture and bytes output required')
        return self.target.write_output(data)
    def target_read_input(self,count):
        if self.target is None or type(count)is not int or count<0:raise ValueError('begun fixture and nonnegative input count required')
        return self.target.read_input(count)
    def target_link_poll(self,rid):
        if self.target is None or rid not in self.extension.protocols:raise ValueError('begun fixture and live dmseq stream required')
        self.mailbox_frame(rid,self.target.data0,self.target.data1);self.mailbox_poll(rid)
        self.target.data0=self.extension.mailboxes[rid]['data0'];self.target.service(0)
        return {'target':self.target.state(),'probe':self.dmseq_state()}
