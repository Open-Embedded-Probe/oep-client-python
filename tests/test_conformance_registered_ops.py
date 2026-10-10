import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_registered_ops import RegisteredOpsChecks
from oep_client.virtual_registered_ops_model import RegisteredOpsModel, MINIMAL
from test_conformance import REG


# Independent literal op names/numbers, including all optional operations.
OPERATIONS={
'oep.probe.plan':[(16,'plan_apply'),(17,'plan_release')], 'oep.probe.restart':[(16,'restart')],
'oep.probe.link':[(16,'source'),(17,'sink'),(18,'port_speed')],
**{n:list(zip(range(16,20),('scan','attach','detach','connections'))) for n in ('oep.wire.rvswd','oep.wire.swio','oep.wire.swd')},
'oep.target.riscv-dm':list(zip(range(16,24),('dmi','halt','resume','reset','read_block','write_block','run','step'))),
'oep.target.arm-adi':[(16,'transfer'),(17,'read_block'),(18,'write_block')],
'oep.target.console':list(zip(range(16,24),('open','read','marks','clear','mark','write','close','streams'))),
'oep.fixture.gpio':[(16,'set'),(17,'read')],
'oep.fixture.uart':list(zip(range(16,23),('configure','read','marks','clear','mark','write','status'))),
'oep.fixture.i2c-target':list(zip(range(16,21),('configure','read_rx','preload_tx','status','stretch'))),
'oep.fixture.spi-target':list(zip(range(16,20),('configure','arm','read_rx','status'))),
'oep.fixture.logic':list(zip(range(16,25),('configure','start','stop','force','status','read','segments','release','query')))+[(1,'subscribe'),(2,'unsubscribe')],
'oep.fixture.analog':list(zip(range(16,26),('configure','start','stop','force','status','read','segments','release','query','calibration')))+[(1,'subscribe'),(2,'unsubscribe')],
'oep.fixture.capture-group':list(zip(range(16,21),('bind','start','stop','force','status')))+[(1,'subscribe'),(2,'unsubscribe')],
'oep.probe.config':list(zip(range(16,22),('get','set','save','erase','unset','state'))),
}


def setup():
    reg=copy.deepcopy(REG);reg['interface']=[dict(name=name,revision=1,op=[dict(code=code,name=op) for code,op in rows]) for name,rows in OPERATIONS.items()]
    reg['describe_common'].update(role_channels=1,max_clock_hz=2,max_length=3,min_clock_hz=4,features=5,channel_group=6)
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
 ('oep.probe.config',{18},False),('oep.probe.config',{19},False),('oep.probe.config',{18,19},True),
 ('oep.fixture.logic',{1},False),('oep.fixture.logic',{2},False),('oep.fixture.logic',{1,2,19,24},True),('oep.fixture.logic',{25},False),
 ('oep.fixture.analog',{1,2,19,24},True),('oep.fixture.capture-group',{1},False),('oep.fixture.capture-group',{1,2,19},True),
 ('oep.probe.link',{18},True),('oep.fixture.i2c-target',{20},True),('oep.wire.rvswd',{16},True),('oep.wire.swio',{16},True),('oep.wire.swd',{16},True),
])
def test_optional_ops_pairs_and_calibration(name,extra,passed):
    model,c=setup();fn=next(fn for fn,n in model.selection.items() if n==name);model.extension.offered[fn].update(extra)
    r=RegisteredOpsChecks(c,model.selection).run();assert (r['status']=='passed')==passed,r['checks']


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
