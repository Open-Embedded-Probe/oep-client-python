from oep_client.conformance import Checks
from oep_client.conformance_cut import CutChecks
from oep_client.conformance_cut_sample import SampleCutAdapter
from oep_client.virtual_cut_model import CutModel
from test_conformance_debug import setup as debug_setup


def setup():
    _,c,_=debug_setup();c.reg.setdefault('common',{}).setdefault('enum',{}).update(mark_kind={'link_lost':8,'closed':9},mark_detail_closed={'connection_closed':4});model=CutModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleCutAdapter(model)


def test_cut_contracts_pass():
    _,c,a=setup();r=CutChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==9 and not r['full_conformance']


import pytest
from oep_client.virtual_core import Completed, Reject


@pytest.mark.parametrize('fault,case',[
    ('early_close','REQUEST'),('early_close','REQUEST-HALTED'),
    ('no_cut','REQUEST'),('single_line','CONSOLE'),('no_link_mark','CONSOLE'),
    ('mark_order','CONSOLE'),('mark_position','CONSOLE'),('closed_cause','CONSOLE'),
    ('drive_left','REQUEST'),('changed_hart','REQUEST-HALTED'),
    ('parent_first','REQUEST'),('response_last','REQUEST'),
    ('no_recovery','RECOVERY'),('timer_cut','RECOVERY'),
    ('other_closed','ISOLATED'),('rejection_cut','REJECTED'),
    ('partial_done','REQUEST-PARTIAL'),('replay_access','REQUEST'),
])
def test_cut_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;decide=e.decide;close=e.close_connection;streamclose=e.close_stream;observe=e.observe;dispatch=e.dispatch_request
        def broken_decide(parent,source):
            if fault=='no_cut':return False
            if fault=='single_line':e.streak[parent]=2
            result=decide(parent,source)
            if fault=='early_close' and result and source=='request':e.close_connection(parent)
            return result
        def broken_close(parent):
            if fault=='changed_hart':e.targets[parent]['halted']=False
            close(parent)
            if fault=='drive_left':
                for pin in e.freed_pins:e.freed_pins[pin]['mode']='wire'
            if fault in ('parent_first','response_last'):
                rows=e.lifecycle;label='connection-close' if fault=='parent_first' else 'response-write';index=next((i for i,r in enumerate(rows) if r[0]==label and (label=='response-write' or r[1]==parent)),None)
                if index is not None:
                    row=rows.pop(index);rows.insert(0,row) if fault=='parent_first' else rows.append(row)
            if fault=='other_closed':
                for other in list(e.connections):close(other)
        def broken_streamclose(rid):
            stream=e.streams[rid][1]
            if fault=='no_link_mark':stream.marks=[r for r in stream.marks if r[2]!=8];e.lifecycle=[r for r in e.lifecycle if not(r[0]=='mark' and r[2]==8)]
            streamclose(rid)
            if rid in e.archives:
                marks=e.archives[rid]['marks']
                if fault=='mark_order':marks.reverse()
                elif fault=='mark_position' and len(marks)>1:marks[-2]=(marks[-2][0],marks[-2][1]+1)+marks[-2][2:]
                elif fault=='closed_cause':marks[-1]=marks[-1][:4]+(2,)
        def broken_observe(parent,status):
            if fault=='no_recovery' and status==0:return
            observe(parent,status)
        def broken_dispatch(fn,op,payload,tags):
            try:result=dispatch(fn,op,payload,tags)
            except Reject:
                if fault=='rejection_cut' and fn==5:
                    parent=int.from_bytes(payload[:2],'little');e.streak[parent]=2;e.decide(parent,'request')
                raise
            if fault=='partial_done' and fn==5 and op==16 and result[:2]==b'\x01\0' and result[2]==2:return Completed(b'\0\0'+result[2:],result.outcome)
            return result
        e.decide=broken_decide;e.close_connection=broken_close;e.close_stream=broken_streamclose;e.observe=broken_observe;e.dispatch_request=broken_dispatch
    model.install=faulty_install
    if fault=='timer_cut':
        def broken_tick():
            for parent in list(model.extension.connections):model.extension.close_connection(parent)
        model.cut_tick=broken_tick
    if fault=='replay_access':
        handle=model.handle
        def broken_handle(req):
            e=model.extension;cached=any(row[0]=='response-write' and row[1]==int.from_bytes(req[1:3],'little') for row in e.lifecycle)
            result=handle(req)
            if cached:e.packets.append(dict(replayed=True))
            return result
        c.send=broken_handle
    r=CutChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-CUT-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','status','policy','grammar','marks'])
def test_cut_context_blocks(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='status':c.reg['status']['line']=3
    elif invalid=='grammar':c.reg['interface'][-1]['enum']['dmi_step']['write']=7
    elif invalid=='marks':c.reg['common']['enum']['mark_kind']['link_lost']=7
    else:
        state=a.state
        def broken():
            result=state();result['cut_policy']='timer';return result
        a.state=broken
    r=CutChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])


@pytest.mark.parametrize('method,args',[
    ('accept_request',('bad',)),('cut_console_poll',(True,)),('cut_console_poll',(999,)),
])
def test_invalid_cut_controls_preserve_state(method,args):
    model,_,_=setup();before=model.cut_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.cut_state()==before


def test_busy_writer_rejects_other_request_without_mutation():
    model,c,a=setup();checker=CutChecks(c,a);checker.run();checker.fresh()
    with c.holding(3000) as sid:
        parent,rid=checker.prepare(sid);req=checker.dmi_request(sid,parent,[checker.read(4)]);a.accept(req)
        try:
            before=model.cut_state()
            for call in (lambda:model.accept_request(req),lambda:model.handle(req+b'x'),lambda:model.cut_console_poll(rid)):
                with pytest.raises(ValueError):call()
                assert model.cut_state()==before
        finally:c.exchange(req)


def test_cut_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41
