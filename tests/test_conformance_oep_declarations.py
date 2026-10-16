import copy
import pytest
from oep_client.conformance import Checks, ops
from oep_client.catalog import pack_ops
from oep_client.conformance_oep_declarations import OepDeclarationChecks
from oep_client.virtual_oep_declaration_model import OepDeclarationModel
from test_conformance import REG
from spec_names import SPEC, iface


# The selected SPEC's wires and console, by name (tests/spec_names.py).
SPECS=[copy.deepcopy(iface(name)) for name in ('oep.wire.rvswd','oep.wire.swio','oep.wire.swd','oep.target.console')]
SELECTION={1:'oep.wire.rvswd',2:'oep.wire.swio',3:'oep.wire.swd',4:'oep.target.console'}


def setup():
    reg=copy.deepcopy(REG);reg['interface']=copy.deepcopy(SPECS);reg['describe_common'].update({k:SPEC['describe_common'][k] for k in ('role_channels','channel_group','min_clock_hz')})
    model=OepDeclarationModel(lambda:0,boot_id=17);c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c


def test_standard_declarations_pass_without_mutating_requests():
    _,c=setup();r=OepDeclarationChecks(c,SELECTION).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
    assert len(r['checks'])==13 and not r['full_conformance']
    assert all(x['level']=='oep-interface' for x in r['checks'])
    assert all(bytes.fromhex(x['request_hex'])[6:10]==b'\0'*4 for x in c.trace)
    assert r['observed']['wire_max_connections']=={'1':2,'2':1,'3':1}


MECH_TAG=iface('oep.target.console')['tlv']['describe']['mechanism']
MECH=lambda name:bytes((MECH_TAG,))+len(name).to_bytes(2,'little')+name   # one mechanism of the named list (core §7.7)
OPS_TAG=SPEC['describe_common']['ops']
WIRE_OPS=dict((o['name'],o['code']) for o in iface('oep.wire.rvswd')['op']);CON_OPS=dict((o['name'],o['code']) for o in iface('oep.target.console')['op'])


def without(row,codes):
    """An ops row without these ops."""
    return bytes((OPS_TAG,))+len(packed:=pack_ops(set(ops(row[3:]))-set(codes))).to_bytes(2,'little')+packed


def only(row,codes):
    return bytes((OPS_TAG,))+len(packed:=pack_ops(set(ops(row[3:]))&set(codes))).to_bytes(2,'little')+packed


@pytest.mark.parametrize('defect',['missing_attach','missing_detach','missing_connections','missing_console_close','console_mark_without_marks','missing_mechanisms','unknown_mechanism','preset_none','capacity_width','missing_pins','same_pin','pin_range',
                                   'uncounted_mechanisms','duplicate_mechanism','empty_mechanisms','write_with_sdi_only'])
def test_standard_declaration_mutants_fail(defect):
    model,c=setup();describe=model.extension.describe
    def bad(fn):
        rows=list(describe(fn))
        if defect.startswith('missing_') and defect in ('missing_attach','missing_detach','missing_connections') and fn==1:
            gone=WIRE_OPS[defect[len('missing_'):]]
            rows=[without(row,[gone]) if row[0]==OPS_TAG else row for row in rows]
        if fn==4:
            if defect=='missing_console_close':rows=[without(row,[CON_OPS['close']]) if row[0]==OPS_TAG else row for row in rows]   # close: required
            if defect=='console_mark_without_marks':rows=[without(row,[CON_OPS['marks']]) if row[0]==OPS_TAG else row for row in rows]   # mark kept, marks gone
            if defect in ('missing_mechanisms','empty_mechanisms'):rows=[row for row in rows if row[0]!=MECH_TAG]
            if defect in ('unknown_mechanism','preset_none'):rows=[MECH(b'bogus' if defect=='unknown_mechanism' else b'none') if row[0]==MECH_TAG else row for row in rows]
            if defect=='uncounted_mechanisms':rows=[MECH(b'') if row[0]==MECH_TAG else row for row in rows]        # an empty name
            if defect=='duplicate_mechanism':rows=[row for row in rows]+[MECH(b'dmdata')]
            if defect=='write_with_sdi_only':rows=[MECH(b'sdi') if row[0]==MECH_TAG else row for row in rows]   # write kept (console §1)
        if fn==1:
            CAP=iface('oep.wire.rvswd')['tlv']['describe']['max_connections'];RC,CG=SPEC['describe_common']['role_channels'],SPEC['describe_common']['channel_group']
            if defect=='capacity_width':rows=[bytes((CAP,2,0,1,0)) if row[0]==CAP else row for row in rows]
            if defect=='missing_pins':rows=[row for row in rows if row[0] not in (RC,CG)]
            if defect=='same_pin':rows=[row[:-1]+b'\x01' if row[0]==RC else row for row in rows]
            if defect=='pin_range':rows=[row[:-1]+b'\x80' if row[0]==RC else row for row in rows]
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
    assert WIRE_OPS['scan'] not in r['observed']['oep_required_ops']['1'] and r['observed']['console_mechanisms']['4']==['dmdata']


def test_future_registry_mandatory_op_is_not_silently_ignored():
    _,c=setup();c.reg['interface'][0]['op'].append({'name':'future_required','code':20})
    assert OepDeclarationChecks(c,SELECTION).run()['status']=='failed'


@pytest.mark.parametrize('invalid',['missing','width','zero'])
def test_swio_fixed_clock_must_be_declared(invalid):
    model,c=setup();describe=model.extension.describe
    def bad(fn):
        rows=describe(fn)
        if fn!=2:return rows
        MIN=SPEC['describe_common']['min_clock_hz']
        if invalid=='missing':return tuple(row for row in rows if row[0]!=MIN)
        return tuple((bytes((MIN,1,0,1)) if invalid=='width' else bytes((MIN,4,0,0,0,0,0))) if row[0]==MIN else row for row in rows)
    model.extension.describe=bad
    assert OepDeclarationChecks(c,SELECTION).run()['status']=='failed'


def test_nonmapping_selection_reports_failure_without_wire():
    _,c=setup();r=OepDeclarationChecks(c,[1]).run()
    assert r['status']=='failed' and not c.trace


def test_a_console_offering_only_the_minimum_ops_passes():
    """console §1 (the op table, 要否): marks, clear, mark and write are optional - a console offering only open, read,
    close and streams passes the declaration checks; one offering mark without marks fails them."""
    minimum=[CON_OPS[k] for k in ('open','read','close','streams')]
    for keep, ok in ((minimum, True), (minimum+[CON_OPS['mark']], False)):
        model,c=setup();describe=model.extension.describe
        def bad(fn, keep=keep):
            rows=list(describe(fn))
            if fn==4:rows=[only(row,keep) if row[0]==OPS_TAG else row for row in rows]
            return tuple(rows)
        model.extension.describe=bad;r=OepDeclarationChecks(c,SELECTION).run()
        assert (r['status']=='passed')==ok,[(x['id'],x.get('error')) for x in r['checks']]


def test_sdi_only_without_write_passes():
    """console §1: a fn that offers write offers mechanism 1 or 2; one without write may declare SDI alone."""
    model,c=setup();describe=model.extension.describe
    def sdi(fn):
        rows=list(describe(fn))
        if fn==4:rows=[MECH(b'sdi') if row[0]==MECH_TAG else without(row,[CON_OPS['write']]) if row[0]==OPS_TAG else row for row in rows]
        return tuple(rows)
    model.extension.describe=sdi
    assert OepDeclarationChecks(c,SELECTION).run()['status']=='passed'
