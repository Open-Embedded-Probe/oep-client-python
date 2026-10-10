import pytest
from oep_client.conformance_exhausted import ExhaustedChecks
from oep_client.conformance_exhausted_sample import SampleExhaustedHost
from test_conformance_recovery_races import setup as base


def setup(mode='none'):
    now,model,adapter,runner=base(mode)
    return now,model,adapter,ExhaustedChecks(runner.c,adapter)


@pytest.mark.parametrize('mode',['none','prefix','queued'])
def test_exhausted_recovery_passes(mode):
    _,model,_,runner=setup(mode);report=runner.run()
    assert report['status']=='passed',[(row['id'],row.get('error')) for row in report['checks']]
    assert len(report['checks'])==13 and not report['full_conformance']
    assert model.ep.current_core.holder is None
    rows=report['exhausted_observation']['records']
    assert [row['status'] for row in rows]==(['completed','unknown','unknown','completed'] if mode=='prefix' else ['unknown']*3+['completed'])


@pytest.mark.parametrize('defect',['ignore_barrier','reuse_sid','rewrite_unknown','erase_known','keep_binding','force'])
def test_exhausted_host_mutants_fail(defect):
    _,_,_,runner=setup('prefix')
    class Broken(SampleExhaustedHost):
        def choose_sid(self):
            if defect=='reuse_sid':return next(row['session_id'] for row in self.pending.snapshot() if row['session_id'])
            return super().choose_sid()
        def recover(self,sid,boot):
            if defect=='ignore_barrier':self.barrier=lambda:True
            if defect=='keep_binding':
                class Kept(dict):
                    def clear(self):pass
                self.bindings=Kept(self.bindings)
            if defect in ('rewrite_unknown','erase_known'):
                for row in self.pending.records:
                    if row['route']==self.old_route:row['status']='completed' if defect=='rewrite_unknown' else 'unknown'
            send=self.c.send
            if defect=='force':
                def force(request):
                    if request[5]==self.c.core['open']:request=request[:14]+b'\x01'+request[15:]
                    return send(request)
                self.c.send=force
            try:return super().recover(sid,boot)
            finally:self.c.send=send
    runner.make_host=Broken
    report=runner.run()
    assert report['status']=='failed'
    assert any(row['id'].startswith('CORE-EXHAUSTED-') and row['status']=='failed' for row in report['checks'])


def host_fixture():
    import struct
    from oep_client.conformance_recovery_race_sample import SamplePendingHost
    now,model,a,runner=setup();c,peer=runner.c,runner.peer
    c.confirm();c.identity();peer.confirm();peer.identity();boot=c.boot
    c.success(c.request(c.core['open'],struct.pack('<IB',3000,0),session=123,corr=1))
    limits={route:{key:checks.observed['confirm'][key] for key in ('max_frame','window','max_inflight')}
            for route,checks in ((a.primary,c),(a.peer,peer))}
    ledger=SamplePendingHost(c.reg,limits)
    ledger.track(a.primary,c.request(c.core['end'],session=123,corr=65535),boot)
    ledger.abandon(a.primary);a.close(a.primary)
    host=SampleExhaustedHost(peer,a.functions,ledger,a.primary,a.peer,lambda:True)
    return now,model,a,peer,ledger,host,boot


def test_boot_change_stops_before_new_open():
    _,model,a,peer,ledger,host,boot=host_fixture();before=ledger.snapshot()
    a.restart(boot^1)
    assert host.recover(123,boot)['status']=='boot-changed'
    assert ledger.snapshot()==before and model.ep.current_core.holder is None
    assert not any(bytes.fromhex(row['request_hex'])[5]==peer.core['open'] for row in peer.trace)


def test_another_holder_is_not_forced_or_adopted():
    import struct
    _,model,a,peer,ledger,host,boot=host_fixture()
    peer.success(peer.request(peer.core['open'],struct.pack('<IB',60000,1),session=456))
    before=model.state();records=ledger.snapshot();first=len(peer.trace)
    try:
        assert host.recover(123,boot)['status']=='lock-held'
        assert model.state()==before and ledger.snapshot()==records and peer.session is None
        assert all(int.from_bytes(bytes.fromhex(row['request_hex'])[6:10],'little')==0 for row in peer.trace[first:])
    finally:peer.success(peer.request(peer.core['end'],session=456))


@pytest.mark.parametrize('sid',[0,-1,True,0x100000000,123])
def test_invalid_replacement_never_opens(sid):
    from oep_client.conformance import Violation
    now,model,a,peer,ledger,host,boot=host_fixture();now[0]+=3050;a.service()
    host.choose_sid=lambda:sid;before=ledger.snapshot();state=model.state();first=len(peer.trace)
    with pytest.raises(Violation):host.recover(123,boot)
    assert ledger.snapshot()==before and model.state()==state
    assert not any(bytes.fromhex(row['request_hex'])[5]==peer.core['open'] for row in peer.trace[first:])


def test_lost_new_open_quarantines_without_adoption_or_retry():
    now,model,a,peer,ledger,host,boot=host_fixture();now[0]+=3050;a.service()
    send=peer.send
    def lost(request):
        response=send(request)
        if request[5]==peer.core['open']:raise TimeoutError('logical fresh open reply loss')
        return response
    peer.send=lost
    with pytest.raises(TimeoutError):host.recover(123,boot)
    assert peer.session is None and a.peer in ledger.quarantined
    assert [row['status'] for row in ledger.snapshot()]==['unknown','unknown']
    first=len(peer.trace)
    from oep_client.conformance import Violation
    with pytest.raises(Violation):host.recover(123,boot)
    assert len(peer.trace)==first
    now[0]+=60050;a.service()  # explicit fixture expiry, no recovery host retry/force
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('invalid',['close','pending','wait_ms','mode'])
def test_missing_controls_block_recovery(invalid):
    _,_,a,runner=setup()
    if invalid=='mode':a.batch_mode='invalid'
    else:setattr(a,invalid,None)
    report=runner.run()
    assert report['status']=='failed' and report['exhausted_observation'] is None


def test_boot_change_during_inventory_discards_readback():
    now,model,a,peer,ledger,host,boot=host_fixture();now[0]+=3050;a.service()
    inventory=host.inventory;before=ledger.snapshot()
    def raced():
        value=inventory();a.restart(boot^1);return value
    host.inventory=raced
    assert host.recover(123,boot)['status']=='boot-changed'
    assert ledger.snapshot()==before and model.ep.current_core.holder is None
