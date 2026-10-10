import pytest
from oep_client.conformance import Checks
from oep_client.conformance_retry import RetryChecks
from oep_client.conformance_retry_sample import SampleRetryAdapter
from oep_client.virtual_retry_model import RetryModel
from test_conformance_debug import setup as debug_setup


def setup():
    _,c,_=debug_setup();model=RetryModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleRetryAdapter(model)


def test_retry_contracts_pass():
    _,c,a=setup();r=RetryChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==14 and not r['full_conformance']


@pytest.mark.parametrize('fault,case',[
    ('retry_host','READ'),('retry_host','WRITE'),('retry_host','RESUME'),
    ('retry_fault','FAULT'),('no_busy','BUSY-READ'),('no_busy','BUSY-WRITE'),
    ('busy_delivered','BUSY-EXHAUST'),('budget','BUSY-EXHAUST'),
    ('no_owned','OWNED-0'),('speed_saved','OWNED-1'),('drive','OWNED-1'),
    ('pullup','OWNED-1'),('wrong_idle','OWNED-1'),('drive','OWNED-EXHAUST'),
    ('repeat_partial','PROGRESS'),('early_packet','PREFLIGHT'),
])
def test_retry_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;transaction=e.transaction;packet=e.packet;rest=e.rest;dispatch=e.dispatch_request
        def broken_transaction(parent,kind,address,value=None,owned=False):
            if fault=='no_busy':return e.packet(parent,kind,address,value,e.connections[parent]['speed'])
            if fault=='no_owned':owned=False
            if fault=='retry_host':owned=True
            result=transaction(parent,kind,address,value,owned)
            if fault=='retry_host' and kind=='write' and result[0]==2 or fault=='retry_fault' and result[0]==3 or fault=='budget' and result[0]==1 or fault=='repeat_partial' and result[0]==2:
                result=transaction(parent,kind,address,value,owned)
            if fault=='speed_saved' and owned:e.connections[parent]['speed']//=2
            return result
        def broken_packet(parent,kind,address,value,speed):
            result=packet(parent,kind,address,value,speed)
            if fault=='busy_delivered' and result[0]==1 and kind=='write':e.writereg(parent,address,value);e.packets[-1]['delivered']=True
            return result
        def broken_rest(parent,failed):
            rest(parent,failed)
            if fault=='drive' and failed:e.levels[parent][0]['mode']='wire'
            if fault=='pullup' and failed:e.levels[parent][1]['pull']=1
            if fault=='wrong_idle' and not failed:e.levels[parent][1]['level']=1
        def broken_dispatch(fn,op,payload,tags):
            if fault=='early_packet' and fn==5 and op==16:e.packets.append(dict(invalid=True))
            return dispatch(fn,op,payload,tags)
        e.transaction=broken_transaction;e.packet=broken_packet;e.rest=broken_rest;e.dispatch_request=broken_dispatch
    model.install=faulty_install;r=RetryChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-RETRY-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','status','grammar','budget','cut'])
def test_retry_context_blocks(invalid):
    model,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='status':c.reg['status']['line']=3
    elif invalid=='grammar':c.reg['interface'][-1]['enum']['dmi_step']['read']=3
    else:
        state=a.state
        def broken():
            s=state();s['retry_limit' if invalid=='budget' else 'cut_policy']=3 if invalid=='budget' else 'automatic';return s
        a.state=broken
    r=RetryChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])


@pytest.mark.parametrize('method,args',[
    ('retry_plan',(True,[0])),('retry_plan',(999,[0])),('retry_plan',(1,[True])),
    ('retry_plan',(1,[4])),('retry_plan',(1,(0,))),('retry_idle',(1,True)),
    ('retry_idle',(1,2)),('retry_probe_read',(True,)),
])
def test_invalid_retry_controls_preserve_state(method,args):
    model,c,a=setup();checker=RetryChecks(c,a)
    # First run installs the schema; obtain a fresh owned parent without probing.
    checker.run();checker.fresh()
    with c.holding(3000) as sid:
        parent,_=checker.attach(sid);actual=(parent,)+args[1:] if args[0]==1 and type(args[0])is int else args
        before=model.retry_state()
        with pytest.raises(ValueError):getattr(model,method)(*actual)
        assert model.retry_state()==before


def test_retry_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41
