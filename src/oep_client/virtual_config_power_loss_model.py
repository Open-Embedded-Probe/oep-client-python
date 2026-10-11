"""Byte-write journal fixture; no actual flash, wall-clock timing or GPIO device."""
import copy
import json
import struct
import zlib
from .virtual_config_model import ConfigModel


class SavePowerLoss(RuntimeError):pass


class ConfigJournal:
    def __init__(self):self.reset()
    def reset(self):self.cells=[bytearray(),bytearray()];self.active=0;self.serial=0;self.pending=None
    def encode(self,items,identities,serial):
        doc=dict(items=[[t,k.hex(),v.hex()] for (t,k),v in sorted(items.items())],
                 identities=[[fn,*row] for fn,row in sorted(identities.items())])
        body=json.dumps(doc,separators=(',',':'),ensure_ascii=True).encode('ascii')
        head=b'OEPS'+struct.pack('<IH',serial,len(body));data=head+body
        return data+struct.pack('<I',zlib.crc32(data))+b'\xa5'
    def decode(self,cell):
        if len(cell)<15 or cell[:4]!=b'OEPS':return None
        serial,size=struct.unpack_from('<IH',cell,4);end=10+size
        if len(cell)!=end+5 or cell[end+4]!=165 or zlib.crc32(cell[:end])!=int.from_bytes(cell[end:end+4],'little'):return None
        try:
            doc=json.loads(bytes(cell[10:end]));items={(t,bytes.fromhex(k)):bytes.fromhex(v) for t,k,v in doc['items']}
            identities={fn:(name,instance,revision) for fn,name,instance,revision in doc['identities']}
            return serial,dict(items=items,identities=identities)
        except (ValueError,KeyError,TypeError):return None
    def recover(self):
        choices=[(row[0],index,row[1]) for index,cell in enumerate(self.cells) if (row:=self.decode(cell)) is not None]
        self.pending=None
        if not choices:self.active=0;self.serial=0;return None
        self.serial,self.active,blob=max(choices,key=lambda row:row[0]);return copy.deepcopy(blob)
    def begin(self,items,identities):
        if self.pending is not None:raise ValueError('one journal transaction at a time')
        record=self.encode(items,identities,self.serial+1)
        target=1-self.active
        self.cells[target]=bytearray();self.pending=dict(target=target,record=record,at=0)
    def step(self):
        if self.pending is None:raise ValueError('no active journal write')
        p=self.pending;self.cells[p['target']].append(p['record'][p['at']]);p['at']+=1
        if p['at']==len(p['record']):self.serial+=1;self.active=p['target'];self.pending=None
    def image(self):return [bytes(cell).hex() for cell in self.cells]


class ConfigPowerLossModel(ConfigModel):
    def __init__(self,*,boot_id=17):
        self.journal=ConfigJournal();self.cut_at=None;self.events=[]
        super().__init__(boot_id=boot_id)
    def install(self):
        blob=self.journal.recover()
        self.saved=None if blob is None else blob['items'];self.saved_identities={} if blob is None else blob['identities']
        super().install()
    def write_config(self,items,identities):
        self.journal.begin(items,identities);written=0
        if self.cut_at==0:raise SavePowerLoss()
        while self.journal.pending is not None:
            self.journal.step();written+=1
            if self.cut_at==written:raise SavePowerLoss()
        self.saved=copy.deepcopy(items);self.saved_identities=copy.deepcopy(identities)
    def erase_config(self):self.journal.reset();super().erase_config()
    def write_steps(self):
        items=self.extension.items;ids={int.from_bytes(v[:2],'little'):self.catalog[int.from_bytes(v[:2],'little')] for (t,k),v in items.items() if t==1}
        return len(self.journal.encode(items,ids,self.journal.serial+1))
    def power_cycle(self):
        old=self.ep.boot_id;image=self.journal.image()
        self.ep.reboot((old+1)&0xffffffff);self.install()
        return dict(action='power-cycle',image=image,recovered_image=self.journal.image(),
                    boot_id=self.ep.boot_id,old_boot_id=old)
    def cut_save(self,request,step):
        total=self.write_steps()
        if type(step)is not int or not 0<=step<=total:raise ValueError('explicit byte-write cut required')
        self.cut_at=step;reply=None
        try:reply=self.handle(request)
        except SavePowerLoss:
            self.cut_at=None;event=self.power_cycle()
            event=dict(action='power-loss',cut=step,total=total,request_hex=request.hex(),reply_hex=None,
                       **{k:v for k,v in event.items() if k!='action'})
            self.events.append(event);return event
        finally:self.cut_at=None
        raise ValueError('save did not reach the requested cut: '+repr(reply))
