import pytest
from oep_client.conformance_recovery_races import RecoveryRaceChecks
from oep_client.conformance_recovery_race_sample import SampleRecoveryRaceAdapter, SamplePendingHost
from test_conformance_recovery import setup as base


def setup(mode='none'):
    now,model,_,runner = base()
    adapter = SampleRecoveryRaceAdapter(model,batch_mode=mode,clock_ms=lambda:now[0],wait_ms=lambda ms:now.__setitem__(0,now[0]+ms))
    runner.c.send = lambda request:adapter.exchange(adapter.primary,request)
    return now,model,adapter,RecoveryRaceChecks(runner.c,adapter)


@pytest.mark.parametrize('mode',['none','prefix','queued'])
def test_races_pass(mode):
    _,model,_,runner = setup(mode)
    report = runner.run()
    assert report['status']=='passed',[(x['id'],x.get('error')) for x in report['checks']]
    assert len(report['checks'])==27
    assert len(report['recovery_decisions'])==13
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None
    records = report['batch_observation']['records']
    assert [x['status'] for x in records]==(['completed','unknown','unknown','completed'] if mode=='prefix' else ['unknown','unknown','unknown','completed'])


@pytest.mark.parametrize('defect',['skip_final_check','ignore_locked','choose_T'])
def test_force_host_mutants_fail(defect):
    _,_,_,runner=setup()
    live = runner.host.live_session
    calls=[0]
    def altered(sid,boot):
        calls[0]+=1
        if defect=='skip_final_check' and calls[0]%2==0:return 'live'
        result=live(sid,boot)
        if defect=='ignore_locked' and result=='session-unavailable' and runner.peer.session is not None:return 'live'
        return result
    if defect in ('skip_final_check','ignore_locked'): runner.host.live_session=altered
    else:
        recover=runner.host.recover
        def chosen(*args,**kwargs):
            result=recover(*args,**kwargs)
            if result['status']=='session-unavailable' and runner.state()['holder'] is not None:
                result.update(status='observed-added',inventory=runner.state()['resources'])
            return result
        runner.host.recover=chosen
    report=runner.run()
    assert report['status']=='failed'
    assert any(x['id'].startswith('CORE-RECOVERY-RACE-') and x['status']=='failed' for x in report['checks']),report


@pytest.mark.parametrize('defect',['only_last','erase_known','all_routes','adopt_late'])
def test_abandonment_mutants_fail(defect):
    _,_,_,runner=setup('prefix' if defect=='erase_known' else 'none')
    class Broken(SamplePendingHost):
        def abandon(self,route):
            pre=self.snapshot(); result=super().abandon(route)
            if defect=='only_last':
                for row,old in zip(self.records,pre):
                    if row['route']==route and old['status']=='pending' and row is not self.records[-2]:row.update(old)
                result=result[-1:]
            if defect=='erase_known':
                for row in self.records:
                    if row['route']==route:row.update(status='unknown',response=None)
            if defect=='all_routes':
                for row in self.records:
                    if row['status']=='pending':row.update(status='unknown',response=None)
            return result
        def accept(self,route,epoch,frame):
            if defect=='adopt_late' and route in self.quarantined:
                self.quarantined.remove(route);self.epochs[route]=epoch
                for row in self.records:
                    if row['route']==route and row['status']=='unknown':row['status']='pending'
            return super().accept(route,epoch,frame)
    runner.make_pending_host=Broken
    report=runner.run()
    assert report['status']=='failed'
    assert any(x['id']=='CORE-RECOVERY-RACE-BATCH-'+runner.a.batch_mode.upper() and x['status']=='failed' for x in report['checks'])


def ledger_setup():
    from test_conformance import REG
    _,_,_,runner=setup()
    return runner.c,SamplePendingHost(REG,{'primary':{'max_frame':64,'window':80,'max_inflight':3},
                                          'peer':{'max_frame':64,'window':96,'max_inflight':2}})


