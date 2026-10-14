import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_oep_declarations import OepDeclarationChecks
from oep_client.virtual_oep_declaration_model import OepDeclarationModel
from test_conformance import REG


# The public tests carry only the registry subset they actually consume.
SPECS=[{'name':name,'revision':1,'op':[{'name':op,'code':code} for op,code in operations],
        'tlv':{'describe':{'mechanisms' if name.endswith('console') else 'max_connections':64}},
        'enum':{'mechanism':{'sdi':0,'dmdata':1,'dmseq':2,'none':255},'mark_kind':{'reset':0x40,'restart':0x41},
                'mark_detail_reset':{'ndmreset':1},'mark_detail_restart':{'havereset':1,'resync':2},
                'mark_detail_lost':{'target_timeout':0x40}} if name.endswith('console') else
               {'pin_role':{'swdio':1,**({} if name.endswith('swio') else {'swclk':2})}}}
       for name,operations in [(name,[('scan',16),('attach',17),('detach',18),('connections',19)]) for name in
                               ('oep.wire.rvswd','oep.wire.swio','oep.wire.swd')]+[('oep.target.console',list(zip(('open','read','marks','clear','mark','write','close','streams'),range(16,24))))]]
SELECTION={1:'oep.wire.rvswd',2:'oep.wire.swio',3:'oep.wire.swd',4:'oep.target.console'}


def setup():
    reg=copy.deepcopy(REG);reg['interface']=copy.deepcopy(SPECS);reg['describe_common'].update(role_channels=1,channel_group=6,min_clock_hz=4)
    model=OepDeclarationModel(lambda:0,boot_id=17);c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c


def test_standard_declarations_pass_without_mutating_requests():
    _,c=setup();r=OepDeclarationChecks(c,SELECTION).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
    assert len(r['checks'])==13 and not r['full_conformance']
    assert all(x['level']=='oep-interface' for x in r['checks'])
    assert all(bytes.fromhex(x['request_hex'])[6:10]==b'\0'*4 for x in c.trace)
    assert r['observed']['wire_max_connections']=={'1':2,'2':1,'3':1}


MECH=lambda value:b'\x40'+len(value).to_bytes(2,'little')+value   # describe tag 0x40 mechanisms


@pytest.mark.parametrize('defect',['missing_attach','missing_detach','missing_connections','missing_console_close','console_mark_without_marks','missing_mechanisms','unknown_mechanism','preset_none','capacity_width','missing_pins','same_pin','pin_range',
                                   'uncounted_mechanisms','duplicate_mechanism','empty_mechanisms'])
def test_standard_declaration_mutants_fail(defect):
    model,c=setup();describe=model.extension.describe
    def bad(fn):
        rows=list(describe(fn))
        if defect.startswith('missing_') and defect in ('missing_attach','missing_detach','missing_connections') and fn==1:
            bit={'missing_attach':1,'missing_detach':2,'missing_connections':3}[defect]
            rows=[row[:4]+bytes((row[4]&~(1<<bit),)) if row[0]==7 else row for row in rows]
        if fn==4:
            if defect=='missing_console_close':rows=[row[:4]+bytes((row[4]&~64,)) if row[0]==7 else row for row in rows]   # close 0x16: required
            if defect=='console_mark_without_marks':rows=[row[:4]+bytes((row[4]&~4,)) if row[0]==7 else row for row in rows]   # mark kept, marks 0x12 gone
            if defect=='missing_mechanisms':rows=[row for row in rows if row[0]!=64]
            if defect in ('unknown_mechanism','preset_none'):rows=[MECH(bytes((1,3 if defect=='unknown_mechanism' else 255))) if row[0]==64 else row for row in rows]
            if defect=='uncounted_mechanisms':rows=[MECH(b'\x01') if row[0]==64 else row for row in rows]        # the mechanism alone, no n
            if defect=='duplicate_mechanism':rows=[MECH(b'\x02\x01\x01') if row[0]==64 else row for row in rows]
            if defect=='empty_mechanisms':rows=[MECH(b'\x00') if row[0]==64 else row for row in rows]
        if fn==1:
            if defect=='capacity_width':rows=[b'\x40\x02\0\x01\0' if row[0]==64 else row for row in rows]
            if defect=='missing_pins':rows=[row for row in rows if row[0] not in (1,6)]
            if defect=='same_pin':rows=[row[:-1]+b'\x01' if row[0]==1 else row for row in rows]
            if defect=='pin_range':rows=[row[:-1]+b'\x80' if row[0]==1 else row for row in rows]
        return tuple(rows)
    model.extension.describe=bad;r=OepDeclarationChecks(c,SELECTION).run()
    assert r['status']=='failed' and any(x['status']=='failed' for x in r['checks'][1:])


