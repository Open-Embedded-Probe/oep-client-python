import pytest
from oep_client.conformance import Checks
from oep_client.conformance_gpio_lifecycle import GpioLifecycleChecks
from oep_client.conformance_gpio_lifecycle_sample import SampleGpioLifecycleAdapter
from oep_client.virtual_gpio_lifecycle_model import GpioLifecycleModel
from test_conformance_gpio import setup as gpio_setup


def setup():
    _,c=gpio_setup();model=GpioLifecycleModel(boot_id=17);return model,Checks(model.handle,c.reg,'virtual-resource-1')


def test_gpio_lifecycle_checks():
    model,c=setup();r=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model)).run();assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed'];assert len(r['checks'])==11 and not r['full_conformance'];assert all(row['level']=='core' for row in r['checks'])


def test_gpio_operation_backward_checks():
    from oep_client.conformance_gpio import GpioChecks
    model,c=setup();r=GpioChecks(c,SampleGpioLifecycleAdapter(model)).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==25


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==25


@pytest.mark.parametrize('kind',['EXPIRY','EXPIRED-SET-REPLAY','EXPIRED-PLAN-REPLAY','ENDED-ID','FORCE','FORCE-OLD'])
def test_checker_detects_pin_release_omission(kind):
    model,c=setup();install=model.install
    def broken_install():
        install();model.extension.restore=lambda ch:None
    model.install=broken_install;checker=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model));checker.cases=(kind,)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='CORE-GPIO-LIFECYCLE-'+kind)['status']=='failed',r['checks']


@pytest.mark.parametrize('kind',['KEEPALIVE','REJECTED-RENEWAL'])
def test_checker_detects_failure_to_renew(kind):
    model,c=setup();install=model.install
    def broken_install():
        install();model.ep.current_core.response_sent=lambda *args,**kwargs:None
    model.install=broken_install;checker=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model));checker.cases=(kind,)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='CORE-GPIO-LIFECYCLE-'+kind)['status']=='failed',r['checks']


def test_same_id_open_cannot_release_gpio():
    model,c=setup();original=model.handle
    def broken(request):
        if request[3:6]==b'\0\0\x10' and int.from_bytes(request[6:10],'little')==model.ep.current_core.holder:model.extension.release()
        return original(request)
    c.send=broken;checker=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model));checker.cases=('SAME-ID-OPEN',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='CORE-GPIO-LIFECYCLE-SAME-ID-OPEN')['status']=='failed'


def test_sid_zero_read_must_not_renew_gpio_lease():
    model,c=setup();original=model.handle
    def broken(request):
        reply=original(request)
        if request[3:6]==b'\2\0\x11' and request[6:10]==b'\0'*4:model.ep.current_core.deadline=model.milliseconds+1000
        return reply
    c.send=broken;checker=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model));checker.cases=('READ-REPLAY-NONRENEWAL',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='CORE-GPIO-LIFECYCLE-READ-REPLAY-NONRENEWAL')['status']=='failed'


@pytest.mark.parametrize('value',[-1,60001,True,1.5])
def test_clock_control_rejects_invalid_values(value):
    model,_=setup()
    with pytest.raises(ValueError):model.gpio_advance(value)
    assert model.milliseconds==0


@pytest.mark.parametrize('kind',['EXPIRED-SET-REPLAY','EXPIRED-PLAN-REPLAY'])
def test_expired_cached_success_cannot_reexecute_gpio(kind):
    model,c=setup();original=model.handle
    def broken(request):
        reply=original(request)
        if model.ep.current_core.holder is None and reply[3:5]==b'\1\0':
            if request[3:6]==b'\1\0\x10':model.extension.dispatch_request(1,16,b'',model.extension.request_tags(1,16,request[10:]))
            elif request[3:6]==b'\2\0\x10':model.extension.pins[0]=(3,0);model.extension.events.append(('set',0,3,0))
        return reply
    c.send=broken;checker=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model));checker.cases=(kind,)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='CORE-GPIO-LIFECYCLE-'+kind)['status']=='failed'


def test_same_id_open_must_keep_gpio_replay_history():
    model,c=setup();original=model.handle
    def broken(request):
        same=request[3:6]==b'\0\0\x10' and int.from_bytes(request[6:10],'little')==model.ep.current_core.holder
        reply=original(request)
        if same:model.ep.current_core.cache.clear()
        return reply
    c.send=broken;checker=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model));checker.cases=('SAME-ID-OPEN',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='CORE-GPIO-LIFECYCLE-SAME-ID-OPEN')['status']=='failed'


def test_gpio_expiry_must_not_happen_one_ms_early():
    model,c=setup();install=model.install
    def broken_install():
        install();core=model.ep.current_core;original=core.tick
        def early():
            if core.holder is not None and model.milliseconds>=core.deadline-1:core.release()
            else:original()
        core.tick=early
    model.install=broken_install;checker=GpioLifecycleChecks(c,SampleGpioLifecycleAdapter(model));checker.cases=('EXPIRY',)
    r=checker.run();assert next(row for row in r['checks'] if row['id']=='CORE-GPIO-LIFECYCLE-EXPIRY')['status']=='failed'


@pytest.mark.parametrize('invalid',['selection','revision','clock'])
def test_lifecycle_context_preflight(invalid):
    model,c=setup();adapter=SampleGpioLifecycleAdapter(model)
    if invalid=='selection':adapter.selection={2:'oep.fixture.gpio'}
    elif invalid=='revision':next(s for s in c.reg['interface'] if s['name']=='oep.fixture.gpio')['revision']=2
    else:
        original=model.gpio_lifecycle_state
        def state():
            result=original();result['clock_scope']='wall-clock';return result
        model.gpio_lifecycle_state=state
    r=GpioLifecycleChecks(c,adapter).run();assert r['status']=='failed';assert all(row['status']=='blocked' for row in r['checks'][1:])
