import pytest
from oep_client.conformance import Checks
from oep_client.conformance_uart import UartChecks
from oep_client.conformance_uart_sample import SampleUartAdapter
from oep_client.virtual_uart_model import UartModel
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,c=registered_setup();reg=c.reg;reg['limits']={**reg.get('limits',{}),'uart_baud_tolerance_pct':5,'uart_default_baud':115200};reg['constants']={'min_max_frame':64}
    for s in reg['interface']:
        if s['name']=='oep.probe.plan':s['tlv']={'plan_apply':{'role_assignment':16},'describe':{'plan_roles':64}}
        if s['name']=='oep.fixture.uart':s['tlv']={'configure':{'format':1},'describe':{'formats':64}}
    model=UartModel(boot_id=17);return model,Checks(model.handle,reg,'virtual-resource-1')


def test_uart_checks():
    model,c=setup();r=UartChecks(c,SampleUartAdapter(model)).run();assert r['status']=='passed',[(r['id'],r.get('error')) for r in r['checks'] if r['status']!='passed'];assert len(r['checks'])==29 and not r['full_conformance']


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==17


def test_uart_metadata():
    from oep_client.conformance_fixture_metadata import FixtureMetadataChecks
    _,c=setup();r=FixtureMetadataChecks(c,{2:'oep.fixture.uart'}).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==4


@pytest.mark.parametrize('kind',UartChecks.cases)
def test_wrong_wire_results_detected(kind):
    model,c=setup();original=model.handle
    def broken(request):
        reply=original(request)
        if int.from_bytes(request[3:5],'little'):
            if reply[3]==1:reply=reply[:4]+b'\1'+reply[5:]
            elif reply[4]==3:reply=reply[:4]+b'\x0b\0'
            else:reply=reply[:4]+b'\3'
        return reply
    c.send=broken;checker=UartChecks(c,SampleUartAdapter(model));checker.cases=(kind,)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-UART-'+kind)['status']=='failed'


def test_reconfigure_cannot_clear_received_data():
    model,c=setup();install=model.install
    def broken_install():
        install();e=model.extension;original=e.dispatch_request
        def broken(fn,op,payload,tags):
            value=original(fn,op,payload,tags)
            if fn==2 and op==16:e.stream.data.clear()
            return value
        e.dispatch_request=broken
    model.install=broken_install;checker=UartChecks(c,SampleUartAdapter(model));checker.cases=('RECONFIGURE',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-UART-RECONFIGURE')['status']=='failed'


@pytest.mark.parametrize('kind',['RECREATE','REPLACE','END','EXPIRY','FORCE'])
def test_uart_counter_reset_on_close_detected(kind):
    model,c=setup();install=model.install
    def broken_install():
        install();e=model.extension;original=e.close
        def broken(reason):
            original(reason);e.position=0;e.serial=0
        e.close=broken
    model.install=broken_install;checker=UartChecks(c,SampleUartAdapter(model));checker.cases=(kind,)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-UART-'+kind)['status']=='failed'


def test_uart_write_replay_cannot_requeue_bytes():
    model,c=setup();original=model.handle;seen=set()
    def broken(request):
        repeated=request in seen;seen.add(request);reply=original(request)
        if repeated and request[3:6]==b'\2\0\x15':model.extension.stream.tx.extend(request[12:])
        return reply
    c.send=broken;checker=UartChecks(c,SampleUartAdapter(model));checker.cases=('WRITE-REPLAY',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-UART-WRITE-REPLAY')['status']=='failed'


@pytest.mark.parametrize('invalid',['selection','revision','tolerance'])
def test_uart_preflight(invalid):
    model,c=setup();adapter=SampleUartAdapter(model)
    if invalid=='selection':adapter.selection={2:'oep.fixture.uart'}
    elif invalid=='revision':next(s for s in c.reg['interface'] if s['name']=='oep.fixture.uart')['revision']=2
    else:c.reg['limits']['uart_baud_tolerance_pct']=6
    assert UartChecks(c,adapter).run()['status']=='failed'


def test_unknown_success_response_tlv_allowed():
    model,c=setup();original=model.handle
    def extended(request):
        reply=original(request)
        if request[3:6] in (b'\1\0\x10',b'\1\0\x11',b'\2\0\x10',b'\2\0\x15',b'\2\0\x16') and reply[3:5]==b'\1\0':reply+=b'\x7f\1\0x'
        return reply
    c.send=extended;r=UartChecks(c,SampleUartAdapter(model)).run();assert r['status']=='passed',r['checks']
