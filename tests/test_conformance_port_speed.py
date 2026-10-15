import pytest
from oep_client.conformance import Checks
from oep_client.conformance_port_speed import PortSpeedChecks
from oep_client.conformance_port_speed_sample import SamplePortSpeedAdapter
from oep_client.virtual_port_speed_model import PortSpeedModel
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,c=registered_setup();c.reg.setdefault('timing',{}).update(port_speed_idle_ms=3000)
    c.reg.setdefault('limits',{}).update(port_speed_tolerance_pct=2)
    model=PortSpeedModel(boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1')


def test_port_speed_handshake():
    model,c=setup();r=PortSpeedChecks(c,SamplePortSpeedAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==23 and not r['full_conformance']


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    model,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',PortSpeedChecks.cases)
def test_wrong_wire_result_is_detected(case):
    model,c=setup();original=model.begin
    def broken(request,route=0):
        reply=original(request,route)
        if request[3:6]==b'\4\0\x12':reply=reply[:3]+b'\1\1'+reply[5:]
        model.pending['reply']=reply
        return reply
    model.begin=broken;r=PortSpeedChecks(c,SamplePortSpeedAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-PORT-SPEED-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('defect,case',[
    ('early_trial','TRY-BARRIER'),('trial_deadline_before_write','TRY-BARRIER'),
    ('wrong_actual','COMMIT'),('no_commit','COMMIT'),
    ('early_revert','REVERT-TRIAL'),('early_end','END-BARRIER'),
    ('wrong_sent_rate','REVERT-COMMITTED'),('no_verify_expiry','VERIFY'),
    ('verify_early','VERIFY'),('no_idle_expiry','IDLE'),('idle_early','IDLE'),
    ('restart_replay_deadline','REPLAY'),('wrong_cause','STATES'),
    ('ignore_critical','TLV'),('unsupported_tag_mask','TLV'),
    ('accept_zero_verify','MALFORMED'),('ignore_tolerance','TOLERANCE'),
    ('trial_changes_pin','COMMIT'),
    ('nonbridge_accepted','NON-BRIDGE'),('second_port_accepted','ONE-PORT'),
    ('invalid_frame_renews_idle','IDLE-INVALID'),('valid_frame_does_not_renew','IDLE-RENEW'),
    ('allowed_boundary_rejected','TOLERANCE'),('outside_boundary_accepted','TOLERANCE'),
    ('refusal_changes_other_port','ONE-PORT'),
    ('state_phase_stale','STATES'),('state_baud_boot','TRY-BARRIER'),('state_nonbridge_accepted','NON-BRIDGE'),
    ('state_ignores_critical','TLV'),('state_after_verify_stale','VERIFY'),('state_after_idle_stale','IDLE'),
    ('boot_rate_accepted','BOOT-RATE'),('revert_wrong_baud','REVERT-TRIAL'),
    ('commit_zero_verify_rejected','COMMIT'),('commit_needs_actual_baud','TOLERANCE'),
    ('one_port_committed_accepted','ONE-PORT'),('one_port_pending_accepted','ONE-PORT'),('end_not_pending','END-BARRIER'),
    ('no_pending_return','PENDING-VERIFY'),('pending_accepts_step','PENDING-VERIFY'),('verify_return_cancelled','PENDING-VERIFY'),
    ('pending_switch_mid_frame','PENDING-IDLE-OTHER'),('pending_return_missing','PENDING-VERIFY'),
    ('idle_return_cancelled','PENDING-IDLE-OWNER'),('idle_return_cancelled','PENDING-IDLE-OTHER'),
])
def test_semantic_faults(defect,case):
    model,c=setup();begin=model.begin;finish=model.finish;advance=model.advance;fall=model.fall
    raised=lambda state,deadline:dict(model.boot(),state=state,baud=230400,asked=230400,deadline=deadline,owner=model.ep.current_core.holder)
    ok=lambda reply,value:reply[:3]+b'\1\0'+value;stale=[]
    def bad_cancel(route):
        # the rule before oep-spec cf69012: a request frame on the port cancelled a pending return (an idle one, or -
        # wrongly even then - a verify one)
        p=model.ports.get(route)
        if not p or p['state']!=3:return
        if defect=='idle_return_cancelled' and p['why']=='idle':p.update(state=2,why=None,deadline=model.clock_ms+3000)
        if defect=='verify_return_cancelled' and p['why']=='verify':p.update(state=1,why=None,deadline=model.clock_ms+100)
    def bad_fall(route,why):
        if defect=='no_pending_return':model.reset_port(route)
        else:fall(route,why)
    def bad_begin(request,route=0):
        if len(request)>=10 and request[0]==1:bad_cancel(route)
        deadline=model.ports[0]['deadline'];phase=model.ports.get(route,{}).get('state')
        reply=begin(request,route)
        if request[3:6]==b'\4\0\x13' and reply:
            if defect=='state_phase_stale' and reply[3:5]==b'\1\0':reply=reply[:9]+b'\0'+reply[10:]
            if defect=='state_baud_boot' and reply[3:5]==b'\1\0':reply=reply[:5]+(115200).to_bytes(4,'little')+reply[9:]
            if defect=='state_nonbridge_accepted' and route==1:reply=ok(reply,b'\0\xc2\1\0\0')
            if defect=='state_ignores_critical' and reply[3:5]==b'\0\x0b':reply=ok(reply,(230400).to_bytes(4,'little')+b'\1')
            if stale and reply[3:5]==b'\1\0':reply=ok(reply,(230400).to_bytes(4,'little')+bytes((stale.pop(),)))
        if defect=='pending_switch_mid_frame' and phase==3:model.reset_port(route)
        if defect=='end_not_pending' and request[3:6]==b'\0\0\x11':
            for p in model.ports.values():
                if p['state']==3:p.update(state=2,why=None)
        if defect=='valid_frame_does_not_renew' and request[3:6]==b'\0\0\1':model.ports[0]['deadline']=deadline
        if request[3:6]==b'\4\0\x12':
            step=request[14] if len(request)>=17 else -1
            if defect=='early_trial' and step==0 and reply[3:5]==b'\1\0':model.ports[route]=raised(1,model.clock_ms+100)
            if defect=='early_revert' and step==2:model.reset_port(route)
            if defect=='wrong_actual' and step==1:reply=reply[:5]+(460800).to_bytes(4,'little')
            if defect=='wrong_cause' and reply[3:5]==b'\0\4':reply=reply[:-1]+b'\2'
            if defect=='ignore_critical' and reply[3:5]==b'\0\x0b':reply=reply[:3]+b'\1\0'+b'\0'*4
            if defect=='unsupported_tag_mask' and reply[3:5]==b'\0\x0b' and reply[5]==255:reply=reply[:5]+b'\x7f'+reply[6:]
            if defect=='accept_zero_verify' and reply[3:5]==b'\0\3':reply=reply[:3]+b'\1\0'+b'\0'*4
            if defect=='ignore_tolerance' and reply[3:5]==b'\0\x0b':reply=reply[:3]+b'\1\0'+(230400).to_bytes(4,'little')
            if defect=='nonbridge_accepted' and route==1:reply=reply[:3]+b'\1\0'+(230400).to_bytes(4,'little')
            if defect=='second_port_accepted' and route==2:reply=reply[:3]+b'\1\0'+(230400).to_bytes(4,'little')
            if defect=='refusal_changes_other_port' and route==2:model.ports[2]=raised(1,model.clock_ms+100)
            if defect=='boot_rate_accepted' and step==0 and reply[3:5]==b'\0\x0b':reply=ok(reply,(115200).to_bytes(4,'little'))
            if defect=='revert_wrong_baud' and step==2 and reply[3:5]==b'\1\0':reply=ok(reply,(230400).to_bytes(4,'little'))
            if defect=='commit_zero_verify_rejected' and step==1 and request[15:17]==b'\0\0':reply=reply[:3]+b'\0\3'
            if defect=='commit_needs_actual_baud' and step==1 and int.from_bytes(request[10:14],'little')==232000:reply=reply[:3]+b'\0\4\1\1\0\6'
            if defect=='one_port_committed_accepted' and route==2 and step==0 and model.ports[0]['state']==2:reply=ok(reply,(230400).to_bytes(4,'little'))
            if defect=='one_port_pending_accepted' and route==2 and step==0 and model.ports[0]['state']==3:reply=ok(reply,(230400).to_bytes(4,'little'))
            if defect=='pending_accepts_step' and phase==3 and step==1:reply=ok(reply,(230400).to_bytes(4,'little'))
            rate=int.from_bytes(request[10:14],'little')
            if defect=='allowed_boundary_rejected' and rate==235102:reply=reply[:3]+b'\0\x0b\0'
            if defect=='outside_boundary_accepted' and rate==235103:reply=reply[:3]+b'\1\0'+(230400).to_bytes(4,'little')
        if defect=='early_end' and request[3:6]==b'\0\0\x11':model.reset_all()
        model.pending['reply']=reply
        return reply
    def bad_finish():
        pending=model.pending;out=finish()
        q=pending['request']
        if defect=='invalid_frame_renews_idle' and pending['reply'] is None and model.ports[0]['state']==2:model.ports[0]['deadline']=model.clock_ms+3000
        if defect=='valid_frame_does_not_renew' and q[3:6]==b'\0\0\1' and model.ports[0]['state']==2:model.ports[0]['deadline']-=2000
        if defect=='pending_return_missing' and pending['route']==0 and model.ports[0]['state']==0 and pending['reply'] is not None and model.events[-1]['baud']==230400 and not pending['actions']:
            model.ports[0]=raised(2,model.clock_ms+3000)
        if q[3:6]==b'\4\0\x12' and len(q)>=17:
            step=q[14]
            if defect=='trial_deadline_before_write' and step==0:model.ports[0]['deadline']=100
            if defect=='no_commit' and step==1:model.ports[0]['state']=1
            if defect=='trial_changes_pin' and step==0:model.extension.pins[1]=(3,0)
            if defect=='restart_replay_deadline' and step==0 and not pending['actions']:model.ports[0]['deadline']=model.clock_ms+100
            if defect=='wrong_sent_rate' and step==2:model.events[-1]['baud']=115200
        return out
    def bad_advance(delta):
        p=model.ports[0].copy();advance(delta)
        if defect=='no_verify_expiry' and p['state']==1:model.ports[0]=p
        if defect=='verify_early' and p['state']==1:model.reset_port(0)
        if defect=='no_idle_expiry' and p['state']==2:model.ports[0]=p
        if defect=='idle_early' and p['state']==2:model.reset_port(0)
        if defect in ('state_after_verify_stale','state_after_idle_stale') and p['state']==(1 if 'verify' in defect else 2) and not model.ports[0]['state']:stale[:]=[p['state']]
    model.begin=bad_begin;model.finish=bad_finish;model.advance=bad_advance;model.fall=bad_fall
    r=PortSpeedChecks(c,SamplePortSpeedAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-PORT-SPEED-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['raw','select','speed','begin','finish','advance','receive','events','seed','restart_fixture'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SamplePortSpeedAdapter(model);setattr(a,missing,None)
    r=PortSpeedChecks(c,a).run()
    assert r['checks'][2]['status']=='failed'


def test_port_speed_without_port_state_does_not_pass():
    model,c=setup();describe=model.extension.describe
    model.extension.describe=lambda fn:(b'\7\2\0\x10\x07',) if fn==4 else describe(fn)
    r=PortSpeedChecks(c,SamplePortSpeedAdapter(model)).run()
    assert r['status']=='failed' and next(x for x in r['checks'] if x['id']=='OEP-PORT-SPEED-CONTEXT')['status']=='failed'
    assert next(x for x in r['checks'] if x['id']=='OEP-REGISTERED-OPS-4')['status']=='failed'
