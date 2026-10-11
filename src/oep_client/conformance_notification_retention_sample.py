"""Bounded logical notification retention under real producer sequence advances."""
import hashlib
from pathlib import Path
import struct
from . import message
from .virtual_route_model import RouteModel


class SampleNotificationRetentionAdapter:
    def run_case(self,case):
        model=RouteModel(lambda:0,boot_id=17)
        model.handle(message.Request(1,0,16,struct.pack('<IB',60000,0),7).pack())
        for fn in (1,2):model.handle(message.Request(1+fn,fn,1,struct.pack('<HI',0,0),7).pack())
        model.seed_sequence(1,case['seed'])
        model.hold('primary',0)
        model.emit(1,1);model.emit(2,2)
        if case['active']:
            model.hold('primary',1);model.service()
        position=0
        def advance(n):
            nonlocal position
            for i in range(n):
                if i%2:model.feed(1,position,b'x');position+=1
                else:model.emit(1,3)
        def state():
            pipe=model.pipes['primary'];active=pipe['active']
            return dict(open=pipe['open'],holder=model.ep.current_core.holder,
                        next_seq={str(k):v for k,v in model.extension.subscriptions.items()},
                        active={'frame_hex':active[0].hex(),'offset':active[1]} if active else None,
                        pending_hex=[p.hex() for p in pipe['push']],
                        bytes_held=sum(len(p) for p in model.queue('primary')),
                        resources=dict(model.extension.live))
        observations=[dict(label='initial',produced=1,state=state())]
        advance(16382);observations.append(dict(label='before',produced=16383,state=state()))
        advance(1);observations.append(dict(label='boundary',produced=16384,state=state()))
        advance(2);observations.append(dict(label='after',produced=16386,state=state()))
        return dict(case=case,observations=observations)
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('virtual_route_model.py','virtual_data_model.py','virtual_core.py','message.py')}
