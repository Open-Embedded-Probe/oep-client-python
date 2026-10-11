"""Multi-route link bounds and shared session/reconnect history contracts."""
import struct

from .conformance import require,tlvs
from .conformance_link_transfers import LinkTransferChecks
from .conformance_registered_ops import RegisteredOpsChecks


class LinkRoutesChecks:
    cases=('ROUTE-LIMITS','FREE-CACHE','SHARED-LOCK','RECONNECT','REPLAY-RECONNECT','END-SHARED')

    def __init__(self,checks,adapter):self.c,self.a=checks,adapter;self.observations=[]
    def select(self,route):
        before=self.a.state();boot=self.c.boot
        self.a.select(self.c,route);self.c.confirm();self.c.identity()
        require(self.c.max_frame==self.a.limits[route] and self.c.confirm_transport==route,'route-specific confirm identity/limit differs')
        if boot is not None:require(self.c.boot==boot,'route switch changed probe boot')
        self.unchanged('select',before,route)
    def unchanged(self,action,before,route=None):
        after=self.a.state();self.observations.append(dict(action=action,route=route,before=before,after=after))
        require(after==before,action+' changed protected shared state')
    def context(self):
        require(self.a.routes==(0,1,2) and self.a.limits=={0:64,1:256,2:1024} and self.a.kinds=={0:1,1:4,2:6},'explicit three logical routes required')
        require(self.a.session_route==2 and self.a.retained_bytes>=23,'TCP session route and small replay retention required')
        for name in ('select','close','reopen','trace'):require(callable(getattr(self.a,name,None)),'explicit route control/trace required')
        self.a.select(self.c,self.a.session_route)
        self.c.confirm();self.c.identity()
        entries=self.c.list_snapshot();self.functions=[(r['fn'],r['name'],r['instance'],r['revision']) for r in entries]
        for route in self.a.routes:
            self.select(route)
            require([(r['fn'],r['name'],r['instance'],r['revision']) for r in self.c.list_snapshot()]==self.functions,'function identities differ by route')
            values=[v for t,v in self.c.describe(0) if t==73]
            require(set(values)=={bytes((r,self.a.kinds[r],0 if r==1 else 255)) for r in self.a.routes},'shared transport declarations differ')
        self.a.select(self.c,self.a.session_route)
        RegisteredOpsChecks(self.c,self.a.selection).run()
        self.transfer=LinkTransferChecks(self.c,self.a);self.transfer.context()
    def reconnect(self):
        route=self.a.session_route;before=self.a.state();boot=self.c.boot
        self.a.close(route)
        try:
            self.unchanged('close',before,route)
        except Exception:
            # Do not hide the observed ownership loss behind cleanup end's
            # no_session. This changes checker bookkeeping, never Probe state.
            if self.a.state()['ownership']['holder']!=before['ownership']['holder']:self.c.session=None
            raise
        finally:
            self.a.reopen(route)
        self.select(route)
        require(self.c.boot==boot,'reopen changed probe boot')
        self.unchanged('reopen',before,route)
    def case(self,name):
        self.a.select(self.c,self.a.session_route)
        self.transfer.fresh()
        with self.c.holding(3000) as sid:
            self.a.seed(self.c,sid);seed=self.a.state()
            require(seed['plans']=={2:[1]} and seed['pins'][1][0]==4 and seed['ownership']['holder']==sid,'live protected seed missing')
            self.observations.append(dict(action='seed',case=name,state=seed))
            if name=='ROUTE-LIMITS':
                for route in (0,2,1,0,1,2):
                    self.select(route)
                    for n in (0,1,self.a.limits[route]-7,0xffffffff):self.transfer.source(n)
                    count=self.a.limits[route]-12;self.transfer.sink(bytes(i&255 for i in range(count)))
            elif name=='FREE-CACHE':
                # Sequential Probe stimulus: SID0 has no replay ledger, even if a
                # different route uses the same already-resolved corr.
                for route in self.a.routes:
                    self.select(route);before=self.a.state()
                    request=self.c.request(16,struct.pack('<I',0xffffffff),fn=4,corr=60000)
                    payload=self.c.success(request);n=self.a.limits[route]-7
                    require(payload==struct.pack('<H',n)+bytes(i&255 for i in range(n)),'SID0 was cached or route bound was reused')
                    self.unchanged('free-source',before,route)
            elif name=='SHARED-LOCK':
                for route in self.a.routes:
                    self.select(route);before=self.a.state()
                    locked=self.c.success(self.c.request(self.c.core['lock_state']))
                    require(len(locked)>=5 and locked[0]==1 and int.from_bytes(locked[1:5],'little')>0,'shared lock_state differs');tlvs(locked[5:])
                    self.transfer.refused(self.c.request(self.c.core['open'],struct.pack('<IB',3000,0),session=sid+1),'locked')
                    self.transfer.source(5);self.transfer.sink(b'abc')
                    self.unchanged('shared-lock',before,route)
            elif name=='RECONNECT':
                self.reconnect();self.transfer.source(5,sid);self.transfer.sink(b'abc',sid)
            elif name=='REPLAY-RECONNECT':
                request,payload=self.transfer.source(16,sid)
                self.reconnect();before=self.a.state()
                require(self.c.success(request)==payload,'reconnect lost retained result')
                self.unchanged('replay-after-reopen',before,self.a.session_route)
                self.transfer.refused(request+b'\x7f\0\0','malformed')
            elif name=='END-SHARED':
                self.transfer.source(5,sid);self.transfer.sink(b'abc',sid)
            self.a.select(self.c,self.a.session_route)
        if name=='END-SHARED':
            for route in self.a.routes:
                self.select(route);state=self.a.state()
                require(not state['plans'] and state['pins']==state['idle'] and state['ownership']['holder'] is None,'end cleanup not shared')
                value=self.c.success(self.c.request(self.c.core['lock_state']));require(value[:5]==b'\0'*5,'released lock_state differs');tlvs(value[5:])
                self.transfer.source(5);self.transfer.sink(b'')
    def run(self):
        start=len(self.c.results)
        self.c.check('OEP-LINK-ROUTES-CONTEXT','core §4.4/5/6; transports §3; link §2',self.context)
        context=next(row for row in self.c.results[start:] if row['id']=='OEP-LINK-ROUTES-CONTEXT')
        if context['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-LINK-ROUTES-'+name,'core §4.4/5/6; transports §3; link §2',lambda name=name:self.case(name))
        checks=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in checks) else 'failed',full_conformance=False,
            checks=checks,state_observations=self.observations,transfer_observations=getattr(self,'transfer',None).observations if hasattr(self,'transfer') else [],
            route_wire_trace=self.a.trace() if callable(getattr(self.a,'trace',None)) else [],
            scope='one logical Probe, three route bounds, shared GPIO/plan/session and TCP close/reopen replay',
            unchecked=['transport framing and actual socket/USB lifecycle','concurrent routes/windows/notification routing',
                'port_speed handshake/timing','production Host/Probe and physical performance'])
