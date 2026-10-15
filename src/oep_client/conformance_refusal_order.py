"""Header -> replay -> session -> unordered remaining rejection conditions."""
from .conformance import require, tlvs
from .conformance_registered_ops import fixed_then_tlvs
from .conformance_host_deadline import reviewed_identity


def request(corr,fn,op,sid,payload=b''):
    return b'\1'+corr.to_bytes(2,'little')+fn.to_bytes(2,'little')+bytes((op,))+sid.to_bytes(4,'little')+payload


class RefusalOrderChecks:
    cases=('ZERO','FN','OP','SID0','REPLAY','MISMATCH','HOLE','FOREIGN','OWN-BAD','OTHER-ANY')
    def __init__(self,adapter,*,spec):self.a=adapter;self.identity=reviewed_identity(spec);self.checks=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='REFUSAL-ORDER-'+name,level='core',clause='core §4.3/5.2/6.1/6.2')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.checks.append(row)
    def controls(self):
        for name in ('prepare','raw','state','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit replay-limit/session/refusal controls required')
    def case(self,name):
        for retention in ('normal','request-lost','response-lost'):
            for expired in (False,True):
                ctx=dict(retention=retention,expired=expired);setup=self.a.prepare(ctx);before=self.a.state()
                original=bytes.fromhex(setup['cached_request_hex']);corr=int.from_bytes(original[1:3],'little')
                require(setup['context']==ctx and setup['state']==before and before['last']==7 and before['next_id']==2,'priority seed/context differs')
                require(before['holder']==(None if expired else 7) and before['live']==({} if expired else {'1':[1,1]}) and before['deadline']==1000,'priority lease/resource seed differs')
                if retention=='normal':require(setup['stored_request_hex']==original.hex() and setup['stored_reply_hex']==setup['original_reply_hex'],'normal replay seed lost bytes')
                elif retention=='request-lost':require(setup['stored_request_hex'] is None and setup['stored_reply_hex'] is not None,'request retention limit not exercised')
                else:require(setup['stored_request_hex']==original.hex() and setup['stored_reply_hex'] is None,'response retention limit not exercised')
                admission=None;payload=b'\3\xfe\1\0x'
                if name=='ZERO':data=request(0,65535,255,0,payload);reasons={3}
                elif name=='FN':data=request(corr,65535,255,7,payload);reasons={1}
                elif name=='OP':data=request(corr,1,255,7,payload);reasons={2}
                elif name=='SID0':data=request(corr,1,16,0,payload);reasons={9}
                elif name=='REPLAY':data=original;reasons=None if retention=='normal' else {12}
                elif name=='MISMATCH':
                    data=original[:10]+(b'OEP?\2\2' if retention=='response-lost' else b'\2')
                    reasons={12 if retention=='request-lost' else 3}
                elif name=='HOLE':data=request(2,1,16,7,payload);reasons={12}
                elif name=='FOREIGN':data=request(100,1,16,8,payload);reasons={7 if expired else 8}
                elif name=='OWN-BAD':data=request(100,1,16,7,payload);reasons={7} if expired else {3,11}
                else:
                    data=request(100,1,16,7,payload);admission=6;reasons={7} if expired else {3,6,11}
                reply=self.a.raw(data,admission);after=self.a.state()
                self.observations.append(dict(case=name,context=ctx,setup=setup,input_hex=data.hex(),reply_hex=reply.hex() if reply is not None else None,
                                              allowed_reasons=sorted(reasons) if reasons else None,admission_reason=admission,before=before,after=after))
                require(reply is not None and reply[:3]==b'\2'+data[1:3],'priority response role/corr differs')
                if reasons is None:require(reply.hex()==setup['original_reply_hex'],'replay lost original success result')
                else:
                    require(reply[3]==0 and reply[4] in reasons,'wrong refusal priority')
                    reason=reply[4]
                    # locked: remaining(u32) then TLVs with the owner; unsupported: the tag, then TLVs; the rest: TLVs
                    # only. More TLVs, in any order, may follow each fixed part (core §2.3, §4.3).
                    fixed=(1000-before['now_ms']).to_bytes(4,'little') if reason==8 else b'\xfe' if reason==11 else b''
                    require(fixed_then_tlvs(reply[5:],fixed) and (reason!=8 or dict(reversed(tlvs(reply[9:]))).get(1)==b'priority'),
                            'refusal payload/tag/remaining owner differs')
                if name in ('OWN-BAD','OTHER-ANY'):
                    for field in ('holder','last','owner_hex','lease_ms','generation','next_id','live'):
                        require(after[field]==before[field],'payload/state refusal changed extension/owner: '+field)
                    require(after['deadline']==(1000 if expired else 1400) and after['high']==100 and set(after['cache'])==set(before['cache'])|{'100'},'new last-session refusal did not update lease/history correctly')
                    require(all(after['cache'][key]==value for key,value in before['cache'].items()),'new refusal corrupted retained history')
                    cap={'normal':2048,'request-lost':10,'response-lost':16}[retention]
                    expected=dict(request_hex=data.hex() if len(data)<=cap else None,reply_hex=reply.hex() if len(reply)<=cap else None)
                    require(after['cache']['100']==expected,'new refusal history does not retain expected request/result')
                else:require(after==before,'early refusal or replay changed state/lease/history')
    def run(self):
        self.check('CONTEXT',self.controls)
        if self.checks[-1]['status']=='passed':
            for name in self.cases:self.check(name,lambda name=name:self.case(name))
        return dict(status='passed' if all(c['status']=='passed' for c in self.checks) else 'failed',checks=self.checks,observations=self.observations,
                    spec=self.identity,full_conformance=False,implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['all extension payload/resource-state combinations','physical admission capacity and transport timing','production Host integration'])