def test_epoch_reuse_ignores_old_reader_even_with_same_corr():
    c,ledger=ledger_setup()
    old=c.request(c.core['clock'],session=17,corr=2)
    epoch=ledger.track('primary',old,19)
    late=b'\x02\x02\0\x01\0'
    ledger.abandon('primary')
    from oep_client.conformance import Violation
    with pytest.raises(Violation):ledger.track('primary',c.request(c.core['clock'],corr=2),19)
    with pytest.raises(Violation):ledger.resume('primary',transport_recovered=False,confirmed_boot=19)
    ledger.resume('primary',transport_recovered=True,confirmed_boot=20)
    with pytest.raises(Violation):ledger.track('primary',old,20)
    new=c.request(c.core['clock'],corr=2)
    current=ledger.track('primary',new,20)
    assert current!=epoch
    before=ledger.snapshot()
    assert not ledger.accept('primary',epoch,late)
    assert ledger.snapshot()==before
    assert ledger.accept('primary',current,late)
    assert [x['status'] for x in ledger.snapshot()]==['unknown','completed']


@pytest.mark.parametrize('invalid',['count','bytes','duplicate','zero','boot','detail','shape'])
def test_ledger_rejects_invalid_admission_or_result(invalid):
    from oep_client.conformance import Violation
    c,ledger=ledger_setup()
    request=c.request(c.core['clock'],session=17,corr=2)
    epoch=ledger.track('primary',request,19)
    before=ledger.snapshot()
    with pytest.raises(Violation):
        if invalid=='count':
            ledger.track('primary',c.request(c.core['clock'],session=17,corr=3),19)
            ledger.track('primary',c.request(c.core['clock'],session=17,corr=4),19)
            before=ledger.snapshot()
            ledger.track('primary',c.request(c.core['clock'],session=17,corr=5),19)
        elif invalid=='bytes':
            ledger.track('primary',c.request(c.core['clock'],bytes(54),session=17,corr=3),19)
            before=ledger.snapshot()
            ledger.track('primary',c.request(c.core['clock'],session=17,corr=4),19)
        elif invalid=='duplicate':ledger.track('primary',c.request(c.core['clock'],session=23,corr=2),19)
        elif invalid=='zero':ledger.track('primary',c.request(c.core['clock'],corr=0),19)
        elif invalid=='boot':ledger.track('primary',c.request(c.core['clock'],corr=3),20)
        elif invalid=='detail':ledger.accept('primary',epoch,b'\x02\x02\0\x01\xff')
        else:ledger.accept('primary',epoch,b'\x02\x02\0')
    assert ledger.snapshot()==before


@pytest.mark.parametrize('invalid',['transition','close','mode'])
def test_missing_controls_block_session_work(invalid):
    _,model,adapter,runner=setup()
    if invalid=='transition':adapter.arm_transition=None
    elif invalid=='close':adapter.close=None
    else:adapter.batch_mode='physical'
    report=runner.run()
    assert report['status']=='failed'
    assert model.ep.current_core.last is None


def test_known_rejection_is_preserved_but_result_lost_is_not_inferred_rejected():
    c,ledger=ledger_setup()
    epoch=ledger.track('primary',c.request(c.core['clock'],session=17,corr=2),19)
    ledger.track('primary',c.request(c.core['clock'],session=17,corr=3),19)
    ledger.track('primary',c.request(c.core['clock'],session=17,corr=4),19)
    rejected=b'\x02\x02\0\0\x03'
    lost=b'\x02\x03\0\0\x0c'
    assert ledger.accept('primary',epoch,rejected)
    assert ledger.accept('primary',epoch,lost)
    before=ledger.snapshot()
    assert [x['status'] for x in before]==['rejected','unknown','pending']
    affected=ledger.abandon('primary')
    assert [x['corr'] for x in affected]==[4]
    assert ledger.snapshot()[:2]==before[:2]
