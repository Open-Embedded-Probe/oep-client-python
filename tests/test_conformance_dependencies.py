import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_dependencies import DependencyChecks
from oep_client.conformance_dependency_sample import SampleDependencyAdapter
from oep_client.virtual_dependency_model import DependencyModel
from test_conformance import REG
from test_conformance_oep_declarations import SPECS


def setup():
    reg=copy.deepcopy(REG);reg['common']={'enum':{'mark_kind':{'lost':1,'clear':2,'host':3}}}
    reg['interface']=[copy.deepcopy(s) for s in SPECS if s['name']=='oep.target.console']   # restart mark: console's own kind
    now=[0];model=DependencyModel(lambda:now[0],boot_id=17);c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c,SampleDependencyAdapter(model),lambda seconds:now.__setitem__(0,now[0]+int(seconds*1000))


def test_dependency_contracts_pass():
    _,c,a,wait=setup();r=DependencyChecks(c,a,wait=wait).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==19 and not r['full_conformance']
    assert all(x['level']=='interface' for x in r['checks'])


@pytest.mark.parametrize('defect',['parent_first','orphan','close_parent','clear_tx','reset_drops','reuse_id','implicit_last','read_consume','retain_end'])
def test_dependency_mutants_fail(defect):
    model,c,a,wait=setup();restart=model.restart_fixture
    def corrupt():
        result=restart();e=model.extension;dispatch=e.dispatch;close=e.close_connection
        def bad_close(rid):
            if defect=='parent_first':
                start=len(e.events);close(rid);e.events[start:]=sorted(e.events[start:])
            elif defect=='orphan':e.connections.remove(rid);e.events.append(('connection',rid))
            else:close(rid)
        e.close_connection=bad_close
        def bad_dispatch(fn,op,payload):
            if defect=='implicit_last' and fn==2 and op==17 and e.streams:payload=max(e.streams).to_bytes(4,'little')+payload[4:]
            result=dispatch(fn,op,payload)
            if fn==2 and op==22 and defect=='close_parent':e.release()
            if fn==2 and op==19 and defect=='clear_tx':e.streams[int.from_bytes(payload,'little')][1].tx.clear()
            if fn==2 and op==17 and defect=='read_consume':e.streams[int.from_bytes(payload[:4],'little')][1].data.clear()
            if defect=='reuse_id' and fn==2 and op==22:e.next_id-=1
            return result
        e.dispatch=bad_dispatch
        if defect=='reset_drops':
            reset=model.reset_target
            def bad_reset(rid):
                reset(rid)
                for parent,s in e.streams.values():
                    if parent==rid:s.data.clear()
            model.reset_target=bad_reset
        if defect=='retain_end':e.release=lambda:None
        return result
    model.restart_fixture=corrupt;r=DependencyChecks(c,a,wait=wait).run()
    assert r['status']=='failed' and any(x['status']=='failed' for x in r['checks'][1:])


@pytest.mark.parametrize('invalid',['component','addressing','ops','scope','enum'])
def test_adoption_preflight_blocks_invalid_configuration(invalid):
    _,c,a,wait=setup();a.adoption=copy.deepcopy(a.adoption)
    if invalid=='component':a.adoption['connection']['component']='unknown'
    elif invalid=='addressing':a.adoption['stream']['addressing']='persistent-fn'
    elif invalid=='ops':a.adoption['stream']['ops'].pop('write')
    elif invalid=='scope':a.reset_scope='physical'
    else:c.reg['common']['enum']['mark_kind']['clear']=6
    r=DependencyChecks(c,a,wait=wait).run()
    assert r['checks'][0]['status']=='failed'
    assert not c.trace and not a.trace and all(x['status']=='blocked' for x in r['checks'][1:])


def test_dependency_report_excludes_physical_and_standard_operation_verdicts():
    _,c,a,wait=setup();r=DependencyChecks(c,a,wait=wait).run()
    assert r['levels']['oep-interface']=='not executed'
    assert 'physical release/pin idle' in r['unchecked']


def test_logical_restart_is_rejected_while_owned():
    model,c,a,_=setup();c.confirm();c.identity()
    with c.holding(3000):
        before=model.dependency_state()
        with pytest.raises(ValueError):a.restart_fixture()
        assert model.dependency_state()==before


@pytest.mark.parametrize('defect',['marks_wrong_resource','detach_twice','missing_ops'])
def test_wire_dependency_defects_fail(defect):
    model,c,a,wait=setup();restart=model.restart_fixture
    def corrupt():
        result=restart();e=model.extension;dispatch=e.dispatch
        def bad(fn,op,payload):
            if defect=='marks_wrong_resource' and fn==2 and op==18 and e.streams:
                payload=max(e.streams).to_bytes(4,'little')+payload[4:]
            if defect=='detach_twice' and fn==1 and op==17 and int.from_bytes(payload,'little') not in e.connections:
                from oep_client.virtual_core import Reject
                raise Reject(10)
            return dispatch(fn,op,payload)
        e.dispatch=bad
        if defect=='detach_twice':
            core=model.ep.current_core;handle=core.handle
            def replay(request,route,**kwargs):
                corr=int.from_bytes(request[1:3],'little')
                if request[3:6]==b'\x01\0\x11' and corr in core.cache:core.cache.pop(corr);core.high=corr-1
                return handle(request,route,**kwargs)
            core.handle=replay
        return result
    if defect=='missing_ops':model.extension.describe=lambda fn:() if fn==2 else ()
    else:model.restart_fixture=corrupt
    r=DependencyChecks(c,a,wait=wait).run()
    assert r['status']=='failed'


@pytest.mark.parametrize('operation',[(2,19),(2,22),(1,17)])
def test_empty_completion_payload_requires_well_formed_tlvs(operation):
    model,c,a,wait=setup();restart=model.restart_fixture
    def corrupt():
        result=restart();e=model.extension;dispatch=e.dispatch
        def bad(fn,op,payload):
            value=dispatch(fn,op,payload)
            return b'\x7f' if (fn,op)==operation else value
        e.dispatch=bad;return result
    model.restart_fixture=corrupt;r=DependencyChecks(c,a,wait=wait).run()
    assert r['status']=='failed' and any(x['status']=='failed' for x in r['checks'][1:])