@pytest.mark.parametrize('invalid',['empty','unknown_name','wrong_fn','wrong_name','revision'])
def test_selection_and_revision_must_match(invalid):
    _,c=setup();selection=dict(SELECTION)
    if invalid=='empty':selection={}
    elif invalid=='unknown_name':selection[1]='io.example.custom'
    elif invalid=='wrong_fn':selection[9]=selection.pop(1)
    elif invalid=='wrong_name':selection[1]='oep.wire.swd'
    else:c.reg['interface'][0]['revision']=2
    r=OepDeclarationChecks(c,selection).run()
    assert r['checks'][0]['status']=='failed' and all(x['status']=='blocked' for x in r['checks'][1:])


def test_scan_is_optional_and_the_mechanisms_are_counted():
    """debug §1: scan is optional; console §1: mechanisms is n(u8) n x mechanism, n >= 1, no duplicates."""
    model,c=setup()
    r=OepDeclarationChecks(c,SELECTION).run();assert r['status']=='passed'
    assert 16 not in r['observed']['oep_required_ops']['1'] and r['observed']['console_mechanisms']['4']==[1]


def test_future_registry_mandatory_op_is_not_silently_ignored():
    _,c=setup();c.reg['interface'][0]['op'].append({'name':'future_required','code':20})
    assert OepDeclarationChecks(c,SELECTION).run()['status']=='failed'


@pytest.mark.parametrize('invalid',['missing','width','zero'])
def test_swio_fixed_clock_must_be_declared(invalid):
    model,c=setup();describe=model.extension.describe
    def bad(fn):
        rows=describe(fn)
        if fn!=2:return rows
        if invalid=='missing':return tuple(row for row in rows if row[0]!=4)
        return tuple((b'\x04\x01\0\x01' if invalid=='width' else b'\x04\x04\0\0\0\0\0') if row[0]==4 else row for row in rows)
    model.extension.describe=bad
    assert OepDeclarationChecks(c,SELECTION).run()['status']=='failed'


def test_nonmapping_selection_reports_failure_without_wire():
    _,c=setup();r=OepDeclarationChecks(c,[1]).run()
    assert r['status']=='failed' and not c.trace


def test_a_console_offering_only_the_minimum_ops_passes():
    """console §1 (the op table, 要否): marks, clear, mark and write are optional - a console offering only open, read,
    close and streams passes the declaration checks; one offering mark without marks fails them."""
    for keep, ok in ((0x01 | 0x02 | 0x40 | 0x80, True), (0x01 | 0x02 | 0x10 | 0x40 | 0x80, False)):
        model,c=setup();describe=model.extension.describe
        def bad(fn, keep=keep):
            rows=list(describe(fn))
            if fn==4:rows=[row[:4]+bytes((row[4]&keep,)) if row[0]==7 else row for row in rows]
            return tuple(rows)
        model.extension.describe=bad;r=OepDeclarationChecks(c,SELECTION).run()
        assert (r['status']=='passed')==ok,[(x['id'],x.get('error')) for x in r['checks']]
