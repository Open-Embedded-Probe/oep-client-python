import pytest
from oep_client.conformance_host_wait import HostWaitChecks,reviewed_identity as actual_review,REVIEWED
from oep_client.conformance_host_wait_sample import SampleHostWaitAdapter


SPEC='/explicit/host-wait-spec'


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_host_wait.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)


def test_existing_host_save_budget_path():
    r=HostWaitChecks(SampleHostWaitAdapter(),spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==4 and len(r['observations'])==87 and not r['full_conformance']
    assert not r['production_opcode_alignment_proven']


@pytest.mark.parametrize('fault,group',[
    ('no_argument','SAVE'),('no_margin','SAVE'),('no_transfer','SAVE'),('no_notifications','SAVE'),
    ('wrong_wire_length','SAVE'),('lease_clamp','SAVE'),('short_timeout','SAVE'),
    ('outer_discarded','SAVE'),('scope_leaks','SAVE'),('failure_scope_leaks','FAILURE'),
    ('wrong_hash','SAVE'),('wrong_projection','SAVE'),('latest_not_applied','LIMITS')])
def test_budget_and_scope_faults_are_detected(fault,group):
    a=SampleHostWaitAdapter();save=a.save;limits=a.limits
    def broken(case):
        r=save(case);p=r['record'];argument=p['expect_ms']/1000
        if fault=='no_argument':p['expect_ms']=0
        if fault=='no_margin':p['wait_s']=argument+p['transfer_s']
        if fault=='no_transfer' and case['kind']=='cobs':p['wait_s']=argument+1
        if fault=='no_notifications' and case['kind']=='cobs':p['wait_s']=argument+1+(p['tx_len']+p['frame'])*10/p['baud']
        if fault=='wrong_wire_length':p['tx_len']=10
        if fault=='lease_clamp':p['wait_s']=min(p['wait_s'],.25)
        if fault=='short_timeout':p['wait_s']=case['timeout']
        if fault=='outer_discarded' and case['outer_ms']>case['maximum']:p['expect_ms']=case['maximum']
        if fault=='scope_leaks':r['final_expect_ms']=case['maximum']
        if fault=='failure_scope_leaks' and case['failure']:r['final_expect_ms']=case['maximum']
        if fault=='wrong_hash':r['value']=0
        if fault=='wrong_projection':r['projection']['current_save_code']=3
        return r
    def broken_limits(kind,baud):
        rows=limits(kind,baud)
        if fault=='latest_not_applied':rows[-1]['frame']=256
        return rows
    a.save=broken;a.limits=broken_limits;r=HostWaitChecks(a,spec=SPEC).run();row=next(x for x in r['checks'] if x['id']=='HOST-WAIT-'+group)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['save','limits','source_sha256'])
def test_missing_controls_do_not_pass(missing):
    a=SampleHostWaitAdapter();setattr(a,missing,None);r=HostWaitChecks(a,spec=SPEC).run();assert r['checks'][0]['status']=='failed'


def registry():
    return dict(timing=dict(host_wait_add_ms=1000,notify_pending_max_frames=2),constants=dict(min_max_frame=64),limits=dict(max_op_ms_max=600000),
                interface=[dict(name='oep.probe.config',revision=1,op=[dict(name='save',code=18)])])


@pytest.mark.parametrize('document',REVIEWED)
def test_changed_reviewed_document_is_not_accepted(monkeypatch,tmp_path,document):
    import hashlib
    import oep_client.conformance_host_wait as module
    from oep_client.conformance import Violation
    hashes={}
    for name in REVIEWED:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'reviewed');hashes[name]=hashlib.sha256(b'reviewed').hexdigest()
    (tmp_path/document).write_bytes(b'changed')
    monkeypatch.setattr(module,'REVIEWED',hashes);monkeypatch.setattr(module,'spec_identity',lambda path:(registry(),{'commit':'unit-fixture'}))
    with pytest.raises(Violation,match='document changed'):actual_review(tmp_path)


@pytest.mark.parametrize('field',['margin','notifications','frame','maximum','save'])
def test_changed_registry_wait_rule_is_not_accepted(monkeypatch,field):
    import oep_client.conformance_host_wait as module
    from oep_client.conformance import Violation
    r=registry()
    if field=='margin':r['timing']['host_wait_add_ms']=999
    if field=='notifications':r['timing']['notify_pending_max_frames']=1
    if field=='frame':r['constants']['min_max_frame']=63
    if field=='maximum':r['limits']['max_op_ms_max']=600001
    if field=='save':r['interface'][0]['op'][0]['code']=3
    monkeypatch.setattr(module,'spec_identity',lambda path:(r,{'commit':'unit-fixture'}))
    with pytest.raises(Violation):actual_review('/explicit/changed-wait-spec')
