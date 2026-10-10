from oep_client.conformance import Checks
from oep_client.conformance_writer_lease import WriterLeaseChecks
from oep_client.conformance_writer_lease_sample import SampleWriterLeaseAdapter
from oep_client.virtual_writer_lease_model import WriterLeaseModel
from test_conformance_cut import setup as cut_setup


def setup():
    _,c,_=cut_setup();c.reg['common']['enum']['mark_detail_closed']['session_ended']=2
    model=WriterLeaseModel(boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleWriterLeaseAdapter(model)


def test_writer_lease_contracts_pass():
    _,c,a=setup();r=WriterLeaseChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==7 and not r['full_conformance']


import pytest


@pytest.mark.parametrize('fault,case',[
    ('accept_renew','DELIVERY'),('wait_pause','DELIVERY'),('cached_renew','DELIVERY'),
    ('no_execution_pause','EXECUTION'),('no_expiry','EXPIRED'),
    ('stale_token','EXPIRED'),('no_generation','EXPIRED'),
    ('resources_left','EXPIRED'),('parent_first','EXPIRED'),
    ('close_cause','EXPIRED'),('cut_cause','CUT-EXPIRED'),
    ('changed_hart','EXPIRED'),('drive_left','EXPIRED'),
    ('late_discard','EXPIRED'),
])
def test_writer_lease_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install;accept=model.accept_request;advance=model.writer_advance;flush=model.flush_response
    def faulty_install():
        install();core=model.ep.current_core;e=model.extension;sent=core.response_sent;release=core.release;close=e.close_connection;close_stream=e.close_stream
        if fault=='no_expiry':core.tick=lambda:None
        if fault=='resources_left':e.release=lambda:None
        def broken_sent(token,**kwargs):
            if fault=='cached_renew' and token is None and core.holder is not None:core.deadline=model.milliseconds+core.lease
            if fault=='stale_token' and token is not None and core.holder is None:core.holder=token[0]
            return sent(token,**kwargs)
        def broken_release():
            old=core.generation;release()
            if fault=='no_generation':core.generation=old
        def broken_close(parent):
            if fault=='changed_hart':e.targets[parent]['halted']=False
            close(parent)
            if fault=='drive_left':
                for pin in e.freed_pins:e.freed_pins[pin]['mode']='wire'
            if fault=='parent_first':
                i=next(i for i,r in enumerate(e.lifecycle) if r[:2]==('connection-close',parent));e.lifecycle.insert(0,e.lifecycle.pop(i))
        def broken_stream(rid):
            if fault=='cut_cause':
                releasing=e.releasing;e.releasing=False
                try:return close_stream(rid)
                finally:e.releasing=releasing
            close_stream(rid)
            if fault=='close_cause' and rid in e.archives:
                marks=e.archives[rid]['marks'];marks[-1]=marks[-1][:4]+(4,)
        core.response_sent=broken_sent;core.release=broken_release;e.close_connection=broken_close;e.close_stream=broken_stream
    model.install=faulty_install
    def broken_accept(req):
        old=model.execution_ms;result=accept(req);core=model.ep.current_core
        if fault=='accept_renew':core.deadline=model.milliseconds+core.lease
        if fault=='no_execution_pause':core.deadline-=old
        return result
    def broken_advance(ms):
        advance(ms)
        if fault=='wait_pause' and model.waiting is not None:model.ep.current_core.deadline+=ms
    def broken_flush():
        expired=model.ep.current_core.holder is None;token=model.waiting['token'];result=flush()
        if fault=='late_discard' and expired and token is not None and result is not None:return result[:3]+bytes((0,c.reasons['no_session']))
        return result
    model.accept_request=broken_accept;model.writer_advance=broken_advance;model.flush_response=broken_flush
    r=WriterLeaseChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='CORE-WRITER-LEASE-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','clock','budget','mark'])
def test_writer_lease_context_blocks(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='mark':c.reg['common']['enum']['mark_detail_closed']['session_ended']=4
    else:
        state=a.state
        def broken():
            result=state();result['clock_scope' if invalid=='clock' else 'max_op_ms']='wall-clock' if invalid=='clock' else 1;return result
        a.state=broken
    r=WriterLeaseChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])


@pytest.mark.parametrize('method,value',[
    ('writer_advance',True),('writer_advance',-1),('writer_advance',60001),
    ('writer_execution',True),('writer_execution',-1),('writer_execution',5001),
])
def test_invalid_writer_controls_preserve_state(method,value):
    model,_,_=setup();before=model.writer_lease_state()
    with pytest.raises(ValueError):getattr(model,method)(value)
    assert model.writer_lease_state()==before


def test_writer_lease_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41


def test_writer_lease_retains_wire_cut_contracts():
    from oep_client.conformance_cut import CutChecks
    _,c,a=setup();r=CutChecks(c,a).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==9


def test_existing_cut_fixture_uses_session_cause_on_expiry():
    from oep_client.virtual_cut_model import CutModel
    from oep_client.conformance_cut import CutChecks
    from oep_client.conformance_cut_sample import SampleCutAdapter
    milliseconds=[0];_,original,_=cut_setup();model=CutModel(lambda:milliseconds[0],boot_id=17)
    c=Checks(model.handle,original.reg,'virtual-resource-1');a=SampleCutAdapter(model);checker=CutChecks(c,a)
    assert checker.run()['status']=='passed';checker.fresh()
    with c.holding(1000) as sid:
        parent,rid=checker.prepare(sid);checker.failed(sid,parent);a.plan(parent,[2]);req=checker.dmi_request(sid,parent,[checker.write_reg(4,42)])
        a.accept(req)
        try:
            milliseconds[0]+=1000;a.tick();state=a.state()
            assert not state['connections'] and not state['streams']
            assert state['archives'][rid]['marks'][-1][2]==9 and state['archives'][rid]['marks'][-1][4]==2
        finally:
            c.exchange(req)
            if model.ep.current_core.holder is None:c.session=None
