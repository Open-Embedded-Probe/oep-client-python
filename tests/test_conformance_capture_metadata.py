import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_capture_metadata import CaptureMetadataChecks
from oep_client.virtual_capture_metadata_model import CaptureMetadataModel
from oep_client.virtual_core import tlv
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,original=registered_setup();reg=original.reg
    tags={'oep.fixture.logic':{'mode':64,'rate_range':65,'channels':68,'trigger':69},'oep.fixture.analog':{'mode':64,'rate_range':65,'channels':68,'trigger':69,'frontend':70},'oep.fixture.capture-group':{'tracks':64}}
    for s in reg['interface']:
        if s['name'] in tags:s['tlv']={'describe':tags[s['name']]}
    model=CaptureMetadataModel(lambda:0,boot_id=17);return model,Checks(model.handle,reg,'virtual-resource-1')


def replace(model,fn,tag,value):
    rows=model.extension.metadata[fn];rows[:]=[v for v in rows if v[0]!=tag]
    if value is not None:rows.append(tlv(tag,value))


def test_metadata_passes_read_only():
    model,c=setup();r=CaptureMetadataChecks(c,model.metadata_selection).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==8 and not r['full_conformance']
    assert all(bytes.fromhex(row['request_hex'])[6:10]==b'\0'*4 for row in c.trace)


@pytest.mark.parametrize('fn,tag,value',[
    *[(fn,tag,None) for fn in (14,15) for tag in (1,64,65,68,69)],
    *[(fn,tag,b'\0') for fn in (14,15) for tag in (5,64,65,69)],
    (14,65,struct.pack('<IIB',1,2,0)),(14,65,struct.pack('<II',0,2)),(14,65,struct.pack('<II',3,2)),(14,68,b'\0\0'),
    (14,69,struct.pack('<II',2,16)),(15,69,struct.pack('<II',0x18,0)),
    (15,70,None),(15,70,b'\0'),(15,70,struct.pack('<BiiI',0,-1000,3300,0)),
    (16,64,None),(16,64,b''),(16,64,b'\2\x0e\0'),(16,64,b'\0'),(16,5,b'\0'),
])
def test_metadata_defects(fn,tag,value):
    model,c=setup();replace(model,fn,tag,value);r=CaptureMetadataChecks(c,model.metadata_selection).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-CAPTURE-METADATA-'+str(fn))['status']=='failed',r['checks']


@pytest.mark.parametrize('fn,rows',[
    (14,[(1,32,1),(3,32,0)]),(14,[(1,32,0),(3,32,2)]),(15,[(1,32,0),(2,32,4),(1,16,0)]),
])
def test_mode_row_defects(fn,rows):
    # max_segments is 0 for one-shot/streaming; one row per mode (capture §3.5).
    model,c=setup();replace(model,fn,64,None);model.extension.metadata[fn]+=[tlv(64,struct.pack('<BII',*row)) for row in rows]
    r=CaptureMetadataChecks(c,model.metadata_selection).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-CAPTURE-METADATA-'+str(fn))['status']=='failed',r['checks']


@pytest.mark.parametrize('fn',[14,15])
def test_channel_group_may_replace_role_channels(fn):
    model,c=setup();replace(model,fn,1,None);model.extension.metadata[fn].append(tlv(6,bytes((0,1,0,0,0))))
    assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('fn,rows',[(14,[(1,32,0)]),(15,[(2,32,4)]),(14,[(3,32,0),(1,32,0)])])
def test_any_declared_mode_subset_passes(fn,rows):
    model,c=setup();replace(model,fn,64,None);model.extension.metadata[fn]+=[tlv(64,struct.pack('<BII',*row)) for row in rows]
    assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('fn',[14,15,16])
def test_features_absent_or_any_bit_is_not_failed(fn):
    # Revision 1 defines no feature bits; zero may be left out, later bits are not judged here.
    for value in (None,struct.pack('<I',8),struct.pack('<I',1)):
        model,c=setup();replace(model,fn,5,value);assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('fn,tag',[(14,64),(15,70)])
def test_repeated_identifier_cannot_duplicate(fn,tag):
    model,c=setup();model.extension.metadata[fn].append(next(v for v in model.extension.metadata[fn] if v[0]==tag))
    assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='failed'


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==129


def test_registered_ops():
    from oep_client.conformance_registered_ops import RegisteredOpsChecks
    model,c=setup();r=RegisteredOpsChecks(c,model.selection).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==17


@pytest.mark.parametrize('invalid',['selection','revision','tag'])
def test_capture_metadata_preflight(invalid):
    model,c=setup();selection=model.metadata_selection
    if invalid=='selection':selection={1:'oep.probe.plan'}
    elif invalid=='revision':next(s for s in c.reg['interface'] if s['name']=='oep.fixture.logic')['revision']=2
    else:next(s for s in c.reg['interface'] if s['name']=='oep.fixture.logic')['tlv']['describe']['channels']=67
    assert CaptureMetadataChecks(c,selection).run()['status']=='failed'
