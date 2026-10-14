from oep_client.conformance import Checks
from oep_client.conformance_config_power_loss import ConfigPowerLossChecks
from oep_client.conformance_config_power_loss_sample import SampleConfigPowerLossAdapter
from oep_client.virtual_config_power_loss_model import ConfigPowerLossModel
from test_conformance_registered_ops import setup as base_setup
import pytest


def setup():
    _,base=base_setup();model=ConfigPowerLossModel()
    return model,Checks(model.handle,base.reg,'virtual-resource-1')


def test_all_durable_write_boundaries_restore_whole_config():
    model,c=setup();r=ConfigPowerLossChecks(c,SampleConfigPowerLossAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
    assert len(r['checks'])==7 and len(r['state_observations'])==4 and not r['full_conformance']
    import json
    json.dumps(r)


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',ConfigPowerLossChecks.cases)
def test_wrong_recovered_page_is_detected(case):
    model,c=setup();a=SampleConfigPowerLossAdapter(model);send=c.send
    # prepare occurs once to measure the record, then once for every cut.
    active=None;original=ConfigPowerLossChecks.case
    checker=ConfigPowerLossChecks(c,a)
    def run_case(name):
        nonlocal active
        active=name;return original(checker,name)
    def broken(req):
        out=send(req)
        if active==case and req[3:6]==b'\4\0\x10':return out[:3]+b'\1\1'+out[5:]
        return out
    checker.case=run_case;c.send=broken;r=checker.run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-POWER-LOSS-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
    ('overwrite_active','REPLACE'),('erase_active','REUSE'),('choose_old','ACK'),('older_record','REUSE'),
    ('missing_commit','ACK'),('corrupt_crc','ACK'),('partial_items','REPLACE'),
    ('wrong_idle','FIRST'),('keep_live_plan','REPLACE'),('lost_disable','REPLACE'),('wrong_generation','REUSE'),('reuse_boot','FIRST'),
    ('early_response','FIRST'),('recovery_writes','FIRST'),('old_session','FIRST'),
    ('old_history','FIRST'),('old_cache','FIRST'),
    ('resume_partial','REPLACE'),('ghost_new','REPLACE')])
def test_power_loss_faults_are_detected(fault,case):
    model,c=setup();a=SampleConfigPowerLossAdapter(model);checker=ConfigPowerLossChecks(c,a);original_case=checker.case
    active=None;begin=model.journal.begin;recover=model.journal.recover;step=model.journal.step
    cut=a.cut_save;power=a.power_cycle;restart=a.restart_fixture;after_cut=False;unfinished=None
    def run_case(name):
        nonlocal active
        active=name;return original_case(name)
    def bad_begin(items):
        begin(items)
        if active==case and fault=='overwrite_active':
            p=model.journal.pending;p['target']=model.journal.active;model.journal.cells[p['target']]=bytearray()
        if active==case and fault=='erase_active':model.journal.cells[model.journal.active]=bytearray()
    def bad_step():
        step()
        if active==case and model.journal.pending is None:
            cell=model.journal.cells[model.journal.active]
            if fault=='missing_commit':cell[-1]=0
            if fault=='corrupt_crc':cell[-2]^=1
    def bad_recover():
        pending=model.journal.pending
        if active==case and fault=='ghost_new' and pending is not None:
            row=model.journal.decode(pending['record']);model.journal.pending=None
            return row[1]
        blob=recover()
        if active==case and fault=='older_record' and pending is not None:
            from oep_client.conformance_config_power_loss import OLDER
            return list(OLDER)
        if active==case and fault=='choose_old':
            rows=[row for cell in model.journal.cells if (row:=model.journal.decode(cell)) is not None]
            if rows:return min(rows,key=lambda row:row[0])[1]
        return blob
    def tamper(event,holder):
        if active!=case:return event
        if fault=='partial_items':model.extension.items[(2,4)]=b'\4\0mixed'
        if fault=='wrong_idle':model.extension.pins[1]=4
        if fault=='keep_live_plan':model.extension.plans={2:[3]}
        if fault=='lost_disable':model.extension.pins[6]=model.extension.pins[7]=0
        if fault=='wrong_generation':model.extension.generation=2
        if fault=='reuse_boot':model.ep.boot_id=event['old_boot_id'];event['boot_id']=event['old_boot_id']
        if fault=='early_response':event['reply_hex']='0201000100'
        if fault=='recovery_writes':model.journal.cells[0].append(0)
        if fault=='old_session':model.ep.current_core.holder=holder
        if fault=='old_history':model.ep.current_core.last=holder
        if fault=='old_cache':model.ep.current_core.cache[1]=(b'',b'')
        return event
    def bad_cut(req,at):
        nonlocal after_cut,unfinished
        if active==case and fault=='resume_partial':
            unfinished=[(t,v) for (t,ch),v in sorted(model.extension.items.items())]
        holder=model.ep.current_core.holder;r=cut(req,at);after_cut=True;return tamper(r,holder)
    def bad_power():
        holder=model.ep.current_core.holder;r=power();return tamper(r,holder)
    def bad_restart():
        nonlocal after_cut
        if active==case and fault=='resume_partial' and after_cut:
            model.journal.begin(unfinished)
            while model.journal.pending is not None:model.journal.step()
        after_cut=False;return restart()
    checker.case=run_case;model.journal.begin=bad_begin;model.journal.step=bad_step;model.journal.recover=bad_recover
    a.cut_save=bad_cut;a.power_cycle=bad_power;a.restart_fixture=bad_restart
    r=checker.run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-POWER-LOSS-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','generation_at','prepare','write_steps','cut_save','power_cycle'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigPowerLossAdapter(model);setattr(a,missing,None)
    r=ConfigPowerLossChecks(c,a).run();assert r['checks'][2]['status']=='failed'
