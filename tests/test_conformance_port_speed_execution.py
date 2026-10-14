from oep_client.conformance import Checks
from oep_client.conformance_port_speed_execution import PortSpeedExecutionChecks
from oep_client.conformance_port_speed_execution_sample import SamplePortSpeedExecutionAdapter as Adapter
from oep_client.virtual_port_speed_execution_model import PortSpeedExecutionModel
from test_conformance_port_speed import setup as base_setup


def setup():
    _,c=base_setup();model=PortSpeedExecutionModel(boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1')


def test_execution_and_writer_wait():
    model,c=setup();r=PortSpeedExecutionChecks(c,Adapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==11 and not r['full_conformance']


import pytest


@pytest.mark.parametrize('case',PortSpeedExecutionChecks.cases)
def test_each_execution_schedule(case):
    model,c=setup();checks=PortSpeedExecutionChecks(c,Adapter(model));checks.cases=(case,)
    r=checks.run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',PortSpeedExecutionChecks.cases)
def test_bad_wire_response_is_detected(case):
    model,c=setup();begin=model.begin
    def bad(req,route=0):
        out=begin(req,route)
        if req[3:6] in (b'\4\0\x10',b'\4\0\x11'):
            out=out[:3]+b'\0\3';model.pending['reply']=out
        return out
    model.begin=bad;checks=PortSpeedExecutionChecks(c,Adapter(model));checks.cases=(case,)
    r=checks.run();row=r['checks'][-1]
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault',[
 'missing_execution','wrong_execution_duration','consume_idle','renew_lease_early','consume_lease',
 'writer_pauses_idle','writer_pauses_lease','no_idle_send_renewal','no_lease_send_renewal',
 'execution_changes_pin','idle_expiry_early','idle_expiry_missing'])
def test_clock_faults_are_detected(fault):
    model,c=setup();begin=model.begin;advance=model.advance;finish=model.finish;execution=model.execution
    def bad_execution(ms):execution(0 if fault=='missing_execution' else ms+1 if fault=='wrong_execution_duration' else ms)
    def bad_begin(req,route=0):
        start=model.clock_ms;deadline=model.ep.current_core.deadline
        out=begin(req,route)
        if req[3:6]==b'\4\0\x10':
            elapsed=model.clock_ms-start
            if fault=='consume_idle':model.ports[0]['deadline']-=elapsed
            if fault=='renew_lease_early':model.ep.current_core.deadline=model.clock_ms+model.ep.current_core.lease
            if fault=='consume_lease':model.ep.current_core.deadline=deadline
            if fault=='execution_changes_pin':model.extension.pins[1]=(3,0)
        return out
    def bad_advance(ms):
        p=model.ports[0].copy();waiting=model.pending is not None
        if waiting and fault=='writer_pauses_idle':model.ports[0]['deadline']+=ms
        if waiting and fault=='writer_pauses_lease':model.ep.current_core.deadline+=ms
        advance(ms)
        if not waiting and ms==2999 and fault=='idle_expiry_early':model.reset_port(0)
        if not waiting and ms==1 and fault=='idle_expiry_missing' and p['state']==2:model.ports[0]=p
    def bad_finish():
        q=model.pending['request'];idle=model.ports[0]['deadline'];lease=model.ep.current_core.deadline
        out=finish()
        if q[3:6]==b'\4\0\x10':
            if fault=='no_idle_send_renewal':model.ports[0]['deadline']=idle
            if fault=='no_lease_send_renewal':model.ep.current_core.deadline=lease
        return out
    model.execution=bad_execution;model.begin=bad_begin;model.advance=bad_advance;model.finish=bad_finish
    checks=PortSpeedExecutionChecks(c,Adapter(model));checks.cases=(('source',3500,60000),)
    row=checks.run()['checks'][-1]
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[('consume_verify','VERIFY'),('verify_renewed_by_answer','VERIFY'),
 ('other_route_consumes_idle','OTHER-ROUTE'),('other_route_renews_idle','OTHER-ROUTE'),('verify_expiry_early','VERIFY')])
def test_pause_faults_are_detected(fault,case):
    model,c=setup();begin=model.begin;finish=model.finish;advance=model.advance
    def bad_begin(req,route=0):
        start=model.clock_ms;out=begin(req,route)
        if req[3:6]==b'\4\0\x10' and fault in ('consume_verify','other_route_consumes_idle'):model.ports[0]['deadline']-=model.clock_ms-start
        return out
    def bad_finish():
        q=model.pending['request'];out=finish()
        if q[3:6]==b'\4\0\x10' and fault in ('verify_renewed_by_answer','other_route_renews_idle'):model.ports[0]['deadline']=model.clock_ms+1000
        return out
    def bad_advance(ms):
        p=model.ports[0];advance(ms)
        if fault=='verify_expiry_early' and p['state']==1 and model.ports[0] is p and p['deadline']-model.clock_ms==1:model.reset_port(0)
    model.begin=bad_begin;model.finish=bad_finish;model.advance=bad_advance
    checks=PortSpeedExecutionChecks(c,Adapter(model));checks.cases=(case,)
    row=checks.run()['checks'][-1]
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('guard',['execution','max_op_ms'])
def test_fixture_guards(guard):
    model,c=setup();a=Adapter(model)
    if guard=='execution':a.execution=None
    else:model.ep.static[0]=tuple(t for t in model.ep.static[0] if t[0]!=77)
    r=PortSpeedExecutionChecks(c,a).run()
    assert r['checks'][0]['status']=='failed'
