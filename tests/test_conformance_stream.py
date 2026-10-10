import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_stream import StreamChecks
from oep_client.conformance_stream_sample import SampleStreamAdapter
from oep_client.virtual_stream_model import StreamModel
from test_conformance import REG


def setup():
    reg=copy.deepcopy(REG)
    reg['common']={'enum':{'read_from':{'position':0,'oldest':1,'now':2,'last_mark':3},'read_flags':{'more':1,'gap':2},
                          'mark_kind':{'reset':1,'lost':5,'clear':6,'host':7},'mark_detail_reset':{'ndmreset':1},
                          'mark_detail_lost':{'overflow':1}}}
    now=[0];model=StreamModel(lambda:now[0]);c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c,SampleStreamAdapter(model)


def test_stream_contracts_pass():
    _,c,a=setup();report=StreamChecks(c,a).run()
    assert report['status']=='passed',[(r['id'],r.get('error')) for r in report['checks']]
    assert len(report['checks'])==15 and not report['full_conformance']
    assert all(r['level']=='interface' for r in report['checks'])


@pytest.mark.parametrize('defect',['consume','gap','more','future','exclusive','sort_wrap','reset_serial','clear_position','reset_drops_bytes','timestamp','kind','serial_gap'])
def test_stream_mutants_fail(defect):
    model,c,a=setup();restart=model.restart_fixture
    def corrupted(position,serial):
        result=restart(position,serial);s=model.extension;dispatch=s.dispatch;mark=s.mark
        def bad(fn,op,payload):
            response=dispatch(fn,op,payload)
            if op==16:
                if defect=='consume':s.data.clear()
                if defect=='gap':response=response[:8]+bytes([response[8]&~2])+response[9:]
                if defect=='more':response=response[:8]+b'\0'+response[9:]
                if defect=='future' and payload[0]==0 and int.from_bytes(payload[1:9],'little')>s.position:
                    response=payload[1:9]+response[8:]
            if op==17 and defect=='exclusive' and response[1]:response=response[:2]+response[24:]
            if op==18 and defect=='clear_position':s.position=0
            return response
        def bad_mark(kind,detail):
            if defect=='reset_drops_bytes' and kind==1:s.data.clear()
            mark(kind,detail)
            if defect=='sort_wrap':s.marks.sort()
            if defect=='timestamp':s.marks[-1]=s.marks[-1][:3]+(0xffffffffffffffff,s.marks[-1][4])
            if defect=='kind':s.marks[-1]=s.marks[-1][:2]+(2,)+s.marks[-1][3:]
            if defect=='serial_gap':s.serial=(s.serial+1)&0xffffffff
        s.dispatch=bad;s.mark=bad_mark
        if defect=='reset_serial':s.release=lambda:setattr(s,'serial',0)
        return result
    model.restart_fixture=corrupted
    report=StreamChecks(c,a).run()
    assert report['status']=='failed' and any(r['status']=='failed' for r in report['checks'][1:])


@pytest.mark.parametrize('invalid',['component','addressing','ops','geometry','scope'])
def test_adoption_is_explicit_and_invalid_input_blocks(invalid):
    _,c,a=setup();a.adoption=copy.deepcopy(a.adoption)
    if invalid=='component':a.adoption['component']='capture'
    elif invalid=='addressing':a.adoption['addressing']='resource'
    elif invalid=='ops':a.adoption['ops'].pop('write')
    elif invalid=='geometry':a.adoption['byte_capacity']=100
    else:a.reset_scope='physical'
    report=StreamChecks(c,a).run()
    assert report['status']=='failed' and report['checks'][0]['status']=='failed'
    assert all(r['status']=='blocked' for r in report['checks'][1:])


def test_adoption_requires_write_even_when_testing_reads_only():
    model,c,a=setup();describe=model.extension.describe
    model.extension.describe=lambda fn:tuple(row[:4]+b'\x0f' if row[0]==7 else row for row in describe(fn))
    report=StreamChecks(c,a).run()
    assert report['checks'][0]['status']=='failed' and all(row['status']=='blocked' for row in report['checks'][1:])


def test_new_boot_seeds_are_explicit_and_rejected_while_owned():
    model,c,a=setup();c.confirm();c.identity()
    with c.holding(3000):
        before=model.stream_state()
        with pytest.raises(ValueError):a.restart_fixture(0,0)
        assert model.stream_state()==before
    with pytest.raises(ValueError):a.restart_fixture(1<<64,0)
    with pytest.raises(ValueError):a.restart_fixture(0,1<<32)


def test_stream_checker_is_separate_from_oep_specific_operation_verdicts():
    _,c,a=setup();report=StreamChecks(c,a).run()
    assert report['levels']['oep-interface']=='not executed'
    assert 'write delivery/partial/failed outcomes' in report['unchecked']


def test_changed_spec_enum_blocks_old_fixture_before_wire():
    _,c,a=setup();c.reg['common']['enum']['mark_kind']['host']=0x40
    report=StreamChecks(c,a).run()
    assert report['checks'][0]['status']=='failed' and not c.trace
    assert all(row['status']=='blocked' for row in report['checks'][1:])
