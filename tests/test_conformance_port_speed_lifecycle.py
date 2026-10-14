import pytest
from oep_client.conformance_port_speed_lifecycle import PortSpeedLifecycleChecks
from oep_client.conformance_port_speed_sample import SamplePortSpeedAdapter
from test_conformance_port_speed import setup


def test_session_release_and_stale_writes():
    model,c=setup();r=PortSpeedLifecycleChecks(c,SamplePortSpeedAdapter(model)).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==10 and not r['full_conformance']


@pytest.mark.parametrize('case',PortSpeedLifecycleChecks.cases)
def test_bad_result_is_detected(case):
    model,c=setup();begin=model.begin
    def broken(req,route=0):
        out=begin(req,route)
        if req[3:6]==b'\0\0\x10' and len(req)>14 and req[14]:out=out[:5]+b'\0'*8;model.pending['reply']=out
        if req[3:6]==b'\0\0\x12' and out[3:5]==b'\0\7':out=out[:3]+b'\1\0';model.pending['reply']=out
        if req[3:6]==b'\4\0\x12' and out[3:5]==b'\0\7':out=out[:3]+b'\0\4\1\1\0\6';model.pending['reply']=out
        if req[3:6]==b'\4\0\x13' and out[3:5]==b'\1\0':out=out[:9]+bytes((out[9]^1,))+out[10:];model.pending['reply']=out
        return out
    model.begin=broken;r=PortSpeedLifecycleChecks(c,SamplePortSpeedAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-PORT-SPEED-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
 ('early_force','FORCE-TRIAL'),('force_no_reset','FORCE-COMMITTED'),
 ('expiry_keeps_speed','EXPIRY-TRIAL'),('expiry_keeps_owner','EXPIRY-COMMITTED'),
 ('expiry_early','EXPIRY-TRIAL'),('stale_trial','EXPIRED-TRY-WRITE'),
 ('stale_commit','EXPIRED-COMMIT-WRITE'),('stale_owner','EXPIRED-TRY-WRITE'),
 ('commit_write_switched','EXPIRED-COMMIT-WRITE'),('expiry_pending_immediate','EXPIRY-PENDING'),
 ('pending_old_cause6','EXPIRY-PENDING-OLD'),('session_return_cancelled','EXPIRY-PENDING-OPEN'),
 ('session_return_kept','EXPIRY-PENDING'),('force_not_pending','FORCE-COMMITTED')])
def test_state_faults_are_detected(fault,case):
    model,c=setup();begin=model.begin;advance=model.advance;finish=model.finish;fall=model.fall
    def bad_fall(route,why):
        if fault=='expiry_pending_immediate':model.reset_port(route)
        else:fall(route,why)
    def bad_begin(req,route=0):
        phase=model.ports[0]['state'];out=begin(req,route)
        if fault=='early_force' and req[3:6]==b'\0\0\x10' and len(req)>14 and req[14]:model.reset_all()
        if fault=='force_not_pending' and req[3:6]==b'\0\0\x10' and len(req)>14 and req[14]:model.ports[0].update(state=2,why=None)
        if fault=='pending_old_cause6' and phase==3 and req[3:6]==b'\4\0\x12':out=out[:3]+b'\0\4\1\1\0\6';model.pending['reply']=out
        if fault=='session_return_cancelled' and phase==3 and req[3:6]==b'\0\0\x10':model.ports[0].update(state=2,why=None,deadline=model.clock_ms+3000);model.pending['actions']=[]
        return out
    def bad_advance(delta):
        p=model.ports[0].copy();core=model.ep.current_core;holder=core.holder
        if fault=='expiry_keeps_owner':core.deadline+=delta
        advance(delta)
        if fault=='expiry_keeps_speed' and holder is not None:model.ports[0]=p
        if fault=='expiry_early' and delta==999:core.release()
        if fault=='commit_write_switched' and model.pending and model.ports[0]['state']==3:model.reset_port(0)
    def bad_finish():
        pending=model.pending;p=model.ports[0].copy();out=finish()
        if fault=='force_no_reset' and ('reset',) in pending['actions']:model.ports[0]=p
        if fault=='session_return_kept' and p['state']==3:model.ports[0]=p
        if pending['token'] and model.ep.current_core.holder is None:
            if fault in ('stale_trial','stale_commit'):model.ports[0]=dict(model.boot(),state=1 if fault=='stale_trial' else 2,baud=230400,asked=230400,deadline=model.clock_ms+5000)
            if fault=='stale_owner':model.ep.current_core.holder=pending['token'][0]
        return out
    model.begin=bad_begin;model.advance=bad_advance;model.finish=bad_finish;model.fall=bad_fall
    r=PortSpeedLifecycleChecks(c,SamplePortSpeedAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-PORT-SPEED-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row
