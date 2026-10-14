import pytest
from oep_client import conformance_host_core as hc
from oep_client.conformance_core_fixed_fields import CoreFixedFieldChecks,CORE_SHAPES
from oep_client.conformance_host_core_sample import decode
from test_conformance_registered_ops import setup


@pytest.fixture(autouse=True)
def explicit_unit_spec(monkeypatch):
    monkeypatch.setattr(hc,'reviewed_identity',lambda path:dict(commit='unit-fixture',reviewed_core_sha256=hc.REVIEWED_CORE_SHA256))


def checker(reader=decode,factory=None):
    return CoreFixedFieldChecks(reader,factory or (lambda:setup()[1]),spec='/explicit/unit-fixture')


def test_removed_confirm_flags_are_not_reviewed_shape(monkeypatch):
    # a09fa45 removed confirm flags(u8): a 17-byte confirm inventory no longer matches the reviewed core.
    from oep_client import conformance_core_fixed_fields as ff
    old=tuple(('confirm',1,req,reply[:2]+(('flags',1),)+reply[2:]) if name=='confirm' else (name,op,req,reply) for name,op,req,reply in CORE_SHAPES)
    monkeypatch.setattr(ff,'CORE_SHAPES',old)
    with pytest.raises(Exception,match='prefixes differ'):checker().run()


def test_review_and_fixed_fields():
    r=checker().run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==5 and len(r['inventory'])==8 and not r['full_conformance']


@pytest.mark.parametrize('op',[1,2,3,4,16,19])
def test_omitted_reply_field_acceptance_is_detected(op):
    def bad(frame,operation,**kwargs):
        try:return decode(frame,operation,**kwargs)
        except ValueError:
            if op==operation:return dict(corr=7,successful=True,fixed={},tlvs=[])
            raise
    r=checker(bad).run();row=r['checks'][2]
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('op',[1,2,3,16])
def test_incomplete_request_acceptance_is_detected(op):
    def factory():
        model,c=setup();original=c.send
        def bad(req):
            out=original(req)
            if req[3:6]==bytes((0,0,op)) and out[3:5]==b'\0\3':out=out[:3]+b'\1\0'
            return out
        c.send=bad;return c
    r=checker(factory=factory).run();row=r['checks'][1]
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault',['omit_unlocked_remaining','missing_owner_rejected','zero_unsupported_omitted','empty_describe_omitted','zero_clock_omitted'])
def test_default_and_optional_distinctions(fault):
    def bad(frame,op,**kwargs):
        out=decode(frame,op,**kwargs)
        if fault=='omit_unlocked_remaining' and op==19 and out['fixed']==dict(locked=False,remaining_ms=0):out['fixed'].pop('remaining_ms')
        if fault=='zero_unsupported_omitted' and frame[3:]==b'\0\x0b\0':out['fixed']={}
        if fault=='empty_describe_omitted' and op==3 and frame[5:]==b'\0':out['fixed']={}
        if fault=='zero_clock_omitted' and op==4 and frame[5:]==b'\0'*12:out['fixed']={}
        if fault=='missing_owner_rejected' and (frame[3:5]==b'\0\10' or op==19) and not out['tlvs']:raise ValueError('mandatory owner')
        return out
    r=checker(bad).run();case=4 if fault=='missing_owner_rejected' else 3
    assert r['checks'][case]['status']=='failed',r['checks'][case]
