"""u32 segment wrap/release and independent u64 byte positions."""
import hashlib
from pathlib import Path
import struct
from .conformance import require, tlvs
from .conformance_registered_ops import refusal_carries


def answered(reply,fixed):
    """A completed success whose payload starts with `fixed`; the rest is the answer's TLV tail (core §2.3)."""
    if reply[3:5]!=b'\1\0' or reply[5:5+len(fixed)]!=fixed:return False
    tlvs(reply[5+len(fixed):]);return True


def refused_stale(reply):
    """rejected unavailable carrying cause 6, other TLVs in any order around it (core §4.3)."""
    return reply[3:5]==b'\0\4' and refusal_carries('unavailable',reply[5:],b'\1\1\0\6')
from .conformance_host_deadline import reviewed_identity as core_identity

REVIEWED={'interfaces/oep-if-capture.ja.md':'c3d2464e1542a7ba77f1c827dba3ae5fe27b2749b482f2bd8c78a1163856e71e',
          'interfaces/oep-if-common.ja.md':'25fed0b1b8b7f246a8b71d02cea810249803b1c3c1e9c3df466c04f38687f70c'}

def reviewed_identity(path):
    identity=core_identity(path)
    for name,h in REVIEWED.items():require(hashlib.sha256((Path(path)/name).read_bytes()).hexdigest()==h,'reviewed capture serial document changed: '+name)
    return identity

