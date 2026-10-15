"""Probe role discard and minimum message header, separate from payload refusal."""
from .conformance import require
from .conformance_host_deadline import reviewed_identity
from .conformance_registered_ops import fixed_then_tlvs


def answers(reply_hex,fixed_hex):
    """The reply starts with these bytes (header and fixed part); TLVs may follow (core §2.3, §4.3)."""
    return reply_hex is not None and fixed_then_tlvs(bytes.fromhex(reply_hex),bytes.fromhex(fixed_hex))


class ProbeInputChecks:
    request=bytes.fromhex('0103000100100700000001')
    def __init__(self,adapter,*,spec):self.a=adapter;self.identity=reviewed_identity(spec);self.checks=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='PROBE-INPUT-'+name,level='core',clause='core §2.4/4.1/6.1')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.checks.append(row)
    def context(self):
        for name in ('prepare','advance_to','raw','state','source_sha256'):
            require(callable(getattr(self.a,name,None)),'explicit raw input/state/timer controls required')
    def fresh(self):
        s=self.a.prepare()
        require(s['holder']==7 and s['owner_hex']==b'input-checker'.hex() and s['deadline']==1000 and s['live']=={'1':[1,1]} and s['next_id']==2 and s['high']==2 and set(s['cache'])=={'1','2'},'discard ownership/resource/cache seed differs')
    def observe(self,group,data):
        before=self.a.state();reply=self.a.raw(data);after=self.a.state()
        row=dict(group=group,input_hex=data.hex(),reply_hex=reply.hex() if reply is not None else None,before=before,after=after)
        self.observations.append(row);return row
    def discard(self):
        self.fresh();self.a.advance_to(500)
        self.discard_inputs('role','short')
    def discard_inputs(self,role_group,short_group):
        for role in range(256):
            if role==1:continue
            for size in (10,11,64):
                data=(bytes((role,))+self.request[1:]+b'\0'*64)[:size]
                o=self.observe(role_group,data)
                require(o['reply_hex'] is None and o['before']==o['after'],'Probe answered or changed state for non-request role')
        for size in range(10):
            o=self.observe(short_group,self.request[:size])
            require(o['reply_hex'] is None and o['before']==o['after'],'Probe answered or changed state for short request header')
    def unowned(self,mode):
        self.fresh();self.a.advance_to(500 if mode=='end' else 1000)
        transition=self.observe(mode+'-transition',bytes.fromhex('01030000001107000000') if mode=='end' else b'\xff'+self.request[1:])
        b,a=transition['before'],transition['after']
        require(answers(transition['reply_hex'],'0203000100') if mode=='end' else transition['reply_hex'] is None,'end/expiry transition reply differs')
        require(a['holder'] is None and not a['owner_hex'] and not a['live'] and a['last']==7 and a['next_id']==2 and a['deadline']==1000 and a['generation']==b['generation']+1,'unowned discard seed lost release/identity/deadline')
        require(a['high']==(3 if mode=='end' else 2) and set(a['cache'])==({'1','2','3'} if mode=='end' else {'1','2'}),'end/expiry history seed differs')
        require(all(a['cache'][k]==v for k,v in b['cache'].items()),'end/expiry changed old historical replies')
        self.discard_inputs(mode+'-role',mode+'-short')
        clock=self.observe(mode+'-clock',bytes.fromhex('01ffff00000400000000'))
        expected=b'\2\xff\xff\1\0'+(17).to_bytes(4,'little')+(a['now_ms']*1000000).to_bytes(8,'little')
        require(answers(clock['reply_hex'],expected.hex()) and clock['before']==clock['after'],'unowned discard swallowed observer clock or mutated history')
        old=self.observe(mode+'-old-session',bytes.fromhex('0104000100100700000001'))
        x=old['after']
        require(answers(old['reply_hex'],'0204000007') and x['holder'] is None and not x['live'] and x['generation']==a['generation'] and x['next_id']==2 and x['deadline']==1000,'discard revived old session/resource or swallowed no_session')
    def controls(self):
        self.fresh();self.a.advance_to(500)
        short=self.observe('valid-header-short-payload',self.request[:10])
        require(answers(short['reply_hex'],'0203000003') and short['after']['deadline']==1500 and short['after']['live']=={'1':[1,1]} and short['after']['high']==3,'complete header with short payload must refuse,not disappear')
        complete=self.observe('valid-request',bytes.fromhex('0104000100100700000001'))
        require(answers(complete['reply_hex'],'02040001000200') and complete['after']['live']=={'1':[1,1],'2':[1,1]} and complete['after']['next_id']==3,'discard fixture swallowed valid requests')
    def expiry(self):
        self.fresh();self.a.advance_to(999)
        before=self.observe('before-expiry',b'\3'+self.request[1:])
        require(before['reply_hex'] is None and before['before']==before['after'] and before['after']['holder']==7,'discard caused early expiry or lease renewal')
        self.a.advance_to(1000)
        expired=self.observe('at-expiry',b'\xff'+self.request[1:])
        a=expired['after'];b=expired['before']
        require(expired['reply_hex'] is None and a['holder'] is None and not a['owner_hex'] and not a['live'] and a['deadline']==1000 and a['generation']==b['generation']+1,'discard prevented lease expiry or restored ownership')
        require(a['cache']==b['cache'] and a['high']==b['high']==2 and a['next_id']==b['next_id']==2 and a['last']==7,'expiry discarded history/reused resource IDs')
        short=self.observe('short-after-expiry',self.request[:4])
        require(short['reply_hex'] is None and short['before']==short['after'],'short header changed expired state')
        old=self.observe('old-session-after-expiry',self.request)
        require(answers(old['reply_hex'],'0203000007') and old['after']['holder'] is None and not old['after']['live'] and old['after']['deadline']==1000,'valid old-session request revived expired ownership')
    def run(self):
        self.check('CONTEXT',self.context)
        if self.checks[-1]['status']=='passed':
            for name,fn in (('DISCARD',self.discard),('CONTROLS',self.controls),('EXPIRY',self.expiry),
                            ('ENDED',lambda:self.unowned('end')),('EXPIRED',lambda:self.unowned('expired'))):self.check(name,fn)
        return dict(status='passed' if all(c['status']=='passed' for c in self.checks) else 'failed',checks=self.checks,observations=self.observations,
                    spec=self.identity,full_conformance=False,implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['physical framing/empty transport frame handling','all extension resource/dependency types','production Host role and malformed-frame recovery integration'])
