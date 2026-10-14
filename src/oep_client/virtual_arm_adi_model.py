"""Logical ARM raw transfers and abstract bus blocks; no SWD electrical implementation."""
import copy
import struct
from .virtual_attach_model import Attachments, AttachModel
from .virtual_core import Core, Completed, Reject, tlv


def fixed_transfer(data):
    if len(data) < 6: return len(data)
    count = struct.unpack_from('<H', data, 4)[0]; at = 6
    for _ in range(count):
        if at >= len(data) or data[at] & 0xf0: return len(data)
        at += 1 if data[at] & 2 else 5
        if at > len(data): return len(data)
    return at


class ArmAdi(Attachments):
    functions = (1,2,3,4,5)
    names = Attachments.names + (b'oep.target.arm-adi',)
    def __init__(self, ep):
        super().__init__(ep); self.targets = {}; self.acks = []; self.packets = []; self.executed = []; self.bus = []
        self.block_fault = None; self.block_word = None; self.deliver = True
    def list_page(self, first, limit): return struct.pack('<H', len(self.functions)) + super().list_page(first, limit)[2:]
    def describe(self, fn):
        if fn == 5: return (tlv(7,b'\x10\x07'), tlv(3,struct.pack('<H',16)))
        return super().describe(fn)
    def packet(self, parent, req, value=None):
        for _ in range(3):
            phase = {5:'tar',13:'data',15:'data',14:'buffer'}.get(req)
            ack = (self.block_fault[2] if self.block_fault is not None and
                   (self.block_word,phase) == self.block_fault[:2] else self.acks.pop(0) if self.acks else 1)
            self.packets.append((parent,req,ack,value))
            if ack != 2: break
        if ack != 1: return {2:1,4:3}.get(ack,2), ack, None
        target = self.targets[parent]; address = req & 12; bank = target['ap'] if req & 1 else target['dp']
        memory_access = bool(req & 1) and address == 12
        if memory_access:
            tar = target['ap'].get(4,0)
            if req & 2: current = target['memory'].get(tar,0)
            else:
                current = value
                if self.deliver: target['memory'][tar] = value
            self.bus.append((parent,'read' if req & 2 else 'write',tar,current))
            if target['ap'].get(0,0) & 0x37 == 0x12:
                target['ap'][4] = (tar & ~1023) | ((tar+4) & 1023)
        if req & 2:
            if req & 1:
                result = target['pending']; target['pending'] = current if memory_access else bank.get(address,0)
            elif address == 12: result = target['pending']
            else: result = bank.get(address,0)
        else:
            bank[address] = value; result = None
            # A write invalidates the AP read buffer. This fixture chooses 0 for UNKNOWN.
            target['pending'] = 0
        self.executed.append((parent,req,value,result))
        return 0, ack, result
    def dispatch_request(self, fn, op, payload, tags):
        if fn != 5:
            value = super().dispatch_request(fn,op,payload,tags)
            if fn <= 3 and op == 17 and len(value) >= 13 and not getattr(value,'outcome',0):
                parent = struct.unpack_from('<I',value)[0]
                self.targets[parent] = dict(dp={},ap={},pending=0,memory={})
            return value
        if len(payload) < (6 if op == 16 else 10): raise Reject(3)
        parent = struct.unpack_from('<I',payload)[0]
        if op == 16:
            count = struct.unpack_from('<H',payload,4)[0]; at = 6; steps = []
            for _ in range(count):
                if at >= len(payload): raise Reject(3)
                if payload[at] & 0xf0: raise Reject(11,b'\0')     # reserved req bits: unsupported, nothing run (debug §6)
                req = payload[at]; size = 1 if req & 2 else 5
                if at + size > len(payload): raise Reject(3)
                steps.append((req,None if req & 2 else struct.unpack_from('<I',payload,at+1)[0])); at += size
            if at != len(payload): raise Reject(3)
            if 11 + 4*sum(bool(req & 2) for req,_ in steps) > self.ep.probe.max_frame: raise Reject(11,b'\0')
        else:
            address,count = struct.unpack_from('<IH',payload,4)
            if len(payload) != 10 + (4*count if op == 18 else 0) or address & 3: raise Reject(3)
            if count > 4: raise Reject(11,b'\0')
        self.resource(parent,'connection')
        if self.connections[parent]['fn'] != 3: raise Reject(4,tlv(1,b'\x06'))
        if op == 16:
            done,status,ack,values = 0,0,0,[]
            for req,value in steps:
                status,ack,result = self.packet(parent,req,value)
                if status: break
                done += 1
                if req & 2: values.append(result)
            return Completed(struct.pack('<HBBH',done,status,ack,len(values))+b''.join(struct.pack('<I',v) for v in values), 0 if not status else (2 if done else 1))
        values,done,status = [],0,0
        try:
            for i in range(count):
                self.block_word = i
                status,_,_ = self.packet(parent,5,(address+4*i) & 0xffffffff)
                if status: break
                status,_,_ = self.packet(parent,13 if op == 18 else 15,
                    struct.unpack_from('<I',payload,10+4*i)[0] if op == 18 else None)
                if status: break
                if op == 17:
                    status,_,value = self.packet(parent,14)
                    if status: break
                    values.append(value)
                done += 1
        finally:
            self.block_word = None
        return Completed(struct.pack('<HB',done,status)+b''.join(struct.pack('<I',v) for v in values),
                         0 if not status else (2 if done else 1))


