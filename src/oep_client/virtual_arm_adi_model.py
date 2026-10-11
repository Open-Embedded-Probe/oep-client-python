"""Logical ARM raw transfers and abstract bus blocks; no SWD electrical implementation."""
import copy
import struct
from .virtual_attach_model import Attachments, AttachModel
from .virtual_core import Core, Completed, Reject, tlv


def fixed_transfer(data):
    if len(data) < 4: return len(data)
    count = struct.unpack_from('<H', data, 2)[0]; at = 4
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
    def list_page(self, first, limit): return struct.pack('<H', len(self.functions)) + super().list_page(first, limit)[2:]
    def describe(self, fn):
        if fn == 5: return (tlv(7,b'\x10\x07'), tlv(3,struct.pack('<H',16)))
        return super().describe(fn)
    def packet(self, parent, req, value=None):
        for _ in range(3):
            ack = self.acks.pop(0) if self.acks else 1
            self.packets.append((parent,req,ack,value))
            if ack != 2: break
        if ack != 1: return {2:1,4:3}.get(ack,2), ack, None
        target = self.targets[parent]; address = req & 12; bank = target['ap'] if req & 1 else target['dp']
        if req & 2:
            if req & 1:
                result = target['pending']; target['pending'] = bank.get(address,0)
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
            if fn <= 3 and op == 17 and len(value) >= 11 and not getattr(value,'outcome',0):
                parent = struct.unpack_from('<H',value)[0]
                self.targets[parent] = dict(dp={},ap={},pending=0,memory={})
            return value
        if len(payload) < (4 if op == 16 else 8): raise Reject(3)
        parent = struct.unpack_from('<H',payload)[0]
        if op == 16:
            count = struct.unpack_from('<H',payload,2)[0]; at = 4; steps = []
            for _ in range(count):
                if at >= len(payload) or payload[at] & 0xf0: raise Reject(3)
                req = payload[at]; size = 1 if req & 2 else 5
                if at + size > len(payload): raise Reject(3)
                steps.append((req,None if req & 2 else struct.unpack_from('<I',payload,at+1)[0])); at += size
            if at != len(payload): raise Reject(3)
            if 11 + 4*sum(bool(req & 2) for req,_ in steps) > self.ep.probe.max_frame: raise Reject(11,b'\0')
        else:
            address,count = struct.unpack_from('<IH',payload,2)
            if len(payload) != 8 + (4*count if op == 18 else 0) or address & 3: raise Reject(3)
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
        target = self.targets[parent]; values = []
        for i in range(count):
            addr = (address+4*i) & 0xffffffff
            if op == 18: target['memory'][addr] = struct.unpack_from('<I',payload,8+4*i)[0]
            else: values.append(target['memory'].get(addr,0))
            self.bus.append((parent,'write' if op == 18 else 'read',addr,target['memory'].get(addr,0)))
        if count: target['ap'][4] = (address+4*count) & 0xffffffff
        return struct.pack('<HB',count,0)+b''.join(struct.pack('<I',v) for v in values)


class ArmAdiModel(AttachModel):
    def install(self): self.extension = ArmAdi(self.ep); self.ep.current_core = Core(self.ep,self.extension)
    def handle(self, request):
        if len(request) < 6 or struct.unpack_from('<H',request,3)[0] != 5: return super().handle(request)
        e = self.extension; fixed,free = e.fixed,e.free; data = request[10:]
        e.fixed = {16:fixed_transfer(data),17:8,18:8+(4*struct.unpack_from('<H',data,6)[0] if len(data)>=8 else 0)}; e.free = set()
        try: return self.ep.handle(request)
        finally: e.fixed,e.free = fixed,free
    def arm_state(self):
        state = self.attach_state(); e = self.extension
        state.update(targets=copy.deepcopy(e.targets),acks=list(e.acks),packets=list(e.packets),executed=list(e.executed),bus=list(e.bus))
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
