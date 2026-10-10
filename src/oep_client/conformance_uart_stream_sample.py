"""Explicit adoption of common stream contracts by the logical UART fixture."""
from .conformance_uart_sample import SampleUartAdapter


class SampleUartStreamAdapter(SampleUartAdapter):
    adoption={'component':'positioned-stream-v1','addressing':'persistent-fn','fn':2,'ops':{'read':17,'marks':18,'clear':19,'mark':20,'write':21},'byte_capacity':64,'mark_capacity':5,'tx_capacity':4,'reference':'docs/uart-stream-conformance.ja.md plan-owned UART sample'}
    def restart_fixture(self,position=0,serial=0):
        result=super().restart_fixture();self.call('uart_seed',position,serial);return dict(result,position=position,next_serial=serial)
    def mark_external(self,kind,detail=0):return self.call('uart_mark_external',kind,detail)
    def stream_state(self):
        state=self.state();return {key:state[key] for key in ('position','data','marks','next_serial','tx')}
