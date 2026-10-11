import pytest
from oep_client.conformance_port_speed_lifecycle import PortSpeedLifecycleChecks
from oep_client.conformance_port_speed_sample import SamplePortSpeedAdapter
from test_conformance_port_speed import setup


def test_session_release_and_stale_writes():
    model,c=setup();r=PortSpeedLifecycleChecks(c,SamplePortSpeedAdapter(model)).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==7 and not r['full_conformance']


@pytest.mark.parametrize('case',PortSpeedLifecycleChecks.cases)
def test_bad_result_is_detected(case):
    model,c=setup();begin=model.begin
    def broken(req,route=0):
        out=begin(req,route)
        if req[3:6]==b'\0\0\x10' and len(req)>14 and req[14]:out=out[:5]+b'\0'*8;model.pending['reply']=out
        if req[3:6]==b'\0\0\x12' and out[3:5]==b'\0\7':out=out[:3]+b'\1\0';model.pending['reply']=out
        return out
    model.begin=broken;r=PortSpeedLifecycleChecks(c,SamplePortSpeedAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-PORT-SPEED-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
 ('early_force','FORCE-TRIAL'),('force_no_reset','FORCE-COMMITTED'),
 ('expiry_keeps_speed','EXPIRY-TRIAL'),('expiry_keeps_owner','EXPIRY-COMMITTED'),
 ('expiry_early','EXPIRY-TRIAL'),('stale_trial','EXPIRED-TRY-WRITE'),
 ('stale_commit','EXPIRED-COMMIT-WRITE'),('stale_owner','EXPIRED-TRY-WRITE')])
def test_state_faults_are_detected(fault,case):
    model,c=setup();begin=model.begin;advance=model.advance;finish=model.finish
    def bad_begin(req,route=0):
        out=begin(req,route)
        if fault=='early_force' and req[3:6]==b'\0\0\x10' and len(req)>14 and req[14]:model.reset_all()
        return out
    def bad_advance(delta):
        p=model.ports[0].copy();core=model.ep.current_core;holder=core.holder
        if fault=='expiry_keeps_owner':core.deadline+=delta
        advance(delta)
        if fault=='expiry_keeps_speed' and holder is not None:model.ports[0]=p
        if fault=='expiry_early' and delta==999:core.release()
    def bad_finish():
        pending=model.pending;p=model.ports[0].copy();out=finish()
        if fault=='force_no_reset' and ('reset',) in pending['actions']:model.ports[0]=p
        if pending['token'] and model.ep.current_core.holder is None:
            if fault in ('stale_trial','stale_commit'):model.ports[0]=dict(state=1 if fault=='stale_trial' else 2,baud=230400,deadline=model.clock_ms+5000)
            if fault=='stale_owner':model.ep.current_core.holder=pending['token'][0]
        return out
    model.begin=bad_begin;model.advance=bad_advance;model.finish=bad_finish
    r=PortSpeedLifecycleChecks(c,SamplePortSpeedAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-PORT-SPEED-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row
