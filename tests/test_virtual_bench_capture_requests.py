"""Capture §3.2/3.3 request rejection and query preservation, through wire bodies.

This exercises the existing virtual endpoint's capture interface. It does not
claim that its legacy core is the current independent core conformity fixture.
"""
import copy
import struct
import pytest
from oep_client import capture as c,core,host,message as m
from test_virtual_bench_capture import bench,_configure_body


def configured(analog=True):
    ep,hst,logic,clock=bench();track=c.AnalogCapture(hst) if analog else logic
    channels=(16,17) if analog else (20,21)
    core.plan_apply(hst,[(track.fn,k,ch) for k,ch in enumerate(channels)])
    track.configure(rate=10000 if analog else 1000000,samples=8);track.start();clock.t=1;track.wait()
    return ep,hst,track,clock


def snapshot(ep,fn):
    return copy.deepcopy((ep.captures[fn],ep.plan,ep.gpio_modes))


def rejected(ep,hst,track,op,body,exception,tag=None,cause=None):
    before=snapshot(ep,track.fn)
    with pytest.raises(exception) as err:hst.request(track.fn,op,body,locked=op==track.CONFIGURE)
    if exception is host.Rejected:assert err.value.result.detail==m.MALFORMED
    if tag is not None:assert err.value.result.payload==bytes((tag,))
    if cause is not None:assert err.value.cause==cause
    assert snapshot(ep,track.fn)==before,'rejection changed capture/data/plan/pins'


@pytest.mark.parametrize('query',[False,True])
@pytest.mark.parametrize('critical',[False,True])
@pytest.mark.parametrize('frontend',[0,1,2,3])
def test_requested_frontend_is_returned_per_role_and_query_preserves_capture(query,critical,frontend):
    ep,hst,track,_=configured();before=snapshot(ep,track.fn)
    body=_configure_body(rate=10000,samples=8)+m.tlv(c.FRONTEND,bytes((1,frontend)),critical=critical)
    op=track.QUERY_OP if query else track.CONFIGURE
    reply=hst.request(track.fn,op,body,locked=not query);cfg=c._config(reply.payload,True)
    assert cfg.frontend=={0:3,1:frontend}
    if query:assert snapshot(ep,track.fn)==before
    else:
        assert ep.captures[track.fn].state==c.STATE['configured']
        assert not ep.captures[track.fn].data and not ep.captures[track.fn].segs


@pytest.mark.parametrize('query',[False,True])
@pytest.mark.parametrize('critical',[False,True])
@pytest.mark.parametrize('value',[b'',b'\0',b'\0\0\0',b'\0\xff',b'\x02\0'])
def test_invalid_frontend_width_value_or_unplanned_role_is_atomic(query,critical,value):
    ep,hst,track,_=configured();op=track.QUERY_OP if query else track.CONFIGURE
    body=_configure_body(rate=10000)+m.tlv(c.FRONTEND,value,critical=critical)
    rejected(ep,hst,track,op,body,host.Rejected if len(value)!=2 else host.Unsupported,tag=(c.FRONTEND|(128 if critical else 0)) if len(value)==2 else None)


@pytest.mark.parametrize('query',[False,True])
@pytest.mark.parametrize('same_frontend',[False,True])
@pytest.mark.parametrize('critical',[False,True])
def test_two_frontends_for_the_same_role_are_malformed_even_if_identical(query,same_frontend,critical):
    ep,hst,track,_=configured();body=_configure_body(rate=10000)+m.tlv(c.FRONTEND,b'\0\0')+m.tlv(c.FRONTEND,bytes((0,0 if same_frontend else 1)),critical=critical)
    rejected(ep,hst,track,track.QUERY_OP if query else track.CONFIGURE,body,host.Rejected)


@pytest.mark.parametrize('query',[False,True])
@pytest.mark.parametrize('name,value,exception,tag',[
 ('rate',b'\0'*4,host.Rejected,None),('rate',struct.pack('<I',610),host.Unsupported,c.RATE),
 ('samples',b'\0'*4,host.Rejected,None),('mode',b'\x09',host.Unsupported,c.MODE),
 ('trigger',struct.pack('<BBI',1,0,1),host.Unsupported,c.TRIGGER),
 ('trigger',struct.pack('<BBI',3,2,2048),host.Unavailable,None),
])
def test_analog_request_values_preserve_completed_data(query,name,value,exception,tag):
    ep,hst,track,_=configured();tags={'rate':c.RATE,'samples':c.SAMPLES,'mode':c.MODE,'trigger':c.TRIGGER}
    body=b''.join(m.tlv(t,v) for t,v in m.split_tlvs(_configure_body(rate=10000)) if t!=tags[name])+m.tlv(tags[name],value)
    rejected(ep,hst,track,track.QUERY_OP if query else track.CONFIGURE,body,exception,tag=tag,cause='wrong_state' if exception is host.Unavailable else None)


@pytest.mark.parametrize('analog',[False,True])
def test_paused_capture_rejects_configure_and_keeps_queued_segments(analog):
    ep,hst,track,clock=configured(analog)
    track.configure(rate=10000 if analog else 1000000,mode=c.REPEAT,samples=8,segments=1);track.start();clock.t=50
    assert track.status().state==c.STATE['paused']
    assert ep.captures[track.fn].segs and ep.captures[track.fn].data
    rejected(ep,hst,track,track.CONFIGURE,_configure_body(rate=10000 if analog else 1000000,samples=8),host.Unavailable,cause='wrong_state')
