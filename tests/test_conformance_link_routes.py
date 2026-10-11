import pytest

from oep_client.conformance import Checks
from oep_client.conformance_link_routes import LinkRoutesChecks
from oep_client.conformance_link_routes_sample import SampleLinkRoutesAdapter
from oep_client.virtual_link_routes_model import LinkRoutesModel
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,c=registered_setup();model=LinkRoutesModel(lambda:0,boot_id=17)
    return model,Checks(lambda r:model.handle_route(r,2),c.reg,'virtual-resource-1')


def test_shared_routes_pass():
    model,c=setup();r=LinkRoutesChecks(c,SampleLinkRoutesAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==9 and not r['full_conformance']
    assert set(x['route'] for x in r['route_wire_trace'])=={0,1,2}
    assert any(x['route']==2 and x['epoch']>0 for x in r['route_wire_trace'])


@pytest.mark.parametrize('route',[0,1,2])
def test_common_declarations_per_route(route):
    from oep_client.conformance_interfaces import InterfaceChecks
    model,c=setup();c.send=lambda r:model.handle_route(r,route)
    r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',LinkRoutesChecks.cases)
def test_wrong_link_wire_result_is_detected(case):
    model,c=setup();original=model.handle_route
    def broken(request,route):
        reply=original(request,route)
        if int.from_bytes(request[3:5],'little')==4:reply=reply[:3]+b'\1\1'+reply[5:]
        return reply
    model.handle_route=broken;r=LinkRoutesChecks(c,SampleLinkRoutesAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-LINK-ROUTES-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('defect,case',[
    ('global_frame','CONTEXT'),('wrong_transport','CONTEXT'),('route_boot','CONTEXT'),
    ('route_list','CONTEXT'),('source_global_limit','ROUTE-LIMITS'),
    ('sink_mutates_pin','ROUTE-LIMITS'),('free_replay_cache','FREE-CACHE'),
    ('independent_lock','SHARED-LOCK'),('close_clears_session','RECONNECT'),
    ('close_clears_cache','REPLAY-RECONNECT'),('close_reboots','RECONNECT'),
    ('reopen_changes_plan','RECONNECT'),('replay_reexecutes','REPLAY-RECONNECT'),
    ('changed_replay_accepted','REPLAY-RECONNECT'),('end_lock_stays_on_peer','END-SHARED'),
])
def test_route_semantics_faults(defect,case):
    model,c=setup();original=model.handle_route;close=model.close_route;reopen=model.reopen_route;free_cache={};seen=set()
    def broken(request,route):
        reply=original(request,route);fn=int.from_bytes(request[3:5],'little');op=request[5]
        if fn==0 and op==1:
            if defect=='global_frame':reply=reply[:11]+b'\0\4'+reply[13:]
            if defect=='wrong_transport':reply=reply[:-1]+b'\0'
            if defect=='route_boot' and route==1:reply=reply[:18]+b'\xff'*4+reply[22:]
        if defect=='route_list' and fn==0 and op==2 and route==1 and b'oep.probe.link' in reply:
            reply=reply.replace(b'oep.probe.link',b'oep.probe.fake')
        if fn==4 and op==16 and reply[3:5]==b'\1\0':
            if defect=='source_global_limit':
                n=min(int.from_bytes(request[10:14],'little'),1017);reply=reply[:5]+n.to_bytes(2,'little')+bytes(i&255 for i in range(n))
            if defect=='free_replay_cache' and request[6:10]==b'\0'*4 and request[1:3]==(60000).to_bytes(2,'little'):
                reply=free_cache.setdefault(request,reply)
            if defect=='replay_reexecutes' and request in seen: model.extension.pins[1]=(3,0)
            seen.add(request)
        if defect=='sink_mutates_pin' and fn==4 and op==17:model.extension.pins[1]=(3,0)
        if defect=='independent_lock' and fn==0 and op==19 and route!=2:reply=reply[:5]+b'\0'*5
        if defect=='changed_replay_accepted' and fn==4 and reply[3:5]==b'\0\3' and request.endswith(b'\x7f\0\0'):reply=reply[:3]+b'\1\0'
        if defect=='end_lock_stays_on_peer' and fn==0 and op==19 and route!=2 and model.ep.current_core.holder is None:
            reply=reply[:5]+b'\1\xb8\x0b\0\0'
        return reply
    def bad_close(route):
        close(route)
        if defect=='close_clears_session':model.ep.current_core.release()
        if defect=='close_clears_cache':model.ep.current_core.cache.clear()
        if defect=='close_reboots':model.ep.boot_id+=1
    def bad_reopen(route):
        reopen(route)
        if defect=='reopen_changes_plan':model.extension.plans.clear()
    model.handle_route=broken;model.close_route=bad_close;model.reopen_route=bad_reopen
    r=LinkRoutesChecks(c,SampleLinkRoutesAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-LINK-ROUTES-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row
    if defect=='close_clears_session':assert 'close changed protected shared state' in row['error']


@pytest.mark.parametrize('missing',['select','close','reopen','trace'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleLinkRoutesAdapter(model);setattr(a,missing,None)
    r=LinkRoutesChecks(c,a).run()
    assert r['checks'][0]['status']=='failed'


def test_session_requests_keep_one_route_and_sid0_is_not_deduplicated():
    model,c=setup();r=LinkRoutesChecks(c,SampleLinkRoutesAdapter(model)).run()
    assert r['status']=='passed'
    trace=r['route_wire_trace'];owners=set()
    for row in trace:
        request=bytes.fromhex(row['request_hex']);reply=bytes.fromhex(row['reply_hex'])
        if request[3:6]==b'\0\0\x10' and reply[3:5]==b'\1\0':owners.add(request[6:10])
    for sid in owners:
        assert {row['route'] for row in trace if bytes.fromhex(row['request_hex'])[6:10]==sid}=={2}
    repeated=[row for row in trace if bytes.fromhex(row['request_hex'])[1:3]==(60000).to_bytes(2,'little')]
    assert {row['route'] for row in repeated}=={0,1,2} and len(repeated)==3
    assert len({row['request_hex'] for row in repeated})==1
    assert sorted(len(bytes.fromhex(row['reply_hex'])) for row in repeated)==[64,256,1024]
