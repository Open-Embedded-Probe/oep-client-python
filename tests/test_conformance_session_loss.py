import pytest
from oep_client.conformance_session_loss import SessionLossChecks
from oep_client.conformance_session_loss_sample import SampleSessionLossAdapter, SampleSessionLossHost
from test_conformance_recovery import setup as base


def setup():
    now,model,_,runner=base()
    adapter=SampleSessionLossAdapter(model,clock_ms=lambda:now[0],wait_ms=lambda ms:now.__setitem__(0,now[0]+ms))
    runner.c.send=lambda req:adapter.exchange(adapter.primary,req)
    return now,model,adapter,SessionLossChecks(runner.c,adapter)


def test_session_loss_passes():
    _,model,_,runner=setup()
    report=runner.run()
    assert report['status']=='passed',[(row['id'],row.get('error')) for row in report['checks']]
    assert len(report['checks'])==21 and len(report['session_loss_decisions'])==9
    assert not report['full_conformance'] and model.ep.current_core.holder is None


@pytest.mark.parametrize('defect',['new_corr','infer_success','invent_lease','ignore_expiry','ignore_boot','ignore_barrier'])
def test_session_loss_host_mutants_fail(defect):
    _,_,_,runner=setup()
    class Broken(SampleSessionLossHost):
        def recover_session(self,request,boot,barrier):
            if defect=='new_corr':
                import struct
                corr=struct.unpack_from('<H',request,1)[0]
                request=request[:1]+struct.pack('<H',corr+1 if corr<65535 else 1)+request[3:]
            if defect=='ignore_barrier':barrier=lambda:True
            result=super().recover_session(request,boot,barrier)
            if defect=='infer_success' and result['original_outcome']=='unknown':result['original_outcome']='success'
            if defect=='invent_lease' and result['status']=='open-active' and result['lease_ms'] is None:result['lease_ms']=60000
            if defect=='ignore_expiry' and result['status']=='session-unavailable':result['status']='open-active'
            if defect=='ignore_boot' and result['status']=='boot-changed':result['status']='open-active'
            return result
    runner.make_host=Broken
    report=runner.run()
    assert report['status']=='failed'
    assert any(row['id'].startswith('CORE-SESSION-LOSS-') and row['status']=='failed' for row in report['checks'])


@pytest.mark.parametrize('invalid',['discard_request','discard_reply','restart','pending','receive'])
def test_missing_explicit_controls_blocked(invalid):
    _,_,adapter,runner=setup();setattr(adapter,invalid,None)
    report=runner.run()
    assert report['status']=='failed'
    assert not report['session_loss_decisions']


@pytest.mark.parametrize('invalid',['corr','sid','operation','counter','callback','force','tlv'])
def test_invalid_recovery_input_has_no_wire_effect(invalid):
    import struct
    from oep_client.conformance import Violation
    _,_,adapter,runner=setup();c=runner.c
    c.confirm();c.identity();c.corr=1
    host=SampleSessionLossHost(c,adapter.functions)
    request=c.request(c.core['open'],struct.pack('<IB',60000,0),session=123,corr=1)
    if invalid=='corr':request=request[:1]+b'\x02\0'+request[3:]
    if invalid=='sid':request=request[:6]+b'\0'*4+request[10:]
    if invalid=='operation':request=request[:5]+bytes([c.core['keepalive']])+request[6:]
    if invalid=='counter':c.corr=2
    if invalid=='force':request=request[:14]+b'\x01'
    if invalid=='tlv':request+=b'\x40'
    first=len(c.trace)
    with pytest.raises(Violation):host.recover_session(request,c.boot,None if invalid=='callback' else lambda:True)
    assert len(c.trace)==first


def test_boot_change_after_open_liveness_discards_adoption():
    import struct
    _,model,adapter,runner=setup();c=runner.c
    c.confirm();c.identity();boot=c.boot;c.corr=1
    request=c.request(c.core['open'],struct.pack('<IB',60000,0),session=123,corr=1)
    adapter.discard_reply(request);adapter.wait_ms(1050)
    host=SampleSessionLossHost(c,adapter.functions);live=host.live_session
    def raced(sid,expected):
        result=live(sid,expected);adapter.restart(boot^1);return result
    host.live_session=raced
    assert host.recover_session(request,boot,lambda:True)=={'status':'boot-changed','original_outcome':'unknown','lease_ms':None}
    assert model.ep.current_core.holder is None


def test_replay_does_not_reacquire_session_forced_to_another_owner():
    import struct
    from oep_client.conformance import Checks
    _,model,adapter,runner=setup();c=runner.c
    c.confirm();c.identity();boot=c.boot;c.corr=1
    request=c.request(c.core['open'],struct.pack('<IB',60000,0),session=123,corr=1)
    adapter.discard_reply(request);adapter.wait_ms(1050)
    peer=Checks(lambda req:adapter.exchange(adapter.peer,req),c.reg,'virtual-retention-1')
    peer.success(peer.request(peer.core['open'],struct.pack('<IB',60000,1),session=456))
    before=model.state()
    try:
        result=SampleSessionLossHost(c,adapter.functions).recover_session(request,boot,lambda:True)
        assert result=={'status':'session-unavailable','original_outcome':'unknown','lease_ms':None}
        assert model.state()==before
    finally:peer.success(peer.request(peer.core['end'],session=456))
