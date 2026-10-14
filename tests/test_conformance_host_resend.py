import pytest
from oep_client.conformance_host_resend import HostResendChecks
from oep_client.conformance_host_resend_sample import SampleHostResendAdapter


SPEC='/explicit/host-resend-spec'


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_host_resend.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)


def test_same_byte_resends_and_probe_cache():
    r=HostResendChecks(SampleHostResendAdapter(),spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==8 and len(r['observations'])==7 and not r['full_conformance']


@pytest.mark.parametrize('fault,case',[
    ('changed_corr','LOST-COBS'),('changed_payload','LOST-LENGTH'),('changed_reply','LOST-COBS'),
    ('reexecute','PIPELINE-COBS'),('renew_replay','PIPELINE-LENGTH'),('early_retry','LOST-COBS'),
    ('repeat_resolved','PIPELINE-COBS'),('no_failed_flag','FAILURE-RECOVER'),
    ('no_confirm','FAILURE-RECOVER'),('state_before_confirm','FAILURE-RECOVER'),
    ('resume_when_blocked','FAILURE-BLOCKED'),('wrong_result','BAD-CRC'),('lost_live','LOST-COBS')])
def test_retry_fault_observations_are_detected(fault,case):
    a=SampleHostResendAdapter();run=a.run_case
    def broken(name):
        r=run(name)
        if name!=case:return r
        events=r['events'];alloc=[e for e in events if e['action']=='peer' and bytes.fromhex(e['request_hex'])[3:6]==b'\1\0\x10']
        repeated=alloc[2] if name.startswith('PIPELINE') else alloc[1]
        if fault=='changed_corr':p=bytearray.fromhex(repeated['request_hex']);p[1]=4;repeated['request_hex']=p.hex()
        if fault=='changed_payload':repeated['request_hex']=repeated['request_hex'][:-2]+'02'
        if fault=='changed_reply':repeated['reply_hex']='02020001000200'
        if fault=='reexecute':repeated['next_after']+=1
        if fault=='renew_replay':repeated['deadline_after']+=1
        if fault=='early_retry':[e for e in events if e['action']=='write'][1]['started_ns']=1
        if fault=='repeat_resolved':alloc[2]['request_hex']=alloc[0]['request_hex']
        if fault=='no_failed_flag':r['failed_before_next']=False
        if fault=='no_confirm':r['events']=[e for e in events if not(e['action']=='peer' and bytes.fromhex(e['request_hex'])[3:6]==b'\0\0\1')]
        if fault=='state_before_confirm':
            e=next(e for e in events if e['action']=='peer' and bytes.fromhex(e['request_hex'])[3:6]==b'\1\0\x13');events.remove(e);events.insert(0,e)
        if fault=='resume_when_blocked':r['failed_after_next']=False;r['next_id']=3
        if fault=='wrong_result':r['replies_hex']=['02020001000200']
        if fault=='lost_live':r['live']={}
        return r
    a.run_case=broken;r=HostResendChecks(a,spec=SPEC).run();row=next(x for x in r['checks'] if x['id']=='HOST-RESEND-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
    ('no_resend','LOST-COBS'),('short_wait','LOST-LENGTH'),('retry_new_corr','LOST-COBS'),
    ('forget_cache','LOST-COBS'),('retry_renews','PIPELINE-COBS'),('bypass_failed_recovery','FAILURE-RECOVER')])
def test_actual_link_and_core_faults_are_detected(monkeypatch,fault,case):
    from oep_client import link
    from oep_client.virtual_core import Core
    a=SampleHostResendAdapter();run=a.run_case
    def broken(name):
        if name!=case:return run(name)
        with monkeypatch.context() as m:
            if fault=='no_resend':
                setup=link.SerialLink._setup
                def bad_setup(d,*args):setup(d,*args);d.resend=False
                m.setattr(link.SerialLink,'_setup',bad_setup)
            if fault=='short_wait':m.setattr(link.SerialLink,'_wait',lambda d:.05)
            if fault=='retry_new_corr':
                write=link.SerialLink._write;count=0
                def bad_write(d,msgs):
                    nonlocal count
                    msgs=list(msgs)
                    if any(p[3:6]==b'\1\0\x10' for p in msgs):
                        count+=1
                        if count>1:msgs=[p[:1]+b'\4\0'+p[3:] for p in msgs]
                    return write(d,msgs)
                m.setattr(link.SerialLink,'_write',bad_write)
            if fault in ('forget_cache','retry_renews'):
                handle=Core.handle
                def bad_core(c,data,*args,**kwargs):
                    replay=data[3:6]==b'\1\0\x10' and int.from_bytes(data[1:3],'little') in c.cache
                    if replay and fault=='forget_cache':c.cache.clear();c.high=0
                    out=handle(c,data,*args,**kwargs)
                    if replay and fault=='retry_renews':c.deadline+=1
                    return out
                m.setattr(Core,'handle',bad_core)
            if fault=='bypass_failed_recovery':m.setattr(link.SerialLink,'recover_transport',lambda d,*args,**kwargs:setattr(d,'failed_transport',''))
            return run(name)
    a.run_case=broken;r=HostResendChecks(a,spec=SPEC).run();row=next(x for x in r['checks'] if x['id']=='HOST-RESEND-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['run_case','source_sha256'])
def test_missing_controls_do_not_pass(missing):
    a=SampleHostResendAdapter();setattr(a,missing,None);r=HostResendChecks(a,spec=SPEC).run();assert r['checks'][0]['status']=='failed'


@pytest.mark.parametrize('case',['LOST-COBS','LOST-LENGTH','PIPELINE-COBS','FAILURE-RECOVER'])
def test_argument_time_resend_floor_is_detected(monkeypatch,case):
    # core §4.4 at a09fa45: a resend waits max_op_ms + 1000 ms + transfer, not the argument time (0) + 1000 + transfer.
    from oep_client import link
    monkeypatch.setattr(link.SerialLink,'_wait',lambda d:1+d.transfer_s())
    r=HostResendChecks(SampleHostResendAdapter(),spec=SPEC).run();row=next(x for x in r['checks'] if x['id']=='HOST-RESEND-'+case)
    assert row['status']=='failed' and 'before missing response wait' in row['error'],row


def test_missing_fixture_max_op_ms_is_not_assumed():
    a=SampleHostResendAdapter();run=a.run_case
    def broken(name):
        r=run(name);r.pop('max_op_ms');return r
    a.run_case=broken;r=HostResendChecks(a,spec=SPEC).run()
    assert next(x for x in r['checks'] if x['id']=='HOST-RESEND-LOST-COBS')['status']=='failed'
