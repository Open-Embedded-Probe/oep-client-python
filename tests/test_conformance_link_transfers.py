import pytest

from oep_client.conformance import Checks
from oep_client.conformance_link_transfers import LinkTransferChecks
from oep_client.conformance_link_transfer_sample import SampleLinkTransferAdapter
from oep_client.virtual_link_transfer_model import LinkTransferModel
from test_conformance_registered_ops import setup as registered_setup


def setup(frame=256):
    _,c=registered_setup()
    model=LinkTransferModel(lambda:0,boot_id=17,max_frame=frame)
    return model,Checks(model.handle,c.reg,'virtual-resource-1')


@pytest.mark.parametrize('frame',[64,256,1024])
def test_link_transfer_profiles(frame):
    model,c=setup(frame);r=LinkTransferChecks(c,SampleLinkTransferAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==10 and not r['full_conformance']
    assert all(len(bytes.fromhex(x['request_hex']))<=frame for x in c.trace)


@pytest.mark.parametrize('frame',[64,256,1024])
def test_common_interface_declarations(frame):
    from oep_client.conformance_interfaces import InterfaceChecks
    model,c=setup(frame);r=InterfaceChecks(c).run()
    assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('frame',[64,256,1024])
@pytest.mark.parametrize('case',LinkTransferChecks.cases)
def test_wrong_wire_results_are_detected(frame,case):
    model,c=setup(frame);original=model.handle
    def broken(request):
        reply=original(request)
        if int.from_bytes(request[3:5],'little')==4:
            reply=reply[:3]+(b'\1\0' if reply[3]==0 else b'\1\1')+reply[5:]
        return reply
    c.send=broken;r=LinkTransferChecks(c,SampleLinkTransferAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-LINK-TRANSFER-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('defect,case',[
    ('source_plus_one','SOURCE'),('pattern_shift','SOURCE'),('pattern_no_wrap','SOURCE'),
    ('source_empty_nonzero','SOURCE'),('source_wrong_count','SOURCE'),
    ('sink_nonempty','SINK'),('source_mutates_pin','SOURCE'),('sink_mutates_plan','SINK'),
    ('short_source_accepted','MALFORMED'),('short_sink_accepted','MALFORMED'),
    ('unknown_critical_ignored','TLV'),('broken_tail_ignored','TLV'),
    ('unsupported_tag_masked','TLV'),('wrong_session_accepted','SESSION'),
    ('expired_session_accepted','SESSION'),
    ('replay_payload_changes','REPLAY'),('changed_replay_accepted','REPLAY'),
    ('port_accepted','PORT-ABSENT'),
])
def test_semantic_wire_and_state_faults(defect,case):
    model,c=setup(1024);original=model.handle;seen={}
    def broken(request):
        reply=original(request)
        if int.from_bytes(request[3:5],'little')!=4:return reply
        op=request[5];success=reply[3:5]==b'\1\0'
        if op==16 and success:
            n=int.from_bytes(reply[5:7],'little')
            if defect=='source_plus_one':
                n+=1;reply=reply[:5]+n.to_bytes(2,'little')+bytes(i&255 for i in range(n))
            if defect=='pattern_shift':reply=reply[:7]+bytes((v+1)&255 for v in reply[7:])
            if defect=='pattern_no_wrap' and n>256:reply=reply[:7]+bytes(min(i,255) for i in range(n))
            if defect=='source_empty_nonzero' and n==0:reply=reply[:5]+b'\1\0\0'
            if defect=='source_wrong_count':reply=reply[:5]+b'\0\0'+reply[7:]
            if defect=='source_mutates_pin':model.extension.pins[1]=(3,0)
        if op==17 and success:
            if defect=='sink_nonempty':reply+=b'x'
            if defect=='sink_mutates_plan':model.extension.plans.clear()
        if defect in ('short_source_accepted','short_sink_accepted') and not success:
            if (op==16 and defect=='short_source_accepted') or (op==17 and defect=='short_sink_accepted'):
                reply=reply[:3]+b'\1\0'+(b'\0\0' if op==16 else b'')
        if defect=='unknown_critical_ignored' and reply[3:5]==b'\0\x0b':reply=reply[:3]+b'\1\0'
        if defect=='broken_tail_ignored' and reply[3:5]==b'\0\3':reply=reply[:3]+b'\1\0'
        if defect=='unsupported_tag_masked' and reply[3:5]==b'\0\x0b':reply=reply[:5]+bytes((reply[5]&127,))+reply[6:]
        if defect=='wrong_session_accepted' and reply[3:5]==b'\0\x08':reply=reply[:3]+b'\1\0'
        if defect=='expired_session_accepted' and reply[3:5]==b'\0\x07':reply=reply[:3]+b'\1\0'
        if defect=='replay_payload_changes':
            if request in seen and op==16 and success:reply=reply[:7]+b'\xff'*int.from_bytes(reply[5:7],'little')
            seen[request]=True
        if defect=='changed_replay_accepted' and reply[3:5]==b'\0\3' and request.endswith(b'\x7f\0\0'):reply=reply[:3]+b'\1\0'
        if defect=='port_accepted' and op==18:reply=reply[:3]+b'\1\0'+b'\0'*4
        return reply
    c.send=broken;r=LinkTransferChecks(c,SampleLinkTransferAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-LINK-TRANSFER-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


def test_live_seed_is_required():
    model,c=setup();a=SampleLinkTransferAdapter(model);a.seed=lambda c,sid:None
    r=LinkTransferChecks(c,a).run()
    assert all(x['status']=='failed' and 'seed missing' in x['error'] for x in r['checks'][3:])


@pytest.mark.parametrize('missing',['state','seed','restart_fixture'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleLinkTransferAdapter(model);setattr(a,missing,None)
    r=LinkTransferChecks(c,a).run()
    assert r['checks'][2]['status']=='failed'


def test_insufficient_replay_retention_is_a_precondition_failure():
    model,c=setup();model.ep.remember_max=16
    r=LinkTransferChecks(c,SampleLinkTransferAdapter(model)).run()
    assert r['checks'][2]['status']=='failed' and 'retention' in r['checks'][2]['error']
