"""Real Host link resends against the current virtual Core retry cache."""
from unittest.mock import patch
import hashlib
from pathlib import Path
import struct
from . import cobs,frames,link,message
from .conformance_host_deadline_sample import Clock,ScheduledStream
from .virtual_resource_model import ResourceModel


MAX_OP_MS=2000  # the fixture Probe's describe max_op_ms


class SampleHostResendAdapter:
    def run_case(self,name):
        kind='length' if name.endswith('LENGTH') else 'cobs';clock=Clock();stream=ScheduledStream(clock,kind)
        stream.keeps_boundaries=False  # reliable length-framed non-TCP fixture; TCP reconnect is separate.
        peer=ResourceModel(lambda:clock.ns//1_000_000,boot_id=17)
        peer.ep.probe.max_frame=64;peer.ep.max_inflight=2
        peer.handle(message.Request(1,0,16,struct.pack('<IB',60000,0),7).pack())
        seen={};events=[]
        with patch.object(link,'time',clock),patch.object(frames,'time',clock):
            d=link.SerialLink.on_stream(stream,kind,.05);d.held=lambda:True
            # core §4.4 (a09fa45): the fixture op16 defines no shorter whole-request bound, so the floor uses max_op_ms.
            # Production Host.expect_ms carries argument times only; this adapter states the bound explicitly.
            d.expected_s=lambda:MAX_OP_MS/1000
            def write(raw):
                started=clock.ns;clock.sleep(.4);written=clock.ns
                if kind=='length':
                    packets=[];at=0
                    while at<len(raw):n=int.from_bytes(raw[at:at+2],'little');packets.append(raw[at+2:at+2+n]);at+=2+n
                else:packets=[cobs.unframe(p) for p in raw.split(b'\0') if p]
                wire=max(len(p)+2 for p in packets) if kind=='length' else max(len(cobs.frame(p)) for p in packets)
                floor_ns=round((MAX_OP_MS/1000+1+((wire+64*3)*10/115200 if kind=='cobs' else 0))*1e9)
                events.append(dict(action='write',started_ns=started,time_ns=written,wire_length=wire,minimum_ns=floor_ns,requests_hex=[p.hex() for p in packets]))
                for p in packets:
                    req=message.Request.unpack(p);before=peer.extension.next_id;deadline=peer.ep.current_core.deadline;reply=peer.handle(p)
                    events.append(dict(action='peer',time_ns=clock.ns,request_hex=p.hex(),reply_hex=reply.hex(),next_before=before,next_after=peer.extension.next_id,
                                       deadline_before=deadline,deadline_after=peer.ep.current_core.deadline))
                    is_alloc=req.fn==1 and req.op==16;seen[req.corr]=seen.get(req.corr,0)+1
                    drop=is_alloc and (req.corr==2 if name.startswith('FAILURE') else seen[req.corr]==1 and (not name.startswith('PIPELINE') or req.corr==3))
                    if name=='BAD-CRC' and is_alloc and seen[req.corr]==1:drop=False
                    if name=='FAILURE-BLOCKED' and req.fn==0 and req.op==1:drop=True
                    if drop:continue
                    frame=struct.pack('<H',len(reply))+reply if kind=='length' else cobs.frame(reply)
                    if name=='BAD-CRC' and is_alloc and seen[req.corr]==1:
                        payload=reply+b'\0\0';frame=b'\0'+cobs.encode(payload)+b'\0'
                    due=written+10_000_000;stream.schedule.append((due,frame))
                stream.schedule.sort(key=lambda row:row[0])
            stream.on_write=write
            requests=[message.Request(2+i,1,16,b'\1',7).pack() for i in range(2 if name.startswith('PIPELINE') else 1)]
            replies=[];error=None
            try:replies=d.exchange(requests,2,256) if name.startswith('PIPELINE') else [d.send(requests[0])]
            except link.TransportFailed as exc:error=type(exc).__name__
            except TimeoutError as exc:error=type(exc).__name__
            failed_before=d.failed_transport;next_error=None;next_reply=None
            if name.startswith('FAILURE'):
                try:next_reply=d.send(message.Request(3,1,19,b'',0).pack()).hex()
                except ConnectionError as exc:next_error=type(exc).__name__
            return dict(name=name,kind=kind,events=events,finished_ns=clock.ns,replies_hex=[p.hex() for p in replies],error=error,
                        failed_before_next=bool(failed_before),failed_after_next=bool(d.failed_transport),next_error=next_error,next_reply_hex=next_reply,
                        next_id=peer.extension.next_id,max_op_ms=MAX_OP_MS,live={str(rid):list(row) for rid,row in peer.extension.live.items()},retries=d.retries)
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('link.py','frames.py','cobs.py','message.py','virtual_core.py','virtual_resource_model.py','endpoint.py','virtual_bench.py')}
