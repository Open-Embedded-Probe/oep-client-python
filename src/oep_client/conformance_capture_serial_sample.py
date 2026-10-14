"""Explicit retired-prefix seed, real sample feeding and raw capture exchanges."""
import hashlib
from pathlib import Path
import struct
from . import message
from .virtual_core import tlv
from .virtual_logic_model import LogicModel
from .virtual_analog_model import AnalogModel


class SampleCaptureSerialAdapter:
    def run_case(self,case):
        model=(LogicModel if case['kind']=='logic' else AnalogModel)(boot_id=17)
        model.ep.probe.max_frame=256
        feed=model.logic_feed if case['kind']=='logic' else model.analog_feed
        e=model.extension;trace=[];states=[];corr=0
        def state():
            return dict(state=e.state,serial=e.serial,base=e.base,data_hex=bytes(e.data).hex(),segments=[list(r) for r in e.segments],
                        current=[list(r) for r in e.current],generation=e.generation,holder=model.ep.current_core.holder)
        def exchange(fn,op,payload=b'',sid=0):
            nonlocal corr
            corr+=1;request=message.Request(corr,fn,op,payload,sid).pack();before=state();reply=model.handle(request)
            trace.append(dict(fn=fn,op=op,request_hex=request.hex(),reply_hex=reply.hex(),before=before,after=state()))
            return message.Result.unpack(reply)
        def save(label):states.append(dict(label=label,state=state()))
        exchange(0,16,struct.pack('<IB',60000,0),7)
        roles=(0,1) if case['kind']=='logic' else (1,3)
        exchange(1,16,b''.join(tlv(144,struct.pack('<HBH',2,role,ch)) for ch,role in enumerate(roles)),7)
        settings=tlv(64,b'\2')+tlv(66,struct.pack('<I',1000))+tlv(67,struct.pack('<I',2))+tlv(68,struct.pack('<I',2))
        exchange(2,16,settings,7);start=exchange(2,17,b'',7);generation=int.from_bytes(start.payload[0:4],'little')
        # A retired, unretained prefix is represented by an explicit seed. No
        # claim that billions of acquisitions were physically performed.
        e.serial=case['seed'];e.base=(1<<40)+17;save('seed')
        rows=[[0,1],[1,0]] if case['kind']=='logic' else [[10,20],[30,40]]
        feed(rows*2);save('full')
        for first in (case['seed'],(case['seed']+1)&0xffffffff,(case['seed']+2)&0xffffffff,(case['seed']-1)&0xffffffff,(case['seed']+9)&0xffffffff):
            exchange(2,22,struct.pack('<I',first))
        exchange(2,21,struct.pack('<IQH',generation,e.base-1,1000))
        exchange(2,21,struct.pack('<IQH',generation,e.base+1000,1000))
        exchange(2,21,struct.pack('<IQH',generation,e.base+1,0));exchange(2,21,struct.pack('<IQH',generation,e.base-1,0))
        exchange(2,21,struct.pack('<IQH',generation-1,e.base,1000))
        exchange(2,23,struct.pack('<II',generation-1,case['seed']),7)
        exchange(2,23,struct.pack('<II',generation,(case['seed']-1)&0xffffffff),7);save('before-release')
        release=exchange(2,23,struct.pack('<II',generation,case['seed']),7);release_request=bytes.fromhex(trace[-1]['request_hex']);save('released')
        feed(rows);save('wrapped')
        before=state();reply=model.handle(release_request)
        trace.append(dict(fn=2,op=23,request_hex=release_request.hex(),reply_hex=reply.hex(),before=before,after=state(),replay=True))
        for first in (case['seed'],(case['seed']+1)&0xffffffff,(case['seed']+2)&0xffffffff,(case['seed']+3)&0xffffffff):exchange(2,22,struct.pack('<I',first))
        exchange(2,21,struct.pack('<IQH',generation,(1<<40)+17,1000))
        exchange(2,23,struct.pack('<II',generation,(case['seed']+1)&0xffffffff),7)
        feed(rows[:1]);save('partial')
        exchange(2,23,struct.pack('<II',generation,(case['seed']+10)&0xffffffff),7);save('future-release')
        # Everything finished is released: an old position reads write_pos with gap and no data.
        exchange(2,21,struct.pack('<IQH',generation,(1<<40)+17,1000))
        exchange(2,18,b'',7);save('stopped')
        exchange(2,22,struct.pack('<I',(case['seed']+3)&0xffffffff))
        return dict(case=case,states=states,trace=trace,rows=rows,seed_scope='logical-retired-prefix')
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('virtual_logic_model.py','virtual_analog_model.py','virtual_core.py','virtual_gpio_model.py','message.py')}
