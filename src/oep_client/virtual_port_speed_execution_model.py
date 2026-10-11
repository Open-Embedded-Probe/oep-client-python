"""Explicit bounded source/sink execution time in the UART handshake model."""
import struct
from .virtual_core import Core,tlv
from .virtual_port_speed_model import PortSpeedModel,PortSpeedExtension


class TimedPortSpeedExtension(PortSpeedExtension):
    def dispatch_request(self,fn,op,payload,tags):
        value=super().dispatch_request(fn,op,payload,tags)
        if fn==4 and op in (16,17):
            duration=self.model.execution_ms;self.model.execution_ms=0
            self.model.clock_ms+=duration
            self.model.executions.append(dict(fn=fn,op=op,duration_ms=duration))
        return value


class PortSpeedExecutionModel(PortSpeedModel):
    def __init__(self,**kwargs):
        self.execution_ms=0;self.executions=[]
        super().__init__(**kwargs)
        self.ep.max_op_ms=5000
        self.ep.static[0]=tuple(t for t in self.ep.static[0] if t[0]!=77)+(tlv(77,struct.pack('<I',5000)),)
    def install(self):
        self.extension=TimedPortSpeedExtension(self);self.extension.frame_limit=self.frame_limit
        self.ep.current_core=Core(self.ep,self.extension)
    def execution(self,duration):
        if type(duration)is not int or not 0<=duration<=5000 or self.pending is not None or self.execution_ms:
            raise ValueError('idle fixture and bounded 0..5000ms execution required')
        self.execution_ms=duration
    def speed_state(self):
        state=super().speed_state()
        state.update(lease_deadline=self.ep.current_core.deadline,execution_ms=self.execution_ms,executions=list(self.executions))
        return state
    def restart_fixture(self):
        self.execution_ms=0
        return super().restart_fixture()
