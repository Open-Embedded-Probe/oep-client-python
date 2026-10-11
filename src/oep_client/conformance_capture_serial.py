"""u32 segment wrap/release and independent u64 byte positions."""
import hashlib
from pathlib import Path
import struct
from .conformance import require
from .conformance_host_deadline import reviewed_identity as core_identity

REVIEWED={'interfaces/oep-if-capture.ja.md':'bc63cae4b7d717f2228c0412534b1c409eedb5a20547cefd02dd99f0893016ec',
          'interfaces/oep-if-common.ja.md':'6744c4e30af5c5211a73b8f6050b7343e12e9679e7b62ba068fc904fa0ef06b7'}

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
        row=dict(id='CAPTURE-SERIAL-'+name,level='oep-interface',clause='core §2.6; common §1.3; capture §2/3.2')
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
            if e.get('replay'):require(e['before']==e['after'] and reply[3:]==b'\1\0','cached release modified wrapped acquisition')
            if e['fn']==2 and e['op']==22:
                first=int.from_bytes(request[10:14],'little');available=e['before']['segments'];next_serial=e['before']['serial']
                index=next((i for i,row in enumerate(available) if row[0]==first),0)
                rows_expected=[] if first==next_serial or not available else available[index:]
                wire=bytes((0,len(rows_expected)))+b''.join(struct.pack('<IQIQIIBI',*row) for row in rows_expected)
                require(reply[3:]==b'\1\0'+wire,'wrapped serial page from/inclusive/fallback differs')
            if e['fn']==2 and e['op']==21:
                position,maximum=struct.unpack_from('<QI',request,14);s=e['before'];raw=bytes.fromhex(s['data_hex']);start=max(s['base'],min(position,s['base']+len(raw)));part=raw[start-s['base']:start-s['base']+maximum]
                flags=(2 if position<s['base'] else 0)|(1 if start+len(part)<s['base']+len(raw) else 0)
                require(reply[3:]==b'\1\0'+struct.pack('<QBI',start,flags,len(part))+part,'u64 read position/gap/count differs')
        stale=next(e for e in trace if e['fn']==2 and e['op']==23 and int.from_bytes(bytes.fromhex(e['request_hex'])[10:14],'little')==0)
        require(bytes.fromhex(stale['reply_hex'])[3:]==b'\0\4\1\1\0\6' and stale['before']==stale['after'],'wrong generation release changed capture')
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
