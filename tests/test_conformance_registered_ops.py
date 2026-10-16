import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_registered_ops import RegisteredOpsChecks
from oep_client.virtual_registered_ops_model import RegisteredOpsModel, MINIMAL
from test_conformance import REG
from spec_names import SPEC, iface, ops


# Every op of these interfaces, all optional ones included, by name from the selected SPEC (tests/spec_names.py).
_NAMES=('oep.probe.plan','oep.probe.restart','oep.probe.link','oep.wire.rvswd','oep.wire.swio','oep.wire.swd',
        'oep.target.riscv-dm','oep.target.arm-adi','oep.target.console','oep.fixture.gpio','oep.fixture.uart',
        'oep.fixture.i2c-target','oep.fixture.spi-target','oep.fixture.logic','oep.fixture.analog',
        'oep.fixture.capture-group','oep.probe.config')
OPERATIONS={name:[(code,op_name) for op_name,code in ops(name).items()] for name in _NAMES}


def setup():
    reg=copy.deepcopy(REG);reg['interface']=[copy.deepcopy(iface(name)) for name in OPERATIONS]   # each as the SPEC defines it
    reg['describe_common'].update({k:SPEC['describe_common'][k] for k in ('role_channels','max_clock_hz','max_length','min_clock_hz','features','channel_group')})
    model=RegisteredOpsModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,reg,'virtual-resource-1')


def test_registered_standard_ops_pass_read_only():
    model,c=setup();r=RegisteredOpsChecks(c,model.selection).run()
    assert r['status']=='passed',r['checks'];assert len(r['checks'])==18 and not r['full_conformance']
    assert all(bytes.fromhex(row['request_hex'])[6:10]==b'\0'*4 for row in c.trace)


@pytest.mark.parametrize('name,code',[(name,code) for name,codes in MINIMAL.items() for code in codes])
def test_every_minimal_mandatory_op_is_checked(name,code):
    model,c=setup();fn=next(fn for fn,n in model.selection.items() if n==name);model.extension.offered[fn].remove(code)
    r=RegisteredOpsChecks(c,model.selection).run();assert next(row for row in r['checks'] if row['id']=='OEP-REGISTERED-OPS-'+str(fn))['status']=='failed'


@pytest.mark.parametrize('name,extra,passed',[
 ('oep.target.riscv-dm',{19,22,23},True),('oep.target.riscv-dm',{20},False),('oep.target.riscv-dm',{21},False),('oep.target.riscv-dm',{20,21},True),
 ('oep.probe.config',{18,21},False),('oep.probe.config',{19,21},False),('oep.probe.config',{18,19,21},True),('oep.probe.config',{18,19},False),('oep.probe.config',{21},True),
 ('oep.fixture.logic',{1},False),('oep.fixture.logic',{2},False),('oep.fixture.logic',{1,2,19,23,24},True),('oep.fixture.logic',{25},False),
 ('oep.fixture.analog',{1,2,19,23,24,25},True),('oep.fixture.analog',{25},True),('oep.fixture.capture-group',{1},False),('oep.fixture.capture-group',{1,2},True),
 ('oep.fixture.capture-group',{19},False),('oep.fixture.capture-group',{1,2,19},False),
 ('oep.probe.link',{18},False),('oep.probe.link',{18,19},True),('oep.probe.link',{19},True),('oep.target.riscv-dm',{17,18},True),('oep.target.console',{20},False),('oep.target.console',{18,20},True),('oep.target.console',{19,21},True),('oep.fixture.uart',{20},False),('oep.fixture.uart',{18,19,20,21},True),('oep.fixture.i2c-target',{20},True),('oep.wire.rvswd',{16},True),('oep.wire.swio',{16},True),('oep.wire.swd',{16},True),
 ('oep.target.arm-adi',{17},False),('oep.target.arm-adi',{18},False),('oep.target.arm-adi',{17,18},True),   # debug §6: transfer alone; blocks a pair
])
def test_optional_ops_pairs_and_calibration(name,extra,passed):
    model,c=setup();fn=next(fn for fn,n in model.selection.items() if n==name);model.extension.offered[fn].update(extra)
    r=RegisteredOpsChecks(c,model.selection).run();assert (r['status']=='passed')==passed,r['checks']


def mode(value,samples=16,segments=0):
    import struct
    from oep_client.virtual_core import tlv
    return tlv(64,struct.pack('<BII',value,samples,segments))


@pytest.mark.parametrize('name,modes,extra,passed',[
 *[(name,modes,extra,passed) for name in ('oep.fixture.logic','oep.fixture.analog') for modes,extra,passed in (
   ((1,),set(),True),((2,),set(),False),((2,),{23},True),((1,2),set(),False),((3,),set(),False),((3,),{1},False),
   ((3,),{1,2},True),((1,2,3),{1,2},False),((1,2,3),{23},False),((1,2,3),{1,2,23},True),((1,3),{23},False))],
])
def test_mode_required_capture_ops(name,modes,extra,passed):
    # release is required only with mode 2, subscribe/unsubscribe only with mode 3 (capture §1/3.2/3.4).
    model,c=setup();fn=next(fn for fn,n in model.selection.items() if n==name)
    model.extension.offered[fn].update(extra);model.extension.extra[fn]=[mode(m,16,2 if m==2 else 0) for m in modes]
    r=RegisteredOpsChecks(c,model.selection).run();assert (r['status']=='passed')==passed,r['checks']
    if passed:assert c.observed['registered_op_contracts'][str(fn)]['modes']==sorted(modes)


def test_mode_tag_must_be_selected():
    model,c=setup();next(s for s in c.reg['interface'] if s['name']=='oep.fixture.logic')['tlv']={}
    r=RegisteredOpsChecks(c,model.selection).run();assert r['checks'][0]['status']=='failed'


def test_future_mandatory_op_is_not_silently_ignored():
    model,c=setup();c.reg['interface'][9]['op'].append(dict(name='future_required',code=22))
    assert RegisteredOpsChecks(c,model.selection).run()['status']=='failed'


def test_registered_ops_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==137


@pytest.mark.parametrize('invalid',['fn','width','name','type','revision','optional'])
def test_registered_ops_preflight_blocks(invalid):
    model,c=setup();selection=dict(model.selection)
    if invalid=='fn':selection={True:'oep.probe.plan'}
    elif invalid=='width':selection={65536:'oep.probe.plan'}
    elif invalid=='name':selection[1]='io.example.custom'
    elif invalid=='type':selection[1]=[]
    elif invalid=='revision':c.reg['interface'][0]['revision']=2
    else:c.reg['interface'][2]['op']=[r for r in c.reg['interface'][2]['op'] if r['name']!='port_speed']
    r=RegisteredOpsChecks(c,selection).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])
