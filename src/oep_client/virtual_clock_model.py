"""Exact logical nanosecond clock and exchange/read observations; no wall-clock inference."""
import copy
from .virtual_resource_model import ResourceModel, Resources
from .virtual_core import Core


class ClockModel(ResourceModel):
    def __init__(self, boot_id=17):
        self.nanoseconds=0;self.reads=[];self.exchanges=[];self.execution_ns=0;self.delivery_ns=0
        super().__init__(lambda:self.nanoseconds//1_000_000,boot_id)
        self.ep._clock_ns=self.read_ns
    def read_ns(self):
        self.reads.append(self.nanoseconds-self.ep._origin_ns)
        return self.nanoseconds
    def handle(self, request):
        received=self.nanoseconds-self.ep._origin_ns;first=len(self.reads)
        self.clock_advance(self.execution_ns)
        frame=super().handle(request)
        self.clock_advance(self.delivery_ns)
        self.exchanges.append(dict(request=request,received_ns=received,sent_ns=self.nanoseconds-self.ep._origin_ns,reads=self.reads[first:]))
        return frame
    def restart_fixture(self):return self.clock_boot((self.ep.boot_id+1)&0xffffffff)
    def clock_boot(self, boot):
        if type(boot) is not int or not 0<=boot<=0xffffffff or boot==self.ep.boot_id or self.ep.current_core.holder is not None:
            raise ValueError('new explicit u32 boot and unowned model required')
        self.ep.reboot(boot);self.extension=Resources();self.ep.current_core=Core(self.ep,self.extension)
        self.execution_ns=self.delivery_ns=0
        return dict(scope='logical-model',boot_id=boot)
    def clock_advance(self, ns):
        if type(ns) is not int or ns<0 or self.nanoseconds-self.ep._origin_ns+ns>0xfffffffffffffffe:
            raise ValueError('nondecreasing explicit u64 elapsed ns required')
        self.nanoseconds+=ns
    def clock_delays(self, execution, delivery):
        if any(type(v) is not int or not 0<=v<=1_000_000 for v in (execution,delivery)):raise ValueError('bounded logical execution/delivery ns required')
        self.execution_ns,self.delivery_ns=execution,delivery
    def clock_state(self):
        c=self.ep.current_core
        return dict(scope='logical-ns',now_ns=self.nanoseconds-self.ep._origin_ns,boot=self.ep.boot_id,
            reads=list(self.reads),exchanges=copy.deepcopy(self.exchanges),
            ownership=dict(holder=c.holder,last=c.last,deadline=c.deadline,lease=c.lease,owner=c.owner,high=c.high,cache=copy.deepcopy(c.cache),
                           resources=copy.deepcopy(self.extension.live),next_id=self.extension.next_id))
    def clock_tick(self):self.ep.current_core.tick()
