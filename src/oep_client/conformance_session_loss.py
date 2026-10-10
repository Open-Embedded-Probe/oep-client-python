"""Independent end/open loss recovery checks on explicit logical retention equipment."""
import secrets
import struct
from .conformance import require
from .conformance_retention import RetentionChecks
from .conformance_session_loss_sample import SampleSessionLossHost


class SessionLossChecks(RetentionChecks):
    make_host=staticmethod(SampleSessionLossHost)

    def case(self, operation, loss='reply', large=False, lease=60000, transition=None):
        c,a=self.c,self.a
        host=self.make_host(c,a.functions)
        sid=secrets.randbelow(0xffffffff)+1
        while sid==self.state()['last']:sid=secrets.randbelow(0xffffffff)+1
        boot=c.boot
        if operation=='end':
            c.success(c.request(c.core['open'],struct.pack('<IB',60000,0),session=sid,corr=1))
            c.corr=1
            self.created_one(self.create(sid))
            c.corr=65535
            request=c.request(c.core['end'],session=sid,corr=65535)
            if large:request+=b'\x40\x04\0'+b'\0'*4
        else:
            c.corr=1
            request=c.request(c.core['open'],struct.pack('<IB',lease,0),session=sid,corr=1)
            if large:request+=b'\x40\0\0'
        c.session=None  # fixture does not install an unconfirmed session
        discard=a.discard_reply if loss=='reply' else a.discard_request
        require(discard(request) is None,'loss returned original response')
        a.wait_ms(1050)
        before=self.state();first=len(c.trace)
        if transition=='boot':a.restart(boot^1)
        def barrier():
            ready=transition!='barrier'
            require(a.pending(a.primary)==[] and a.receive(a.primary)==[], 'logical reader not drained')
            a.trace.append({'method':'reader_barrier','scope':'logical-model','completed':ready})
            return ready
        try:
            result=host.recover_session(request,boot,barrier)
            if transition=='boot':expected=('boot-changed','unknown',None)
            elif transition=='barrier':expected=('reader-unavailable','unknown',None)
            elif operation=='end':expected=('observed-ended' if large else 'end-confirmed','unknown' if large else 'success',None)
            elif lease==1000:expected=('session-unavailable','success',None)
            else:expected=('open-active','unknown' if large else 'success',None if large else lease)
            require(tuple(result[k] for k in ('status','original_outcome','lease_ms'))==expected,'recovery decision')
            requests=[bytes.fromhex(row['request_hex']) for row in c.trace[first:]]
            mutations=[raw for raw in requests if struct.unpack_from('<BHHBI',raw)[2:4] in ((0,c.core['open']),(0,c.core['end']))]
            require(mutations==([] if transition else [request]), 'recovery changed bytes/corr or automatically repeated mutation')
            require(all(struct.unpack_from('<BHHBI',raw)[2]==0 or struct.unpack_from('<BHHBI',raw)[3]==19 for raw in requests),
                    'recovery created or closed sample resources')
            after=self.state()
            if not transition and operation=='end':
                require(after['holder'] is None and not after['resources'] and after['high']==65535 and
                        after['next_resource_id']==before['next_resource_id'],'end replay state')
                require(c.corr==65535,'end recovery consumed/wrapped corr')
                if loss=='reply':require(after==before,'end replay mutated already-ended state')
            if not transition and operation=='open':
                require(after['next_resource_id']==before['next_resource_id'] and not after['resources'],'open recovery allocated resources')
                require(after['holder']==(None if lease==1000 else sid),'open recovery liveness')
                require(c.corr==(2 if lease==1000 else 3),'open recovery missing live-session read')
            self.decisions.append({'operation':operation,'loss':loss,'large':large,'lease':lease,'transition':transition,
                                   'request_hex':request.hex(),'decision':result,'before':before,'after':after})
        finally:
            # Explicit fixture cleanup; never part of the recovery host decision.
            if self.state()['holder']==sid:
                c.success(c.request(c.core['end'],session=sid))
            c.session=None

    def run(self):
        self.decisions=[]
        report=super().run()
        if report['status']!='passed':self.c.abort=True
        def controls():
            require(getattr(self.a,'reset_scope',None)=='logical-model' and all(callable(getattr(self.a,key,None)) for key in
                    ('discard_request','discard_reply','restart','pending','receive')), 'explicit session-loss controls required')
        self.c.check('CORE-SESSION-LOSS-CONTROL','explicit logical sample',controls)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name,kwargs in [('END',{'operation':'end'}),('END-LOST',{'operation':'end','large':True}),
                            ('END-UNSENT',{'operation':'end','loss':'request'}),('OPEN',{'operation':'open'}),
                            ('OPEN-LOST',{'operation':'open','large':True}),('OPEN-UNSENT',{'operation':'open','loss':'request'}),
                            ('OPEN-EXPIRED',{'operation':'open','lease':1000}),
                            ('BOOT',{'operation':'open','transition':'boot'}),('BARRIER',{'operation':'end','transition':'barrier'})]:
            first=len(self.a.trace)
            self.c.check('CORE-SESSION-LOSS-'+name,'core §4.1/5.2/6/9; sample host',lambda kwargs=kwargs:self.case(**kwargs))
            self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        from .conformance_route_sample import encoded
        report.update(status='passed' if all(row['status']=='passed' for row in self.c.results) else 'failed',
                      checks=self.c.results,adapter_trace=self.a.trace,session_loss_decisions=encoded(self.decisions),
                      scope='sample same-route end/open recovery',
                      unchecked=['production Host integration','physical reader resynchronization/reconnect',
                                 'multiple pending or concurrent writers','arbitrary extension side effects','other retention geometry'])
        return report
