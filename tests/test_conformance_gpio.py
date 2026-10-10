import pytest
from oep_client.conformance import Checks
from oep_client.conformance_gpio import GpioChecks
from oep_client.conformance_gpio_sample import SampleGpioAdapter
from oep_client.virtual_gpio_model import GpioModel
from test_conformance_registered_ops import setup as registered_setup


def setup(selectable=True):
    _,original=registered_setup();reg=original.reg
    for s in reg['interface']:
        if s['name']=='oep.probe.plan':s['tlv']={'plan_apply':{'role_assignment':16},'describe':{'plan_roles':64}}
        if s['name']=='oep.fixture.gpio':s['tlv']={'set':{'drive':1},'unavailable_payload':{'index':64},'describe':{'modes':64,'drive_levels':65}}
    model=GpioModel(lambda:0,boot_id=17,selectable=selectable);return model,Checks(model.handle,reg,'virtual-resource-1')


@pytest.mark.parametrize('selectable',[True,False])
def test_gpio_plan_checks(selectable):
    model,c=setup(selectable);r=GpioChecks(c,SampleGpioAdapter(model)).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==28 and not r['full_conformance']


def test_generic_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==25


def test_gpio_metadata():
    from oep_client.conformance_fixture_metadata import FixtureMetadataChecks
    _,c=setup();c.reg['constants']={'min_max_frame':64};r=FixtureMetadataChecks(c,{2:'oep.fixture.gpio',3:'oep.fixture.gpio'}).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==7


@pytest.mark.parametrize('kind',GpioChecks.cases)
def test_checker_rejects_wrong_wire_results(kind):
    model,c=setup();original=model.handle
    def broken(request):
        reply=original(request)
        if int.from_bytes(request[3:5],'little'):
            if reply[3]==1:reply=reply[:4]+b'\1'+reply[5:]
            elif reply[4]==3:reply=reply[:4]+b'\x0b\0'
            else:reply=reply[:4]+b'\3'
        return reply
    c.send=broken;checker=GpioChecks(c,SampleGpioAdapter(model));checker.cases=(kind,)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-GPIO-'+kind)['status']=='failed',r['checks']


@pytest.mark.parametrize('kind',['PLAN-REPLACE','RELEASE-ONE','RELEASE-ALL','END'])
def test_checker_detects_missing_idle_restore(kind):
    model,c=setup();install=model.install
    def broken_install():
        install();model.extension.restore=lambda ch:None
    model.install=broken_install;checker=GpioChecks(c,SampleGpioAdapter(model));checker.cases=(kind,)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-GPIO-'+kind)['status']=='failed',r['checks']


def test_checker_detects_reversed_set_execution():
    model,c=setup();install=model.install
    def broken_install():
        install();original=model.extension.dispatch_request
        def reversed_order(fn,op,payload,tags):
            if fn==2 and op==16 and payload[0]==2:payload=payload[:1]+payload[4:7]+payload[1:4]
            return original(fn,op,payload,tags)
        model.extension.dispatch_request=reversed_order
    model.install=broken_install;checker=GpioChecks(c,SampleGpioAdapter(model));checker.cases=('SET-ORDER',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-GPIO-SET-ORDER')['status']=='failed'


def test_checker_detects_admission_side_effect():
    from oep_client.virtual_core import Reject
    model,c=setup();install=model.install
    def broken_install():
        install();original=model.extension.dispatch_request
        def partial(fn,op,payload,tags):
            try:return original(fn,op,payload,tags)
            except Reject:
                model.extension.events.append(('premature-write',0));raise
        model.extension.dispatch_request=partial
    model.install=broken_install;checker=GpioChecks(c,SampleGpioAdapter(model));checker.cases=('SET-UNAVAILABLE',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-GPIO-SET-UNAVAILABLE')['status']=='failed'


def test_checker_detects_replayed_pin_writes():
    model,c=setup();original=model.handle;seen=set()
    def repeated(request):
        replay=request in seen;seen.add(request);reply=original(request)
        if replay and request[3:6]==b'\2\0\x10':
            n=request[10];payload=request[10:11+3*n];model.extension.dispatch_request(2,16,payload,[])
        return reply
    c.send=repeated;checker=GpioChecks(c,SampleGpioAdapter(model));checker.cases=('REPLAY',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='OEP-GPIO-REPLAY')['status']=='failed'


@pytest.mark.parametrize('invalid',['selection','revision','tag'])
def test_gpio_context_preflight(invalid):
    model,c=setup();adapter=SampleGpioAdapter(model)
    if invalid=='selection':adapter.selection={2:'oep.fixture.gpio'}
    elif invalid=='revision':next(s for s in c.reg['interface'] if s['name']=='oep.fixture.gpio')['revision']=2
    else:next(s for s in c.reg['interface'] if s['name']=='oep.probe.plan')['tlv']['plan_apply']['role_assignment']=17
    r=GpioChecks(c,adapter).run();assert r['status']=='failed'


def test_unknown_success_response_tlv_is_allowed():
    model,c=setup();original=model.handle
    def extended(request):
        reply=original(request)
        if int.from_bytes(request[3:5],'little') and reply[3:5]==b'\1\0':reply+=b'\x7f\1\0x'
        return reply
    c.send=extended;r=GpioChecks(c,SampleGpioAdapter(model)).run();assert r['status']=='passed',r['checks']
