"""Wait floor (whole-request processing bound + host_wait_add_ms + transfer) and its start point.

The floor is core §4.4: the processing bound is max_op_ms unless the operation defines a shorter
whole-request bound; never the argument time alone. fn 0 quick operations use min(max_op_ms, core_quick_op_max_ms),
and core_quick_op_max_ms while max_op_ms is still unknown. The wait starts at write completion or at the
completion of the preceding response."""
import hashlib
from pathlib import Path
from .conformance import require,spec_identity


REVIEWED={'docs/oep-core.ja.md':'d63f99f1809146ad41f1643526c81417b366d7a9d85af3845daee6654a229a14',
          'docs/oep-transports.ja.md':'2117de20712f7dfad6c81e247cf1b0b206ccf7528fcdf70702cbfce1c17dbadc'}
QUICK_MS=1000  # core_quick_op_max_ms


def processing_bound_ms(case):
    # Independent restatement of core §4.4; the fixture op16 defines no shorter whole-request bound.
    if case['quick']:return QUICK_MS if case['max_op_ms'] is None else min(case['max_op_ms'],QUICK_MS)
    return case['max_op_ms']


def request_bytes(case,corr):
    if case['quick']:return b'\1'+corr.to_bytes(2,'little')+b'\0\0\4\0\0\0\0'
    return b'\1'+corr.to_bytes(2,'little')+b'\1\0\x10\7\0\0\0'+case['argument_ms'].to_bytes(4,'little')


def reviewed_identity(path):
    registry,identity=spec_identity(path)
    require(registry['timing']['host_wait_add_ms']==1000 and registry['timing']['notify_pending_max_frames']==2 and registry['constants']['min_max_frame']==64,'reviewed Host deadline rules changed')
    require(registry['timing'].get('core_quick_op_max_ms')==QUICK_MS and registry['limits']['max_op_ms_max']==600000,'reviewed Host processing bounds changed')
    for name,h in REVIEWED.items():require(hashlib.sha256((Path(path)/name).read_bytes()).hexdigest()==h,'reviewed deadline document changed: '+name)
    return identity


class HostDeadlineChecks:
    groups=('SINGLE','TIMEOUT','PIPELINE','PIPELINE-TIMEOUT','QUICK')
    def __init__(self,a,*,spec):self.a=a;self.identity=reviewed_identity(spec);self.results=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='HOST-DEADLINE-'+name,level='core',clause='core §4.4/7.5; transports §3/6')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.results.append(row)
    def context(self):
        for name in ('run_case','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit virtual-clock deadline controls required')
    def cases(self,name):
        if name=='QUICK':
            # fn 0 clock before max_op_ms is known, and after it with a shorter and a longer max_op_ms.
            for kind in ('length','cobs'):
                for maximum in (None,300,5000):
                    yield dict(kind=kind,quick=True,argument_ms=0,max_op_ms=maximum,write_ms=4000,pipeline=False,missing=False)
            return
        pipeline=name.startswith('PIPELINE');missing=name.endswith('TIMEOUT')
        # (argument, max_op_ms): an argument shorter than max_op_ms detects an argument-only floor.
        pairs=((0,1),(0,5000),(1500,5000),(5000,5000)) if name=='SINGLE' else ((0,1),(1500,5000))
        for kind in ('length','cobs'):
            for argument,maximum in pairs:
                for write in ((400,4000) if name=='SINGLE' else (4000,)):
                    yield dict(kind=kind,quick=False,argument_ms=argument,max_op_ms=maximum,write_ms=write,pipeline=pipeline,missing=missing)
    def group(self,name):
        for case in self.cases(name):
            r=self.a.run_case(dict(case));writes=[e for e in r['events'] if e['action']=='write'];require(len(writes)==1,'deadline test unexpectedly rewrote a request')
            w=writes[0];requests=[request_bytes(case,7+i) for i in range(2 if case['pipeline'] else 1)]
            # length: u16 prefix; COBS: CRC16, one overhead byte (<254) and two delimiters.
            wire=len(requests[0])+(2 if case['kind']=='length' else 5)
            require(w['requests_hex']==r['requests_hex']==[q.hex() for q in requests],'deadline fixture request bytes differ')
            bound=processing_bound_ms(case);transfer=(wire+64*3)*10/115200 if case['kind']=='cobs' else 0
            floor_ns=round((bound/1000+1+transfer)*1e9)
            require(w['started_ns']==0 and w['time_ns']==case['write_ms']*1_000_000 and w['wire_length']==wire and w['minimum_ns']==floor_ns,'deadline write/floor fixture differs')
            received=r['received'];expected=1 if case['pipeline'] and case['missing'] else 0 if case['missing'] else 2 if case['pipeline'] else 1
            require(len(received)==expected,'Host timed out early or returned missing results')
            start=w['time_ns']
            for i,p in enumerate(received):
                require(bytes.fromhex(p['reply_hex'])==b'\2'+(7+i).to_bytes(2,'little')+b'\1\0','deadline result header/corr/outcome differs')
                require(p['corr']==7+i and p['started_ns']==start,'pipeline wait did not start at preceding completed result')
                require(p['time_ns']==start+floor_ns-1_000_000,'Host returned before the complete scheduled frame')
                start=p['time_ns']
            timeouts=[e for e in r['events'] if e['action']=='timeout']
            if case['missing']:
                require(r['outcome']=='timeout' and len(timeouts)==1 and timeouts[0]['started_ns']==start,'missing-response deadline start differs')
                require(timeouts[0]['time_ns']>=start+floor_ns,'Host timed out before required wait')
            else:require(r['outcome']=='success' and not timeouts,'valid delayed reply rejected before minimum wait')
            self.observations.append(dict(group=name,case=case,result=r,minimum_ns=floor_ns,processing_bound_ms=bound))
    def run(self):
        self.check('CONTEXT',self.context)
        if self.results[-1]['status']=='passed':
            for name in self.groups:self.check(name,lambda name=name:self.group(name))
        else:
            for name in self.groups:self.results.append(dict(id='HOST-DEADLINE-'+name,status='blocked'))
        return dict(status='passed' if all(row['status']=='passed' for row in self.results) else 'failed',checks=self.results,
                    full_conformance=False,spec=self.identity,observations=self.observations,
                    implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    bound_source='adapter: max_op_ms, or min(max_op_ms, core_quick_op_max_ms) for fn 0 quick ops',
                    unchecked=['actual wall-clock accuracy and physical transports','resync/retry/own-link negotiations',
                               'different per-request bounds in a single pipeline','operation-defined shorter whole-request bounds',
                               'production Host selection of the processing bound (Host.expect_ms) and registry migration'])
