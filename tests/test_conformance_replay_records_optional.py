"""core §5.1 (a09fa45): "要求・応答の再送用の記録は任意で、件数と byte 数に上限を置いてよく、0 件でもよい" - a probe that keeps
no byte records answers every replay rejected result_lost (from the order state it must keep), and each checker that
replays a request to see it is not run again accepts that as well as the recorded answer."""
import importlib
import pytest


def no_records(model):
    """The model as a probe without replay byte records: every recorded request and answer is longer than its 0-byte
    limit, so a replay is answered rejected result_lost from the kept order state (nothing runs again)."""
    model.ep.remember_max=0


def build(name):
    t=importlib.import_module('test_conformance_'+name)
    if name=='allocator':
        model,c,_,runner=t.setup();no_records(model);return runner
    if name=='dependencies':
        from oep_client.conformance_dependencies import DependencyChecks
        model,c,a,wait=t.setup();no_records(model);return DependencyChecks(c,a,wait=wait)
    if name=='capture_group':
        from oep_client.conformance_capture_group import CaptureGroupChecks
        from oep_client.conformance_capture_group_sample import SampleCaptureGroupAdapter
        model,c=t.setup();no_records(model);return CaptureGroupChecks(c,SampleCaptureGroupAdapter(model))
    if name=='arm_adi':
        from oep_client.conformance_arm_adi import ArmAdiChecks
        model,c,a=t.setup();no_records(model);return ArmAdiChecks(c,a)
    if name=='execution_order':
        model,runner=t.build();no_records(model);return runner
    if name=='lease':
        _,model,_,runner=t.build();no_records(model);return runner
    if name in ('data','resources','subscriptions'):
        model,_,_,runner=t.build();no_records(model);return runner
    if name=='link_routes':
        from oep_client.conformance_link_routes import LinkRoutesChecks
        from oep_client.conformance_link_routes_sample import SampleLinkRoutesAdapter
        model,c=t.setup();no_records(model);return LinkRoutesChecks(c,SampleLinkRoutesAdapter(model))
    if name=='link_transfers':
        from oep_client.conformance_link_transfers import LinkTransferChecks
        from oep_client.conformance_link_transfer_sample import SampleLinkTransferAdapter
        model,c=t.setup(64);no_records(model);return LinkTransferChecks(c,SampleLinkTransferAdapter(model))
    simple={'analog':('conformance_analog','AnalogChecks','conformance_analog_sample','SampleAnalogAdapter'),
            'logic':('conformance_logic','LogicChecks','conformance_logic_sample','SampleLogicAdapter'),
            'i2c':('conformance_i2c','I2cChecks','conformance_i2c_sample','SampleI2cAdapter'),
            'spi':('conformance_spi','SpiChecks','conformance_spi_sample','SampleSpiAdapter'),
            'uart':('conformance_uart','UartChecks','conformance_uart_sample','SampleUartAdapter')}
    if name in simple:
        mod,cls,smod,scls=simple[name];model,c=t.setup();no_records(model)
        return getattr(importlib.import_module('oep_client.'+mod),cls)(c,getattr(importlib.import_module('oep_client.'+smod),scls)(model))
    classes={'attach':'AttachChecks','arm_adi_blocks':'ArmAdiBlockChecks','cut':'CutChecks','debug':'DebugChecks',
             'dmseq':'DmseqChecks','blocks':'BlockChecks','mailbox':'MailboxChecks','outcomes':'OutcomeChecks',
             'reset':'ResetChecks','scan':'ScanChecks','retry':'RetryChecks','step':'StepChecks','run':'RunChecks',
             'scan_options':'ScanOptionsChecks','stream':'StreamChecks','wire_options':'WireOptionsChecks',
             'writer_lease':'WriterLeaseChecks'}
    model,c,a=t.setup();no_records(model)
    return getattr(importlib.import_module('oep_client.conformance_'+name),classes[name])(c,a)


# link_transfers / link_routes / retention and the replay pressure checks (lease) are left out: their explicit fixtures
# declare or instrument the replay records they keep, and those checkers check that declaration.
NAMES=['allocator','analog','attach','arm_adi','arm_adi_blocks','capture_group','cut','debug','dmseq','dependencies',
       'blocks','mailbox','logic','outcomes','i2c','reset','scan','retry','step','run','scan_options','uart','spi',
       'stream','wire_options','writer_lease','execution_order','data','resources','subscriptions']


@pytest.mark.parametrize('name',NAMES)
def test_replay_without_records_passes(name):
    r=build(name).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']


def test_timeline_lifecycle_replay_without_records_passes():
    from test_conformance_capture_timeline_lifecycle import setup,run
    from oep_client.conformance_capture_timeline_lifecycle import CaptureTimelineLifecycleChecks
    for scenario in CaptureTimelineLifecycleChecks.scenarios:
        model,checks=setup();no_records(model);r=run(model,checks,scenario)
        assert r['status']=='passed',(scenario,[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed'])
