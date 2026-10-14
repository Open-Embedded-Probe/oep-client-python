"""u16 notification retention span across blocked and partly written frames."""
from .conformance import require
from .conformance_host_deadline import reviewed_identity


class NotificationRetentionChecks:
    def __init__(self,adapter,*,spec):self.a=adapter;self.identity=reviewed_identity(spec);self.checks=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='NOTIFY-RETENTION-'+name,level='core',clause='core §2.6/10.3/10.4; transports §3')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.checks.append(row)
    def context(self):
        for name in ('run_case','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit producer/held-frame observations required')
    def case(self,case):
        r=self.a.run_case(case);obs=r['observations']
        require(r['case']==case and [o['label'] for o in obs]==['initial','before','boundary','after'] and [o['produced'] for o in obs]==[1,16383,16384,16386],'retention boundary stimulus differs')
        for o in obs:
            s=o['state'];require(s['holder']==7 and s['next_seq']['1']==(case['seed']+o['produced'])%65536 and s['next_seq']['2']==1,'retention changed session or failed to count dropped notifications')
            require(s['bytes_held']<=128,'notification byte queue exceeded bound')
            frames=[bytes.fromhex(p) for p in s['pending_hex']]
            if s['active']:
                frame=bytes.fromhex(s['active']['frame_hex']);require(0<s['active']['offset']<len(frame),'invalid partial frame observation');frames+=[frame]
            for frame in frames:
                require(frame[0] in (3,4) and len(frame)>=6,'retained notification header malformed')
                fn=int.from_bytes(frame[1:3],'little');seq=int.from_bytes(frame[3:5],'little')
                require(fn in (1,2) and (s['next_seq'][str(fn)]-seq)%65536<16384,'retained notification seq reached quarter span')
        initial,before,boundary,after=[o['state'] for o in obs]
        if case['active']:
            require(initial['active'] and before['active']==initial['active'] and before['open'],'partial frame changed before boundary')
            require(not boundary['open'] and boundary['active'] is None and not boundary['pending_hex'] and not after['open'],'stalled partial frame not abandoned by closing route at bound')
        else:
            require(all(s['open'] and s['active'] is None for s in (initial,before,boundary,after)),'unstarted stale notifications closed route')
            first=bytes.fromhex(initial['pending_hex'][0]);require(first.hex() in before['pending_hex'] and first.hex() not in boundary['pending_hex'],'oldest pending notification not evicted at bound')
            fn2=initial['pending_hex'][1];require(fn2 in boundary['pending_hex'] and fn2 in after['pending_hex'],'independent fn sequence evicted by another space')
        self.observations.append(r)
    def run(self):
        self.check('CONTEXT',self.context)
        if self.checks[-1]['status']=='passed':
            for seed in (0,65530):
                for active in (False,True):self.check(f'{seed}-'+('ACTIVE' if active else 'PENDING'),lambda seed=seed,active=active:self.case(dict(seed=seed,active=active)))
        return dict(status='passed' if all(c['status']=='passed' for c in self.checks) else 'failed',checks=self.checks,observations=self.observations,
                    spec=self.identity,full_conformance=False,implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['capture/dmseq and other wrapping value spaces','physical writer buffers and simultaneous producers','Host retained values and reconnect processing'])
