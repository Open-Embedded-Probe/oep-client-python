import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_fixture_metadata import FixtureMetadataChecks
from oep_client.virtual_fixture_metadata_model import FixtureMetadataModel
from oep_client.virtual_core import tlv
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,original=registered_setup();reg=original.reg;reg.setdefault('constants',{})['min_max_frame']=64
    for s in reg['interface']:
        tags={'oep.fixture.gpio':{'modes':64},'oep.fixture.uart':{'formats':64},'oep.fixture.i2c-target':{'queue_depth':64,'max_stretch_us':65},'oep.fixture.spi-target':{'queue_depth':64,'modes':65,'cs_setup_ns':67}}
        if s['name'] in tags:s['tlv']={'describe':tags[s['name']]}
    model=FixtureMetadataModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,reg,'virtual-resource-1')


def replace(model,fn,tag,value):
    rows=model.extension.metadata[fn];rows[:]=[v for v in rows if v[0]!=tag]
    if value is not None:rows.append(tlv(tag,value))


def test_fixture_metadata_passes_read_only():
    model,c=setup();r=FixtureMetadataChecks(c,model.metadata_selection).run()
    assert r['status']=='passed',r['checks'];assert len(r['checks'])==10 and not r['full_conformance']
    assert all(bytes.fromhex(row['request_hex'])[6:10]==b'\0'*4 for row in c.trace)


@pytest.mark.parametrize('fn,tag,value',[
    (10,64,None),(10,64,b'\1'),(10,64,b'\0'*4),
    (11,64,None),(11,64,b''),(11,64,b'\0'),(11,64,b'\2\0'),(11,64,b'\1\4'),
    (11,64,b'\2\0\x02'),(11,64,b'\2\0\x0c'),(11,64,b'\2\0\x20'),
    (12,65,None),(12,65,b'\0'*4),(12,65,b'\1'),(13,67,b'\1'),
    *[(fn,tag,None) for fn in (12,13) for tag in (2,3,64)],
    *[(fn,tag,b'\1'*width) for fn in (12,13) for tag,width in ((2,3),(3,1),(5,3),(64,2))],
    (12,3,struct.pack('<H',53)),(13,3,struct.pack('<H',51)),
])
def test_fixture_metadata_defects_detected(fn,tag,value):
    model,c=setup();replace(model,fn,tag,value);r=FixtureMetadataChecks(c,model.metadata_selection).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-FIXTURE-METADATA-'+str(fn))['status']=='failed',r['checks']


@pytest.mark.parametrize('fn,length',[(12,52),(13,50)])
def test_route_frame_exact_boundary(fn,length):
    model,c=setup();replace(model,fn,3,struct.pack('<H',length))
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('fn,tag',[(12,2),(12,3),(12,64),(13,2),(13,3),(13,64),(13,67)])
def test_no_invented_lower_bounds(fn,tag):
    model,c=setup();replace(model,fn,tag,b'\0'*{2:4,3:2,64:1,67:4}[tag])
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


# features (tag 5) holds optional bits (core §7.4): absent = no bits, so an I2C target without internal pull-ups and an
# MSB-first-only SPI target need not carry it (fixture §3 / §4)
@pytest.mark.parametrize('fn,tag',[(13,67),(12,5),(13,5)])
def test_optional_metadata_absent(fn,tag):
    model,c=setup();replace(model,fn,tag,None)
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


def test_stretch_absence_without_optional_op():
    model,c=setup();replace(model,12,65,None);model.extension.offered[12].remove(20)
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


def test_all_defined_uart_formats():
    model,c=setup();formats=[data|parity|stop for data in (0,1) for parity in (0,4,8) for stop in (0,16)]
    replace(model,11,64,bytes([len(formats),*formats]));assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('fn,length',[(12,116),(13,114)])
def test_large_single_route_boundary(fn,length):
    model,c=setup();model.ep.probe.max_frame=128;replace(model,fn,3,struct.pack('<H',length))
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('fn', [12,13])
def test_other_route_unknown_limit_is_not_pass(fn):
    model,c=setup();model.ep.probe.max_frame=128;model.ep.static[0]+=(tlv(73,b'\1\6\xff'),)
    replace(model,fn,3,struct.pack('<H',60));assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='failed'


def test_small_multiroute_bound_is_proven():
    model,c=setup();model.ep.static[0]+=(tlv(73,b'\1\6\xff'),)
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


def test_fixture_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==137


def test_fixture_registered_ops():
    from oep_client.conformance_registered_ops import RegisteredOpsChecks
    model,c=setup();r=RegisteredOpsChecks(c,model.selection).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==18


@pytest.mark.parametrize('invalid',['minimum','selection','revision','tag'])
def test_fixture_metadata_preflight(invalid):
    model,c=setup();selection=model.metadata_selection
    if invalid=='minimum':c.reg['constants']['min_max_frame']=32
    elif invalid=='selection':selection={1:'oep.probe.plan'}
    elif invalid=='revision':next(s for s in c.reg['interface'] if s['name']=='oep.fixture.gpio')['revision']=2
    else:next(s for s in c.reg['interface'] if s['name']=='oep.fixture.gpio')['tlv']['describe']['modes']=66
    assert FixtureMetadataChecks(c,selection).run()['status']=='failed'




@pytest.mark.parametrize('value',[None,b'',b'\x0f\0',b'\0',b'\x10',b'\x1f',b'\xff'])
def test_spi_modes_are_required_one_byte_1_to_15(value):
    """fixture §4: describe's modes (tag 0x41, u8) is required - bit k = mode k, bits 4-7 0, at least 1."""
    model,c=setup();replace(model,13,65,value);r=FixtureMetadataChecks(c,model.metadata_selection).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-FIXTURE-METADATA-13')['status']=='failed',r['checks']


@pytest.mark.parametrize('value',[b'\x01',b'\x08',b'\x0f'])
def test_spi_modes_any_nonempty_subset_passes(value):
    model,c=setup();replace(model,13,65,value)
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('row',[tlv(1,b'\2\0\0\1'),tlv(6,b'\0\2\1\0\0\2\1\0')])
def test_uart_without_write_declares_no_tx_role(row):
    """fixture §2 (a09fa45): a fn that does not offer write takes no TX role and declares none in role_channels /
    channel_group; the RX role alone passes."""
    model,c=setup();model.extension.metadata[11].append(tlv(1,b'\1\0\0\1'))
    assert FixtureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'
    model,c=setup();model.extension.metadata[11].append(row);r=FixtureMetadataChecks(c,model.metadata_selection).run()
    assert next(x for x in r['checks'] if x['id']=='OEP-FIXTURE-METADATA-11')['status']=='failed',r['checks']
