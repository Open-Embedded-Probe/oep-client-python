"""Independent common write acceptance and counted debug completion checks."""
import struct
from .conformance import require, ops, tlvs


def decode_debug_counted(frame, requested):
    """The shape depends on done, never on whether a status is known."""
    require(type(requested) is int and 0<=requested<=65535 and len(frame)>=8 and frame[0]==2 and frame[3]==1,'completed debug shape')
    done,status=struct.unpack_from('<HB',frame,5)
    require(done<=requested and len(frame)>=8+4*done,'debug done/count')
    values=[struct.unpack_from('<I',frame,8+4*i)[0] for i in range(done)]
    tlvs(frame[8+4*done:])
    return {'done':done,'status':status,'values':values}


def debug_succeeded(frame, decoded, requested, registry):
    return frame[3:5]==bytes((registry['resolutions']['completed'],registry['outcomes']['success'])) and \
           decoded['status']==registry['status']['ok'] and decoded['done']==requested


class OutcomeChecks:
    def __init__(self, checks, adapter):self.c,self.a=checks,adapter

    def fresh(self):
        result=self.a.restart_fixture(0,0)
        require(result['scope']=='logical-model' and result['boot_id']!=self.c.boot,'explicit fresh logical boot required')
        self.c.confirm();self.c.identity();require(self.c.boot==result['boot_id'],'logical boot readback')

    def request(self, kind, sid, payload=b''):
        adopted=self.a.adoptions['stream' if kind=='write' else 'debug']
        key={'write':'write_op','batch':'batch_op','single':'single_op'}[kind]
        return self.c.request(adopted[key],payload,session=sid,fn=adopted['fn'])

    def write(self, sid, data, accepted, outcome):
        request=self.request('write',sid,struct.pack('<H',len(data))+data);before=self.a.outcome_state()
        frame=self.c.exchange(request)
        require(frame[3:5]==bytes((1,outcome)) and len(frame)>=7,'write must complete with expected outcome')
        actual=struct.unpack_from('<H',frame,5)[0];tlvs(frame[7:])
        require(actual==accepted and actual<=len(data),'accepted count')
        after=self.a.outcome_state()
        require(after['queued']==before['queued']+data[:accepted] and after['executed']==before['executed'],
                'write accepted a non-prefix, lost bytes or executed debug work')
        return request,frame

    def write_case(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            if kind=='ZERO':self.write(sid,b'',0,0)
            elif kind=='FULL':self.write(sid,b'abcd',4,0)
            elif kind=='PARTIAL':self.write(sid,b'abcdef',4,2)
            elif kind=='FAILED':
                self.write(sid,b'abcd',4,0);self.write(sid,b'ef',0,1);self.write(sid,b'',0,0)
            elif kind=='DRAIN':
                self.write(sid,b'abcdef',4,2)
                require(self.a.drain_tx(3)==b'abc','fixture drain prefix')
                self.write(sid,b'efg',3,0)
            else:
                for data,accepted,outcome in [(b'abcdef',4,2),(b'z',0,1)]:
                    request,frame=self.write(sid,data,accepted,outcome)
                    before=self.a.outcome_state()
                    # Changing current capacity must not change a cached acceptance result.
                    if before['queued']:self.a.drain_tx(len(before['queued']))
                    stable=self.a.outcome_state()
                    require(self.c.exchange(request)==frame and self.a.outcome_state()==stable,'replayed write re-enqueued/recomputed')
                    if data==b'abcdef':self.write(sid,b'abcd',4,0)

    def batch(self, sid, count, done, status, outcome):
        request=self.request('batch',sid,struct.pack('<H',count));before=self.a.outcome_state();frame=self.c.exchange(request)
        require(frame[3:5]==bytes((1,outcome)),'accepted debug failure returned wrong resolution/outcome')
        decoded=decode_debug_counted(frame,count)
        require(decoded=={'done':done,'status':status,'values':[0xa000+i for i in range(done)]},'debug done/status/prefix values')
        require(debug_succeeded(frame,decoded,count,self.c.reg)==(outcome==0),'debug host success classification')
        after=self.a.outcome_state();require(after['executed']==before['executed']+decoded['values'] and after['queued']==before['queued'],
                                            'debug progress side effects differ from done')
        return request,frame

    def debug_case(self, kind, status=1):
        self.fresh()
        with self.c.holding(3000) as sid:
            if kind=='ZERO':self.a.configure_debug(0,status);self.batch(sid,0,0,0,0)
            elif kind=='FULL':self.batch(sid,3,3,0,0)
            elif kind=='STATUS':
                self.a.configure_debug(0,status);self.batch(sid,3,0,status,1)
                self.a.configure_debug(2,status);self.batch(sid,3,2,status,2)
            elif kind=='REPLAY':
                for done in (0,2):
                    self.a.configure_debug(done,status);request,frame=self.batch(sid,3,done,status,1 if done==0 else 2)
                    self.a.configure_debug(None,status);stable=self.a.outcome_state()
                    require(self.c.exchange(request)==frame and self.a.outcome_state()==stable,'debug replay re-executed or recomputed failure')
            else:
                frame=self.c.exchange(self.request('single',sid));require(frame[3:5]==b'\x01\0' and len(frame)>=9,'single success layout')
                require(struct.unpack_from('<I',frame,5)[0]==0x12345678,'single success payload');tlvs(frame[9:])
                for status in range(1,6):
                    self.a.configure_debug(0,status);request=self.request('single',sid);frame=self.c.exchange(request)
                    require(frame[3:5]==b'\x01\x01' and len(frame)>=6 and frame[5]==status,'no-done failure must be status-only completed failed')
                    tlvs(frame[6:]);stable=self.a.outcome_state()
                    require(self.c.exchange(request)==frame and self.a.outcome_state()==stable,'single failure replay changed state')

    def malformed(self, kind):
        self.fresh()
        with self.c.holding(3000) as sid:
            before=self.a.outcome_state()
            payload=b'\x04\0ab' if kind=='write' else b'\x03'
            self.c.rejected(self.request(kind,sid,payload),'malformed')
            require(self.a.outcome_state()==before,'malformed operation accepted side effects')

    def locks(self):
        self.fresh();before=self.a.outcome_state()
        for kind,payload in [('write',b'\0\0'),('batch',b'\0\0'),('single',b'')]:
            self.c.rejected(self.request(kind,0,payload),'session_required')
        require(self.a.outcome_state()==before,'lock refusal mutated state')

    def run(self):
        start=len(self.c.results)
        def identify():
            require(all(self.c.reg['outcomes'].get(name)==value for name,value in [('success',0),('failed',1),('partial',2)]) and
                    all(self.c.reg['status'].get(name)==value for name,value in [('ok',0),('wait',1),('line',2),('fault',3),('timeout',4),('state',5)]),
                    'selected SPEC outcome/status differs from sample')
            a=self.a.adoptions
            require(a['stream']['component']=='positioned-stream-v1' and a['debug']['component']=='debug-status-v1' and
                    a['stream']['fn']!=a['debug']['fn'] and a['reference'] and a['tx_capacity']==4 and a['max_steps']==8,'explicit outcome adoption/geometry required')
            require(getattr(self.a,'reset_scope',None)=='logical-model' and all(callable(getattr(self.a,key,None)) for key in
                    ('restart_fixture','configure_debug','drain_tx','outcome_state')),'logical outcome controls required')
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot()
            for name,keys in [('stream',('write_op',)),('debug',('batch_op','single_op'))]:
                adopted=a[name];require(any(row['fn']==adopted['fn'] for row in entries),'adopted fn absent')
                offered=ops(dict(reversed(self.c.describe(adopted['fn'])))[self.c.reg['describe_common']['ops']])
                require({adopted[key] for key in keys}<=offered,'adopted operation not offered')
            self.c.observed['outcome_adoptions']=a
        self.c.check('IF-OUTCOME-CONTEXT','common §1.4/3; explicit adoption',identify)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('WRITE-'+kind,lambda kind=kind:self.write_case(kind)) for kind in ('ZERO','FULL','PARTIAL','FAILED','DRAIN','REPLAY')]
        cases.append(('WRITE-MALFORMED',lambda:self.malformed('write')))
        cases.extend((('DEBUG-ZERO',lambda:self.debug_case('ZERO')),('DEBUG-FULL',lambda:self.debug_case('FULL'))))
        cases.extend(('DEBUG-STATUS-'+str(status),lambda status=status:self.debug_case('STATUS',status)) for status in range(1,6))
        cases.extend((('DEBUG-REPLAY',lambda:self.debug_case('REPLAY')),('DEBUG-SINGLE',lambda:self.debug_case('SINGLE')),
                      ('DEBUG-MALFORMED',lambda:self.malformed('batch')),('LOCKS',self.locks)))
        for name,case in cases:
            first=len(self.a.trace);self.c.check('IF-OUTCOME-'+name,'common §1.4/3; core §4.2/5.2',case)
            self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        checks=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in checks) else 'failed','full_conformance':False,
                'scope':'explicit common write acceptance and debug completion contracts',
                'levels':{'core':'completion/cache prerequisites','interface':'selected adopted write/status contracts','oep-interface':'not executed'},
                'checks':checks,'observed':self.c.observed,'adapter_trace':self.a.trace,
                'unchecked':['physical delivery/target progress','extension-specific status semantics','connection close/lifetime',
                             'production Host integration','other capacities/batch layouts','concurrent writers']}