class ArmAdiModel(AttachModel):
    def install(self): self.extension = ArmAdi(self.ep); self.ep.current_core = Core(self.ep,self.extension)
    def handle(self, request):
        if len(request) < 6 or struct.unpack_from('<H',request,3)[0] != 5: return super().handle(request)
        e = self.extension; fixed,free = e.fixed,e.free; data = request[10:]
        e.fixed = {16:fixed_transfer(data),17:10,18:10+(4*struct.unpack_from('<H',data,8)[0] if len(data)>=10 else 0)}; e.free = set()
        try: return self.ep.handle(request)
        finally: e.fixed,e.free = fixed,free
    def arm_state(self):
        state = self.attach_state(); e = self.extension
        state.update(targets=copy.deepcopy(e.targets),acks=list(e.acks),packets=list(e.packets),executed=list(e.executed),bus=list(e.bus),
                     block_fault=e.block_fault,block_word=e.block_word,deliver=e.deliver)
        return state
    def arm_seed(self, parent, dp, ap, pending):
        if parent not in self.extension.targets or any(type(k) is not int or k not in (0,4,8,12) or type(v) is not int or not 0<=v<=0xffffffff for row in (dp,ap) for k,v in row.items()) or type(pending) is not int or not 0<=pending<=0xffffffff: raise ValueError('explicit parent/register/pending seed required')
        target = self.extension.targets[parent]; target['dp'].update(dp); target['ap'].update(ap); target['pending'] = pending
    def arm_acks(self, values):
        if not isinstance(values,list) or any(type(v) is not int or not 0<=v<=7 for v in values): raise ValueError('explicit raw ACK list required')
        self.extension.acks = list(values)
    def arm_memory(self, parent, values):
        if parent not in self.extension.targets or not isinstance(values,dict) or any(type(k) is not int or not 0<=k<=0xffffffff or k&3 or type(v) is not int or not 0<=v<=0xffffffff for k,v in values.items()): raise ValueError('explicit aligned u32 memory seed required')
        self.extension.targets[parent]['memory'].update(values)
    def arm_block_fault(self, index, phase, ack):
        if type(index) is not int or not 0<=index<=3 or phase not in ('tar','data','buffer') or type(ack) is not int or ack not in (0,2,4):
            raise ValueError('explicit word index, phase and failing raw ACK required')
        self.extension.block_fault = (index,phase,ack)
    def arm_delivery(self, value):
        if type(value) is not bool: raise ValueError('explicit posted write delivery bool required')
        self.extension.deliver = value
