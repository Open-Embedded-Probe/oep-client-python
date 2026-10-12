import pytest
from oep_client.conformance_refusal_order import RefusalOrderChecks
from oep_client.conformance_refusal_order_sample import SampleRefusalOrderAdapter

SPEC='/explicit/refusal-order-spec'
@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_refusal_order.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)

def test_refusal_precedence_matrix():
    r=RefusalOrderChecks(SampleRefusalOrderAdapter(),spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==11 and len(r['observations'])==60

@pytest.mark.parametrize('case,wrong',[
    ('ZERO',1),('FN',2),('OP',3),('SID0',11),('REPLAY',7),('MISMATCH',7),
    ('HOLE',3),('FOREIGN',3),('OWN-BAD',2),('OTHER-ANY',8)])
def test_wrong_priority_reason_is_detected(case,wrong):
    a=SampleRefusalOrderAdapter();prepare=a.prepare;raw=a.raw
    def prepared(ctx):
        a.raw=raw;s=prepare(ctx)
        def broken(data,admission=None):
            value=raw(data,admission)
            return value[:3]+bytes((0,wrong))
        a.raw=broken;return s
    a.prepare=prepared;c=RefusalOrderChecks(a,spec=SPEC);c.check('FAULT',lambda:c.case(case))
    assert c.checks[-1]['status']=='failed' and c.checks[-1]['error'].startswith('Violation:'),c.checks

@pytest.mark.parametrize('fault,case',[
    ('renew_replay','REPLAY'),('early_cache','FN'),('mutate_resources','OWN-BAD'),
    ('reset_ids','OTHER-ANY'),('lose_owner','FOREIGN'),('no_history','OWN-BAD'),
    ('wrong_tag','OWN-BAD'),('false_retention','REPLAY'),
    ('corrupt_history','OWN-BAD'),('wrong_new_history','OWN-BAD')])
def test_refusal_state_or_retention_faults(fault,case):
    a=SampleRefusalOrderAdapter();prepare=a.prepare;raw=a.raw
    def prepared(ctx):
        a.raw=raw;s=prepare(ctx)
        if fault=='false_retention' and ctx['retention']=='request-lost':s['stored_request_hex']=s['cached_request_hex']
        def broken(data,admission=None):
            value=raw(data,admission)
            if fault=='renew_replay':a.core.deadline+=1
            if fault=='early_cache':a.core.cache[100]=(data,value)
            if fault=='mutate_resources':a.model.extension.live.clear()
            if fault=='reset_ids':a.model.extension.next_id=1
            if fault=='lose_owner':a.core.owner=b''
            if fault=='no_history':a.core.cache.pop(100,None)
            if fault=='wrong_tag':value=value[:5]+b'\0'
            if fault=='corrupt_history':a.core.cache[3]=(b'changed',None)
            if fault=='wrong_new_history':a.core.cache[100]=(data,None)
            return value
        a.raw=broken;return s
    a.prepare=prepared;c=RefusalOrderChecks(a,spec=SPEC);c.check('FAULT',lambda:c.case(case))
    assert c.checks[-1]['status']=='failed' and c.checks[-1]['error'].startswith('Violation:'),c.checks

@pytest.mark.parametrize('reason',[3,6,11])
def test_other_stage_does_not_impose_extra_priority(reason):
    a=SampleRefusalOrderAdapter();prepare=a.prepare;raw=a.raw
    def prepared(ctx):
        a.raw=raw;s=prepare(ctx)
        # All three reasons apply: invalid kind, critical unsupported tail,
        # and explicitly injected admission failure. Different conforming
        # choices must preserve session/history/resource semantics.
        a.raw=lambda data,admission=None:raw(data,None if reason==11 else reason)
        return s
    a.prepare=prepared;c=RefusalOrderChecks(a,spec=SPEC);c.check('CHOICE',lambda:c.case('OTHER-ANY'))
    assert c.checks[-1]['status']=='passed',c.checks

@pytest.mark.parametrize('fault,case',[
    ('replay_before_header','FN'),('session_before_replay','REPLAY'),
    ('lost_before_equality','MISMATCH'),('format_before_session','FOREIGN')])
def test_actual_ordering_faults(monkeypatch,fault,case):
    a=SampleRefusalOrderAdapter();prepare=a.prepare
    def prepared(ctx):
        s=prepare(ctx);core=a.core;handle=core.handle
        def broken(data,*args,**kwargs):
            corr=int.from_bytes(data[1:3],'little');sid=int.from_bytes(data[6:10],'little')
            if fault=='replay_before_header' and sid==core.last and corr in core.cache:
                return b'\2'+data[1:3]+b'\0\3'
            if fault=='session_before_replay' and sid and core.holder is None:
                return b'\2'+data[1:3]+b'\0\7'
            if fault=='lost_before_equality' and ctx['retention']=='response-lost':
                return b'\2'+data[1:3]+b'\0\14'
            if fault=='format_before_session' and data[10:11]==b'\3':
                return b'\2'+data[1:3]+b'\0\3'
            return handle(data,*args,**kwargs)
        monkeypatch.setattr(core,'handle',broken);return s
    a.prepare=prepared;c=RefusalOrderChecks(a,spec=SPEC);c.check('FAULT',lambda:c.case(case))
    assert c.checks[-1]['status']=='failed',c.checks

@pytest.mark.parametrize('missing',['prepare','raw','state','source_sha256'])
def test_missing_priority_controls(missing):
    a=SampleRefusalOrderAdapter();setattr(a,missing,None)
    assert RefusalOrderChecks(a,spec=SPEC).run()['checks'][0]['status']=='failed'
