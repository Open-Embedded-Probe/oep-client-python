"""Wait starts at write completion or completion of the preceding response."""
import hashlib
from pathlib import Path
from .conformance import require,spec_identity


REVIEWED={'docs/oep-core.ja.md':'cddf917d49f36bbb1a5b24fbda247675834e1c398beaa179728b8efa31831bf9',
          'docs/oep-transports.ja.md':'5a2598c669656f1b875e8f053e316748fbc12cd5347750830aba91daa962f39e'}


def reviewed_identity(path):
    registry,identity=spec_identity(path)
    require(registry['timing']['host_wait_add_ms']==1000 and registry['timing']['notify_pending_max_frames']==2 and registry['constants']['min_max_frame']==64,'reviewed Host deadline rules changed')
    for name,h in REVIEWED.items():require(hashlib.sha256((Path(path)/name).read_bytes()).hexdigest()==h,'reviewed deadline document changed: '+name)
    return identity


class HostDeadlineChecks:
    groups=('SINGLE','TIMEOUT','PIPELINE','PIPELINE-TIMEOUT')
    def __init__(self,a,*,spec):self.a=a;self.identity=reviewed_identity(spec);self.results=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='HOST-DEADLINE-'+name,level='core',clause='core §4.4; transports §6')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.results.append(row)
    def context(self):
        for name in ('run_case','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit virtual-clock deadline controls required')
    def cases(self,name):
        pipeline=name.startswith('PIPELINE');missing=name.endswith('TIMEOUT') or name=='TIMEOUT'
        for kind in ('length','cobs'):
            for argument in ((0,1500,5000) if name=='SINGLE' else (0,5000) if name=='TIMEOUT' else (0,1500)):
                for write in ((400,4000) if name=='SINGLE' else (4000,)):
                    yield dict(kind=kind,argument_ms=argument,write_ms=write,pipeline=pipeline,missing=missing)
    def group(self,name):
        for case in self.cases(name):
            r=self.a.run_case(case);writes=[e for e in r['events'] if e['action']=='write'];require(len(writes)==1,'deadline test unexpectedly rewrote a request')
            w=writes[0];wire=16 if case['kind']=='length' else 19
            expected_requests=[(b'\1'+(7+i).to_bytes(2,'little')+b'\1\0\x10\7\0\0\0'+case['argument_ms'].to_bytes(4,'little')).hex()
                               for i in range(2 if case['pipeline'] else 1)]
            require(w['requests_hex']==r['requests_hex']==expected_requests,'deadline fixture request bytes differ')
            floor_ns=round((case['argument_ms']/1000+1+(wire+64*3)*10/115200 if case['kind']=='cobs' else case['argument_ms']/1000+1)*1e9)
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
            self.observations.append(dict(group=name,case=case,result=r,minimum_ns=floor_ns))
    def run(self):
        self.check('CONTEXT',self.context)
        if self.results[-1]['status']=='passed':
            for name in self.groups:self.check(name,lambda name=name:self.group(name))
        else:
            for name in self.groups:self.results.append(dict(id='HOST-DEADLINE-'+name,status='blocked'))
        return dict(status='passed' if all(row['status']=='passed' for row in self.results) else 'failed',checks=self.results,
                    full_conformance=False,spec=self.identity,observations=self.observations,
                    implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['actual wall-clock accuracy and physical transports','resync/retry/own-link negotiations',
                               'different per-request argument times in a single pipeline','production Host integration and registry migration'])
