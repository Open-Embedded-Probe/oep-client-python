import pytest
from oep_client.conformance_host_deadline import HostDeadlineChecks,reviewed_identity as actual_review,REVIEWED
from oep_client.conformance_host_deadline_sample import SampleHostDeadlineAdapter


SPEC='/explicit/host-deadline-spec'


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_host_deadline.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)


def test_wait_loop_and_pipeline_deadline_origins():
    r=HostDeadlineChecks(SampleHostDeadlineAdapter(),spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==5 and len(r['observations'])==24 and not r['full_conformance']


@pytest.mark.parametrize('fault,group',[
    ('short_timeout','SINGLE'),('no_argument','SINGLE'),('no_margin','SINGLE'),('no_transfer','SINGLE'),
    ('write_start','SINGLE'),('pipeline_write','PIPELINE'),('pipeline_first_byte','PIPELINE'),
    ('pipeline_missing_write','PIPELINE-TIMEOUT')])
def test_actual_wait_loop_faults_are_detected(monkeypatch,fault,group):
    from oep_client import link
    wait=link.SerialLink._wait;recv=link.SerialLink._recv_for
    if fault in ('short_timeout','no_argument','no_margin','no_transfer','write_start'):
        def bad_wait(d):
            if fault=='short_timeout':return .05
            if fault=='no_argument':return 1+d.transfer_s()
            if fault=='no_margin':return d.expected_s()+d.transfer_s()
            if fault=='no_transfer':return d.expected_s()+1
            return max(0,wait(d)-d.stream.events[0]['time_ns']/1e9)
        monkeypatch.setattr(link.SerialLink,'_wait',bad_wait)
    else:
        def bad_recv(d,corr):
            if corr!=8:return recv(d,corr)
            old=d._wait
            event=next(e for e in d.stream.events if e['action']==('arrival' if fault=='pipeline_first_byte' else 'write'))
            remaining=max(0,old()-(link.time.monotonic()-event['time_ns']/1e9))
            d._wait=lambda:remaining
            try:return recv(d,corr)
            finally:d._wait=old
        monkeypatch.setattr(link.SerialLink,'_recv_for',bad_recv)
    r=HostDeadlineChecks(SampleHostDeadlineAdapter(),spec=SPEC).run();row=next(x for x in r['checks'] if x['id']=='HOST-DEADLINE-'+group)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,group',[
    ('early_result','SINGLE'),('first_byte_result','PIPELINE'),('wrong_corr','PIPELINE'),
    ('rewrite','SINGLE'),('missing_but_success','TIMEOUT'),('early_timeout','TIMEOUT'),
    ('missing_second_early_timeout','PIPELINE-TIMEOUT'),('wrong_floor','SINGLE'),('wrong_write_length','SINGLE'),
    ('reply_corr','SINGLE'),('request_bytes','SINGLE')])
def test_observation_faults_are_detected(fault,group):
    a=SampleHostDeadlineAdapter();run=a.run_case
    def bad(case):
        r=run(case);w=next(e for e in r['events'] if e['action']=='write')
        if fault=='early_result' and r['received']:r['received'][0]['time_ns']-=1
        if fault=='first_byte_result' and len(r['received'])==2:r['received'][1]['started_ns']-=10_000_000
        if fault=='wrong_corr' and len(r['received'])==2:r['received'][1]['corr']=7
        if fault=='rewrite':r['events'].append(dict(w))
        if fault=='missing_but_success' and case['missing']:r['outcome']='success'
        if fault in ('early_timeout','missing_second_early_timeout'):
            for e in r['events']:
                if e['action']=='timeout':e['time_ns']=e['started_ns']+w['minimum_ns']-1
        if fault=='wrong_floor':w['minimum_ns']-=1
        if fault=='wrong_write_length':w['wire_length']-=1
        if fault=='reply_corr' and r['received']:r['received'][0]['reply_hex']='0208000100'
        if fault=='request_bytes':w['requests_hex'][0]='ff'
        return r
    a.run_case=bad;r=HostDeadlineChecks(a,spec=SPEC).run();row=next(x for x in r['checks'] if x['id']=='HOST-DEADLINE-'+group)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['run_case','source_sha256'])
def test_missing_controls_do_not_pass(missing):
    a=SampleHostDeadlineAdapter();setattr(a,missing,None);r=HostDeadlineChecks(a,spec=SPEC).run();assert r['checks'][0]['status']=='failed'


def registry():return dict(timing=dict(host_wait_add_ms=1000,notify_pending_max_frames=2),constants=dict(min_max_frame=64))


@pytest.mark.parametrize('document',REVIEWED)
def test_document_guard(monkeypatch,tmp_path,document):
    import hashlib
    import oep_client.conformance_host_deadline as module
    from oep_client.conformance import Violation
    hashes={}
    for name in REVIEWED:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'reviewed');hashes[name]=hashlib.sha256(b'reviewed').hexdigest()
    (tmp_path/document).write_bytes(b'changed');monkeypatch.setattr(module,'REVIEWED',hashes)
    monkeypatch.setattr(module,'spec_identity',lambda path:(registry(),{'commit':'unit-fixture'}))
    with pytest.raises(Violation,match='document changed'):actual_review(tmp_path)


@pytest.mark.parametrize('field',['margin','notifications','frame'])
def test_registry_guard(monkeypatch,field):
    import oep_client.conformance_host_deadline as module
    from oep_client.conformance import Violation
    r=registry()
    if field=='margin':r['timing']['host_wait_add_ms']=999
    if field=='notifications':r['timing']['notify_pending_max_frames']=1
    if field=='frame':r['constants']['min_max_frame']=63
    monkeypatch.setattr(module,'spec_identity',lambda path:(r,{'commit':'unit-fixture'}))
    with pytest.raises(Violation):actual_review('/explicit/changed-deadline-spec')
