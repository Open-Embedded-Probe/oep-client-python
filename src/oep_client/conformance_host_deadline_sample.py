"""Real link wait loops on a scheduled stream and scoped virtual clock."""
import hashlib
from pathlib import Path
import struct
from unittest.mock import patch
from . import cobs,frames,link,message


class Clock:
    def __init__(self):self.ns=0
    def monotonic(self):return self.ns/1e9
    def sleep(self,seconds):self.ns+=max(1,round(seconds*1e9))
    def time(self):return self.monotonic()


class ScheduledStream:
    keeps_boundaries=True
    def __init__(self,clock,kind):
        self.clock=clock;self.kind=kind;self.baudrate=115200 if kind=='cobs' else None;self.timeout=.01
        self.rx=bytearray();self.schedule=[];self.events=[];self.writes=[];self.on_write=None;self.reads=0
    def deliver(self):
        while self.schedule and self.schedule[0][0]<=self.clock.ns:
            at,data=self.schedule.pop(0);self.rx+=data;self.events.append(dict(action='arrival',time_ns=at,length=len(data)))
    @property
    def in_waiting(self):self.deliver();return len(self.rx)
    def reset_input_buffer(self):self.rx.clear()
    def read(self,n=1):
        self.reads+=1
        if self.reads>100000:raise ValueError('virtual read budget exceeded')
        self.deliver()
        if not self.rx:
            step=max(1,round(self.timeout*1e9));end=self.clock.ns+step
            if self.schedule:end=min(end,self.schedule[0][0])
            self.clock.ns=end;self.deliver()
        data=bytes(self.rx[:n]);del self.rx[:n];return data
    def write(self,data):
        self.writes.append(bytes(data));self.on_write(bytes(data));return len(data)


class SampleHostDeadlineAdapter:
    def run_case(self,case):
        clock=Clock();stream=ScheduledStream(clock,case['kind']);events=stream.events
        with patch.object(link,'time',clock),patch.object(frames,'time',clock):
            d=link.SerialLink.on_stream(stream,case['kind'],.05);d.expected_s=lambda:case['argument_ms']/1000
            d.resend=False;d._recover=lambda:events.append(dict(action='recovery-control',time_ns=clock.ns))
            received=[];receive=d._recv_for
            def traced(corr):
                start=clock.ns
                try:r=receive(corr)
                except TimeoutError:events.append(dict(action='timeout',corr=corr,started_ns=start,time_ns=clock.ns));raise
                received.append(dict(corr=corr,started_ns=start,time_ns=clock.ns,reply_hex=r.hex()));return r
            d._recv_for=traced
            requests=[message.Request(7+i,1,16,struct.pack('<I',case['argument_ms']),7).pack() for i in range(2 if case['pipeline'] else 1)]
            def write(raw):
                started=clock.ns;clock.sleep(case['write_ms']/1000);written=clock.ns
                packets=[]
                if case['kind']=='length':
                    at=0
                    while at<len(raw):n=int.from_bytes(raw[at:at+2],'little');packets.append(raw[at+2:at+2+n]);at+=2+n
                else:packets=[cobs.unframe(part) for part in raw.split(b'\0') if part]
                wire=max(len(p)+2 for p in packets) if case['kind']=='length' else max(len(cobs.frame(p)) for p in packets)
                transfer=(wire+64*3)*10/115200 if case['kind']=='cobs' else 0
                floor=case['argument_ms']/1000+1+transfer;delay=round(floor*1e9)-1_000_000
                events.append(dict(action='write',started_ns=started,time_ns=written,wire_length=wire,minimum_ns=round(floor*1e9),requests_hex=[p.hex() for p in packets]))
                for i,p in enumerate(packets):
                    if case['missing'] and i==len(packets)-1:continue
                    req=message.Request.unpack(p);reply=message.Result(req.corr,message.COMPLETED,message.SUCCESS,b'').pack()
                    frame=(struct.pack('<H',len(reply))+reply) if case['kind']=='length' else cobs.frame(reply)
                    due=written+(i+1)*delay
                    stream.schedule.extend([(due-10_000_000,frame[:2]),(due,frame[2:])])
                stream.schedule.sort(key=lambda row:row[0])
            stream.on_write=write;outcome='success';replies=[]
            try:replies=d._exchange_once(requests,2,256,[]) if case['pipeline'] else [d._send(requests[0])]
            except TimeoutError:outcome='timeout'
            return dict(outcome=outcome,finished_ns=clock.ns,received=received,events=events,
                        replies_hex=[r.hex() for r in replies],writes_hex=[w.hex() for w in stream.writes],requests_hex=[r.hex() for r in requests])
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in ('link.py','frames.py','cobs.py','message.py')}
