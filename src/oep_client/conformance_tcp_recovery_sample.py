"""Explicit sample TCP Host policy using actual frames and current virtual Core.

This is not SerialLink's production reconnect implementation.
"""
from pathlib import Path
import hashlib
import struct
from unittest.mock import patch
from . import frames, message
from .conformance_host_deadline_sample import Clock, ScheduledStream
from .virtual_resource_model import ResourceModel
from .virtual_resource_model import Resources
from .virtual_core import Core


class TcpFixtureStream(ScheduledStream):
    keeps_boundaries = True
    def __init__(self, clock):
        super().__init__(clock, 'length')
        self.closed = False
        self.eof = False
    def read(self, n=1):
        if self.closed or self.eof:
            raise ConnectionError('TCP closed')
        return super().read(n)
    def write(self, data):
        if self.closed:
            raise ConnectionError('write on closed TCP')
        return super().write(data)
    def close(self):
        self.closed = True
        self.rx.clear()
        self.schedule.clear()


class SampleTcpRecoveryAdapter:
    def run_case(self, name):
        clock = Clock()
        peer = ResourceModel(lambda: clock.ns // 1_000_000, boot_id=17)
        core = peer.ep.current_core
        peer.ep.probe.max_frame = 64
        events = []
        streams = []
        request = message.Request(2, 1, 16, b'\1', 7).pack()
        def snapshot():
            return dict(holder=core.holder, deadline=core.deadline,last=core.last,high=core.high,boot_id=peer.ep.boot_id,
                        next_id=peer.extension.next_id,
                        live={str(k):list(v) for k,v in peer.extension.live.items()},
                        cache={str(k):repr(v) for k,v in core.cache.items()})
        def connect():
            epoch = len(streams)
            stream = TcpFixtureStream(clock)
            streams.append(stream)
            events.append(dict(action='connect', epoch=epoch, state=snapshot()))
            def write(raw):
                at = 0
                while at < len(raw):
                    n = int.from_bytes(raw[at:at+2], 'little')
                    data = raw[at+2:at+2+n]
                    at += n+2
                    before = snapshot()
                    reply = core.handle(data, epoch)
                    events.append(dict(action='request', epoch=epoch, request_hex=data.hex(),
                                       reply_hex=reply.hex() if reply else None, before=before, after=snapshot()))
                    if not reply:
                        continue
                    wire = struct.pack('<H',len(reply))+reply
                    if epoch == 0 and data == request:
                        if name.startswith('PAUSE'):
                            split = 1 if name == 'PAUSE-HEADER' else 4
                            stream.schedule.extend([(clock.ns, b'\0\0'+wire[:split]),
                                                    (clock.ns+400_000_000, wire[split:])])
                        elif name == 'LENGTH': stream.schedule.append((clock.ns,b'\xff\xff'))
                        elif name == 'SHAPE': stream.schedule.append((clock.ns,b'\3\0\2\2\0'))
                        elif name in ('EOF','REBOOT','REBOOT-SAME','EXPIRED'): stream.eof = True
                        elif name == 'BLOCKED': stream.schedule.append((clock.ns,b'\xff\xff'))
                    elif name == 'BLOCKED' and epoch == 1:
                        pass
                    else: stream.schedule.append((clock.ns,wire))
                stream.schedule.sort(key=lambda row:row[0])
            stream.on_write = write
            return stream, frames.LengthFrames(stream,64)
        with patch.object(frames,'time',clock):
            stream, parser = connect()
            core.handle(message.Request(1,0,16,struct.pack('<IB',60000,0),7).pack(),0)
            parser.send_many([request])
            if name.startswith('PAUSE'):
                first = parser.recv(.1)
                events.append(dict(action='wait', time_ns=clock.ns, reply_hex=first.hex() if first else None,
                                   buffered=parser._buffer.hex()))
                reply = parser.recv(.5)
                events.append(dict(action='result', epoch=0,time_ns=clock.ns,reply_hex=reply.hex() if reply else None))
            else:
                error = None
                try:
                    reply = parser.recv(1)
                    message.Result.unpack(reply)
                except (frames.FramingLost, ConnectionError, message.ProtocolError) as exc:
                    error = type(exc).__name__
                events.append(dict(action='broken', epoch=0,error=error))
                state = snapshot()
                stream.close()
                events.append(dict(action='close', epoch=0,closed=stream.closed,state=snapshot()))
                if name in ('REBOOT','REBOOT-SAME','EXPIRED'):
                    if name=='EXPIRED':
                        clock.sleep(60)
                        core.tick()
                    else:
                        peer.extension.release()
                        peer.ep.reboot(18 if name=='REBOOT' else 17)
                        peer.extension=Resources()
                        core=peer.ep.current_core=Core(peer.ep,peer.extension)
                    events.append(dict(action='transition',name=name,time_ns=clock.ns,before=state,after=snapshot()))
                stream, parser = connect()
                confirm = message.Request(10,0,1,b'OEP?\1\1',0).pack()
                parser.send_many([confirm])
                reply = parser.recv(1)
                valid = False
                if reply:
                    result = message.Result.unpack(reply)
                    valid = result.corr == 10 and result.succeeded and result.payload[:5] == b'OEP!\1'
                events.append(dict(action='confirm',epoch=1,valid=valid,reply_hex=reply.hex() if reply else None))
                if valid and name in ('REBOOT','REBOOT-SAME','EXPIRED'):
                    boot=int.from_bytes(result.payload[12:16],'little')
                    if name=='EXPIRED':
                        parser.send_many([request])
                        replay=parser.recv(1)
                        events.append(dict(action='historical-result',reply_hex=replay.hex()))
                    parser.send_many([message.Request(11,0,19,b'',0).pack()])
                    status=message.Result.unpack(parser.recv(1))
                    events.append(dict(action='core-state',reply_hex=status.pack().hex()))
                    parser.send_many([message.Request(12,0,18,b'',7).pack()])
                    gone=message.Result.unpack(parser.recv(1))
                    events.append(dict(action='old-session',reply_hex=gone.pack().hex()))
                    discovered=[]
                    if name.startswith('REBOOT'):
                        first=0
                        while True:
                            parser.send_many([message.Request(13+first,0,2,struct.pack('<H',first),0).pack()])
                            page=message.Result.unpack(parser.recv(1))
                            total,count=struct.unpack_from('<HB',page.payload)
                            data=page.payload[3:]
                            for _ in range(count):
                                fn,instance,revision,n=struct.unpack_from('<HHBB',data)
                                discovered.append(dict(fn=fn,instance=instance,revision=revision,name=data[6:6+n].decode()))
                                data=data[6+n:]
                            events.append(dict(action='list-page',first=first,reply_hex=page.pack().hex()))
                            first+=count
                            if first>=total:break
                            if not count:raise ValueError('list paging stalled')
                    parser.send_many([message.Request(15,1,19,b'',0).pack()])
                    resources=message.Result.unpack(parser.recv(1))
                    events.append(dict(action='resource-state',reply_hex=resources.pack().hex()))
                    events.append(dict(action='invalidate',boot_id=boot,session=None,resource_ids=[],
                                       old_fn_cache_cleared=name.startswith('REBOOT'),discovered=discovered))
                elif valid:
                    parser.send_many([request])
                    reply = parser.recv(1)
                    events.append(dict(action='result',epoch=1,reply_hex=reply.hex() if reply else None))
                else:
                    stream.close()
                    events.append(dict(action='blocked',epoch=1,closed=stream.closed))
                events.append(dict(action='preserved',before=state,after=snapshot()))
        return dict(name=name,events=events,state=snapshot(),connections=len(streams))
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('frames.py','message.py','virtual_core.py','virtual_resource_model.py','endpoint.py','virtual_bench.py')}
