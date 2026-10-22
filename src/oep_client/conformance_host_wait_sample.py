"""Actual Host/save/link budget path with an in-memory stream, no port or timer wait."""
import hashlib
from pathlib import Path
import struct
from . import host,link,message,settings


class MemoryStream:
    def __init__(self,baud):self.baudrate=baud;self.timeout=0;self.writes=[]
    @property
    def in_waiting(self):return 0
    def reset_input_buffer(self):pass
    def read(self,n=1):return b''
    def write(self,data):self.writes.append(bytes(data));return len(data)


def max_op_ms(h):
    for tag,value in h._describes.get(0,()):
        if tag==77:return struct.unpack('<I',value)[0]
    return None




class SampleHostWaitAdapter:
    def setup(self,kind,baud,maximum,timeout):
        stream=MemoryStream(baud);driver=link.SerialLink.on_stream(stream,'length' if kind=='length' else 'cobs',timeout)
        if baud:driver.base_baud=115200
        records=[];advertised=[256]
        def send(raw):
            driver._write([raw]);req=message.Request.unpack(raw)
            records.append(dict(request_hex=raw.hex(),expect_ms=h.expect_ms,wait_s=driver._wait(),floor_s=driver.wait_floor_s(),
                                transfer_s=driver.transfer_s(),tx_len=driver.tx_len,frame=driver.max_frame,baud=driver.baud))
            if req.fn==0 and req.op==1:
                payload=struct.pack('<4sBHIBI',b'OEP!',1,advertised[0],max(65535,advertised[0]),1,17)+b'\1\1\0\0'
            else:payload=struct.pack('<I',0x12345678)
            return message.Result(req.corr,message.COMPLETED,message.FAILED if failed[0] else message.SUCCESS,payload).pack()
        h=host.Host(send,revision=1,session=7,lease_ms=1000)
        h._boot_id=17
        h._describes={0:[(77,struct.pack('<I',maximum)),(7,bytes([settings.OP['save']&~7,1<<(settings.OP['save']&7)]))]}
        driver.expected_s=lambda:h.expect_ms/1000;driver.lease_s=lambda:h.lease_ms/1000;h.on_limits=driver._limits
        failed=[False]
        return h,driver,records,advertised,failed
    def save(self,case):
        h,d,rows,advertised,failed=self.setup(case['kind'],case['baud'],case['maximum'],case['timeout'])
        d._limits(dict(max_frame=case['frame']));cfg=settings.Settings(h);failed[0]=case.get('failure',False)
        error=None;value=None
        with h.expecting(case['outer_ms']):
            try:value=cfg.save()
            except host.Failed as exc:error=type(exc).__name__
            restored=h.expect_ms
        return dict(record=rows[-1],restored_outer_ms=restored,final_expect_ms=h.expect_ms,error=error,value=value,
                    save_code=settings.OP['save'])
    def limits(self,kind,baud):
        h,d,rows,advertised,failed=self.setup(kind,baud,5000,0.05)
        for value in (256,64,65535):
            # production Host.confirm: its processing bound is Host.op_bound_ms, min(max_op_ms, core_quick_op_max_ms) (core §4.4)
            advertised[0]=value
            h.confirm()
            rows[-1]['max_op_ms']=max_op_ms(h)
        return rows
    def source_sha256(self):
        root=Path(__file__).parent
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                ('host.py','core.py','settings.py','link.py','cobs.py','frames.py','message.py','catalog.py','registry.py')}
