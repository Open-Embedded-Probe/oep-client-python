"""Capture §1.2/3.1: analog layout and role-qualified payloads preserve sparse plan IDs."""
import struct
import pytest
from oep_client import capture as c,core,host,message as m
from oep_client.conformance import tlvs
from test_virtual_bench_capture import bench,_configure_body
from test_virtual_bench_capture_requests import snapshot


@pytest.mark.parametrize('roles',[(1,3),(0,3),(3,)])
@pytest.mark.parametrize('query',[False,True])
def test_sparse_plan_configure_and_query_keep_role_ids(roles,query):
    ep,hst,_,_=bench();track=c.AnalogCapture(hst)
    core.plan_apply(hst,[(track.fn,r,16+i) for i,r in enumerate(roles)])
    if query:
        track.configure(rate=1000,samples=3);track.start()
    before=snapshot(ep,track.fn)
    body=_configure_body(rate=1000,samples=3)+m.tlv(c.FRONTEND,bytes((roles[-1],0)))
    reply=hst.request(track.fn,track.QUERY_OP if query else track.CONFIGURE,body,locked=not query)
    cfg=c._config(reply.payload,True)
    assert cfg.order==list(roles) and set(cfg.zero)==set(cfg.scale_nv)==set(cfg.frontend)==set(roles)
    assert cfg.frontend[roles[-1]]==0
    assert cfg.skew_ns[roles[0]]==0
    if query:assert snapshot(ep,track.fn)==before
    else:
        track.config=cfg;track.start();segment,=track.segments();raw=track.read_segment(segment)
        # Independent known source vectors distinguish role 1 from role 3 after sample 0.
        expected={0:[4095,4095,4095],1:[2048,2248,2447],3:[2048,2148,2248]}
        for role in roles:assert track.values(raw,role)==expected[role]


@pytest.mark.parametrize('critical',[False,True])
def test_frontend_for_absent_ordinal_role_is_rejected_without_changes(critical):
    ep,hst,_,_=bench();track=c.AnalogCapture(hst)
    core.plan_apply(hst,[(track.fn,1,16),(track.fn,3,17)])
    before=snapshot(ep,track.fn)
    with pytest.raises(host.Unsupported) as error:
        hst.request(track.fn,track.CONFIGURE,_configure_body(rate=1000,samples=3)+m.tlv(c.FRONTEND,b'\0\0',critical=critical),locked=True)
    assert error.value.result.payload==bytes((c.FRONTEND|(128 if critical else 0),)) and snapshot(ep,track.fn)==before


@pytest.mark.parametrize('role',[1,3])
def test_trigger_role_is_planned_identity_not_dense_ordinal(role):
    ep,hst,_,_=bench();track=c.AnalogCapture(hst)
    core.plan_apply(hst,[(track.fn,1,16),(track.fn,3,17)])
    track.configure(rate=1000,samples=64,trigger=(c.CROSS_UP,role,2500))
    track.start();segment,=track.segments();assert segment.trigger_index is not None
    values=track.values(track.read_segment(segment),role);i=segment.trigger_index
    assert values[i-1]<2500<=values[i]


def test_unplanned_trigger_role_is_atomic_cause6():
    ep,hst,_,_=bench();track=c.AnalogCapture(hst)
    core.plan_apply(hst,[(track.fn,1,16),(track.fn,3,17)]);before=snapshot(ep,track.fn)
    with pytest.raises(host.Unavailable) as error:track.configure(rate=1000,samples=8,trigger=(c.CROSS_UP,0,2500))
    assert error.value.cause=='wrong_state' and snapshot(ep,track.fn)==before