class CaptureSerialChecks:
    def __init__(self,adapter,*,spec):
        self.a=adapter;self.identity=reviewed_identity(spec);self.checks=[];self.observations=[]
        # Pin the independently reviewed capture/common contracts as well.
        self.documents=dict(REVIEWED)
    def check(self,name,fn):
        row=dict(id='CAPTURE-SERIAL-'+name,level='oep-interface',clause='core §2.6; common §1.3; capture §2.2/3.2')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.checks.append(row)
    def context(self):
        for name in ('run_case','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit serial/position seed and sample controls required')
    def case(self,case):
        r=self.a.run_case(case);require(r['case']==case and r['seed_scope']=='logical-retired-prefix','capture seed scope differs')
        states={s['label']:s['state'] for s in r['states']};seed=case['seed'];base=(1<<40)+17;serial=lambda n:(seed+n)&0xffffffff
        require(list(states)==['seed','full','before-release','released','wrapped','partial','future-release','stopped'],'capture serial timeline differs')
        if case['kind']=='logic':data=b'\x2e';partial=b'\6';rows=[[0,1],[1,0]]
        else:data=struct.pack('<HHHH',0xc053,0xc02b,0xc0a3,0xc07b);partial=data[:4];rows=[[10,20],[30,40]]
        require(r['rows']==rows,'serial sample stimulus differs')
        width=len(data);generation=states['seed']['generation'];require(generation==1 and states['seed']['serial']==seed and states['seed']['base']==base,'seed changed generation/position')
        expected={'full':(5,serial(2),base,data*2,[serial(0),serial(1)]),
                  'before-release':(5,serial(2),base,data*2,[serial(0),serial(1)]),
                  'released':(3,serial(2),base+width,data,[serial(1)]),
                  'wrapped':(5,serial(3),base+width,data*2,[serial(1),serial(2)]),
                  'partial':(3,serial(3),base+2*width,data,[serial(2)]),
                  'future-release':(3,serial(3),base+3*width,b'',[]),
                  'stopped':(1,serial(4),base+3*width,partial,[serial(3)])}
        for label,(status,next_serial,position,raw,ids) in expected.items():
            s=states[label];require((s['state'],s['serial'],s['base'],s['data_hex'],[row[0] for row in s['segments']])==(status,next_serial,position,raw.hex(),ids),'capture retained serial/data state differs: '+label)
            require(s['generation']==generation and s['holder']==7,'serial wrap changed generation/session')
            for row in s['segments']:
                require((next_serial-row[0])%2**32<2**30,'segment metadata retained beyond quarter span')
                index=(row[0]-seed)%2**32
                require(row==[serial(index),base+index*width,1 if index==3 else 2,index*2000000,1000000,0xffffffff,3 if index==3 else 1 if index==2 else 0,generation],'segment metadata position/time/count/flags differs')
        require(states['partial']['current']==states['future-release']['current']==rows[:1] and not states['stopped']['current'],'future release discarded unfinished acquisition')
        trace=r['trace'];pages=[e for e in trace if e['fn']==2 and e['op']==22]
        require(len(pages)==10,'serial page requests missing')
        for e in trace:
            request=bytes.fromhex(e['request_hex']);reply=bytes.fromhex(e['reply_hex'])
            require(int.from_bytes(request[3:5],'little')==e['fn'] and request[5]==e['op'],'trace request fn/op differs')
            require(reply[:3]==b'\2'+request[1:3],'response corr differs')
            if e['op'] in (21,22) and e['fn']==2:require(e['before']==e['after'],'capture read/segments consumed state')
            if e.get('replay'):require(e['before']==e['after'] and answered(reply,b''),'cached release modified wrapped acquisition')
            if e['fn']==2 and e['op']==22:
                first=int.from_bytes(request[10:14],'little');available=e['before']['segments'];next_serial=e['before']['serial']
                index=next((i for i,row in enumerate(available) if row[0]==first),0)
                rows_expected=[] if first==next_serial or not available else available[index:]
                wire=bytes((0,len(rows_expected)))+b''.join(struct.pack('<IQIQIIBI',*row) for row in rows_expected)
                require(answered(reply,wire),'wrapped serial page from/inclusive/fallback differs')
            if e['fn']==2 and e['op']==21:
                # read: generation(u32) position(u64) max(u16) -> position(u64) flags(u8) len(u16) data (capture §3.2).
                require(len(request)==24,'read request shape differs');generation_read,position,maximum=struct.unpack_from('<IQH',request,10);s=e['before']
                if generation_read!=s['generation']:
                    require(refused_stale(reply) and e['before']==e['after'],'wrong generation read not refused with cause 6');continue
                raw=bytes.fromhex(s['data_hex']);start=max(s['base'],min(position,s['base']+len(raw)));part=raw[start-s['base']:start-s['base']+maximum]
                # Before write_pos a recycled position moves forward with gap (also when nothing is left: write_pos, gap, len 0);
                # at or past write_pos it is write_pos, no gap, len 0. max=0 is an empty success under the same rules.
                flags=(2 if position<s['base'] else 0)|(1 if start+len(part)<s['base']+len(raw) else 0)
                require(answered(reply,struct.pack('<QBH',start,flags,len(part))+part),'u64 read position/gap/count differs')
        for op in (21,23):
            stale=[e for e in trace if e['fn']==2 and e['op']==op and int.from_bytes(bytes.fromhex(e['request_hex'])[10:14],'little')==generation-1]
            require(stale and all(refused_stale(bytes.fromhex(e['reply_hex'])) and e['before']==e['after'] for e in stale),'wrong generation read/release missing or changed capture')
        reads=[struct.unpack_from('<QH',bytes.fromhex(e['request_hex']),14) for e in trace if e['fn']==2 and e['op']==21]
        require(any(m==0 for _,m in reads) and any(e['op']==21 and e['before']['data_hex']=='' and e['before']['base']>base and
                    int.from_bytes(bytes.fromhex(e['request_hex'])[14:22],'little')<e['before']['base'] for e in trace),'max=0 and all-released read stimulus missing')
        wrapped=states['wrapped']['segments'][-1];stopped=states['stopped']['segments'][0]
        require(wrapped[6]&1 and stopped[6]&2 and stopped[2]==1,'resumed gap/short stopped segment flags missing')
        self.observations.append(r)
    def run(self):
        self.check('CONTEXT',self.context)
        if self.checks[-1]['status']=='passed':
            for kind in ('logic','analog'):
                for seed in (0,0xfffffffe):self.check(f'{kind}-{seed}',lambda kind=kind,seed=seed:self.case(dict(kind=kind,seed=seed)))
        return dict(status='passed' if all(c['status']=='passed' for c in self.checks) else 'failed',checks=self.checks,observations=self.observations,
                    spec=self.identity,documents_sha256=self.documents,full_conformance=False,implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['arbitrary segment/mark retention capacity and billions of real acquisitions','streaming/group notification serial interleavings','physical sampling and production Host integration'])
