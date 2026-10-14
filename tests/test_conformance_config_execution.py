from oep_client.conformance import Checks
from oep_client.conformance_config_execution import ConfigExecutionChecks
from oep_client.conformance_config_execution_sample import SampleConfigExecutionAdapter
from oep_client.virtual_config_execution_model import ConfigExecutionModel
from test_conformance_registered_ops import setup as base_setup
import pytest


def setup():
    _,base=base_setup();model=ConfigExecutionModel()
    return model,Checks(model.handle,base.reg,'virtual-resource-1')


def test_save_execution_and_writer_lease_boundaries():
    model,c=setup();r=ConfigExecutionChecks(c,SampleConfigExecutionAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
    assert len(r['checks'])==12 and not r['full_conformance']


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


def test_existing_config_operations_with_writer_adapter():
    from oep_client.conformance_config import ConfigChecks
    from oep_client.conformance_config_sample import SampleConfigAdapter
    model,c=setup();r=ConfigChecks(c,SampleConfigAdapter(model)).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',ConfigExecutionChecks.cases)
def test_wrong_save_result_is_detected(case):
    model,c=setup();a=SampleConfigExecutionAdapter(model);checker=ConfigExecutionChecks(c,a);case_run=checker.case;send=c.send;active=None
    def run(name):
        nonlocal active
        active=name;return case_run(name)
    def bad(req):
        out=send(req)
        if active==case and req[3:6]==b'\4\0\x12':return out[:3]+b'\1\1'+out[5:]
        return out
    # buffered() replaces c.send; inject through its explicit begin control too.
    begin=a.begin
    def bad_begin(req):
        out=begin(req)
        if active==case and req[3:6]==b'\4\0\x12':return out[:3]+b'\1\1'+out[5:]
        return out
    checker.case=run;c.send=bad;a.begin=bad_begin;r=checker.run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-EXECUTION-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('case',ConfigExecutionChecks.cases[:-2])
def test_truncated_save_result_tail_is_detected(case):
    model,c=setup();a=SampleConfigExecutionAdapter(model);checker=ConfigExecutionChecks(c,a);case_run=checker.case;begin=a.begin;active=None
    def run(name):
        nonlocal active
        active=name;return case_run(name)
    def bad(req):
        out=begin(req)
        if active==case and req[3:6]==b'\4\0\x12':out+=b'\1'
        return out
    checker.case=run;a.begin=bad;r=checker.run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-EXECUTION-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
    ('no_pause','RUN-1500-1000'),('no_generation_renew','RUN-5000-60000'),
    ('pause_writer','RUN-1500-1000'),('renew_writer','RUN-5000-1000'),
    ('send_renew','RUN-1500-60000'),('revive_expired','EXPIRE-1500'),
    ('busy_response','RUN-5000-1000'),('busy_execute','RUN-1500-1000'),
    ('wrong_elapsed','RUN-5000-1000'),('no_duration','RUN-1500-1000'),
    ('save_changes_pin','RUN-5000-1000'),('noop_writes','SAME'),
    ('rejected_writes','TLV'),('capacity_writes','CAPACITY'),('replay_writes','EXPIRE-5000')])
def test_execution_faults_are_detected(fault,case):
    model,c=setup();a=SampleConfigExecutionAdapter(model);checker=ConfigExecutionChecks(c,a);case_run=checker.case;active=None
    begin=a.begin;advance=a.advance;finish=a.finish;handle=model.handle;write=model.write_config;persist=model.persist_config
    def run(name):
        nonlocal active
        active=name;return case_run(name)
    def bad_begin(req):
        start=model.clock_ms;before=model.ep.current_core.deadline;r=begin(req)
        if active==case:
            core=model.ep.current_core;elapsed=model.clock_ms-start
            if fault=='no_pause':core.deadline-=elapsed
            if fault=='no_generation_renew':core.deadline=before+elapsed      # the pre-a09fa45 rule (core §6.1)
            if fault=='rejected_writes':model.writes+=1
            if fault=='capacity_writes':model.writes+=1
        return r
    def bad_advance(delta):
        pending=model.pending is not None;advance(delta)
        if active==case and pending:
            if fault=='pause_writer':model.ep.current_core.deadline+=delta
            if fault=='renew_writer':model.ep.current_core.deadline=model.clock_ms+model.ep.current_core.lease
    def bad_finish():
        p=model.pending;deadline=model.ep.current_core.deadline;holder=model.ep.current_core.holder;r=finish()
        if active==case:
            if fault=='send_renew':model.ep.current_core.deadline=model.clock_ms+model.ep.current_core.lease
            if fault=='revive_expired' and holder is None:model.ep.current_core.holder=p['token'][0]
        return r
    def bad_handle(req,route=0):
        if active==case and model.in_write:
            if fault=='busy_response':return b'\2'+req[1:3]+b'\1\0'
            if fault=='busy_execute':
                model.in_write=False
                try:return handle(req,route)
                finally:model.in_write=True
        if active==case and fault=='replay_writes' and model.ep.current_core.holder is None and req[3:6]==b'\4\0\x12':model.writes+=1
        return handle(req,route)
    def bad_write(items):
        if active==case and fault=='no_duration':model.execution_ms=0
        write(items)
        if active==case and fault=='wrong_elapsed':model.clock_ms+=1
        if active==case and fault=='save_changes_pin':model.extension.pins[1]=3
    def bad_persist(items):
        if active==case and fault=='noop_writes':return bad_write(items)
        return persist(items)
    checker.case=run;a.begin=bad_begin;a.advance=bad_advance;a.finish=bad_finish;model.handle=bad_handle;model.write_config=bad_write;model.persist_config=bad_persist;c.send=bad_handle
    r=checker.run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-EXECUTION-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','generation_at','prepare','arm','advance','begin','finish'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigExecutionAdapter(model);setattr(a,missing,None)
    r=ConfigExecutionChecks(c,a).run();assert r['checks'][2]['status']=='failed'
