from oep_client.conformance import Checks
from oep_client.conformance_restart import RestartChecks
from oep_client.conformance_restart_sample import SampleRestartAdapter
from oep_client.virtual_restart_model import RestartModel
from test_conformance_registered_ops import setup as base_setup


def setup():
    _,base=base_setup();model=RestartModel(boot_id=17)
    return model,Checks(model.handle,base.reg,'virtual-resource-1')


def test_restart_contract():
    model,c=setup();r=RestartChecks(c,SampleRestartAdapter(model)).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==6 and not r['full_conformance']


import pytest


def test_restart_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    model,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',RestartChecks.cases)
def test_bad_wire_response_is_detected(case):
    model,c=setup();begin=model.begin
    def bad(req,route=0):
        out=begin(req,route)
        if req[3:6]==b'\4\0\x10':out=out[:3]+b'\1\1'+out[5:];model.pending['reply']=out
        return out
    model.begin=bad;r=RestartChecks(c,SampleRestartAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-RESTART-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
 ('boot_before_write','BARRIER'),('never_boot','BARRIER'),('reuse_boot','BARRIER'),
 ('keep_plan','BARRIER'),('keep_pin','BARRIER'),('keep_cache','BARRIER'),
 ('keep_owner','BARRIER'),('bad_uptime','BARRIER'),('quiet_response','BARRIER'),
 ('missing_route','BARRIER'),('wrong_critical_tag','TLV'),('refusal_changes_pin','LOCK')])
def test_state_faults_are_detected(fault,case):
    model,c=setup();a=SampleRestartAdapter(model);begin=model.begin;boot=model.complete_boot;advance=model.advance;raw=a.raw
    def bad_begin(req,route=0):
        out=begin(req,route)
        if req[3:6]==b'\4\0\x10':
            if fault=='boot_before_write' and out[3:5]==b'\1\0':model.complete_boot()
            if fault=='wrong_critical_tag' and out[3:5]==b'\0\x0b':out=out[:5]+b'\x7f';model.pending['reply']=out
            if fault=='refusal_changes_pin' and out[3:5]==b'\0\10':model.extension.pins[1]=(3,0)
        return out
    def bad_boot():
        old=model.ep.current_core;gpio=model.gpio_state();boot_id=model.ep.boot_id
        boot()
        if fault=='reuse_boot':model.ep.boot_id=boot_id
        if fault=='keep_plan':model.extension.plans=gpio['plans']
        if fault=='keep_pin':model.extension.pins[1]=(4,0)
        if fault=='keep_cache':model.ep.current_core.cache=old.cache;model.ep.current_core.last=old.last
        if fault=='keep_owner':model.ep.current_core.holder=old.holder
        if fault=='bad_uptime':model.ep._origin_ns-=51000000   # an uptime past the 50 ms since the answer went out
    def bad_advance(delta):
        if fault=='never_boot' and model.return_at is not None:model.clock_ms+=delta
        else:advance(delta)
    def bad_raw(req,route):
        if fault=='quiet_response' and model.return_at is not None:return model.ep.current_core.handle(req,route)
        if fault=='missing_route' and route==1 and any(e['action']=='boot' for e in model.events):return None
        return raw(req,route)
    model.begin=bad_begin;model.complete_boot=bad_boot;model.advance=bad_advance;a.raw=bad_raw
    r=RestartChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-RESTART-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['begin','finish','advance','raw','events','seed','restart_fixture'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleRestartAdapter(model);setattr(a,missing,None)
    r=RestartChecks(c,a).run();assert r['checks'][2]['status']=='failed'


def test_return_delay_is_an_explicit_fixture_condition():
    model,c=setup();a=SampleRestartAdapter(model);a.restart_delay_ms=0
    r=RestartChecks(c,a).run();assert r['checks'][2]['status']=='failed'


@pytest.mark.parametrize('fault',['failed_clock','wrong_clock_boot'])
def test_restarted_clock_shape_and_identity(fault):
    model,c=setup();begin=model.begin
    def bad(req,route=0):
        out=begin(req,route)
        if req[3:6]==b'\0\0\4':
            if fault=='failed_clock':out=out[:3]+b'\1\1'+out[5:]
            else:out=out[:5]+b'\0'*4+out[9:]
            model.pending['reply']=out
        return out
    model.begin=bad;r=RestartChecks(c,SampleRestartAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-RESTART-BARRIER')
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row
