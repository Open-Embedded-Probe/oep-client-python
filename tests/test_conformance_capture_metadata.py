import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_capture_metadata import CaptureMetadataChecks
from oep_client.virtual_capture_metadata_model import CaptureMetadataModel
from oep_client.virtual_core import tlv
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,original=registered_setup();reg=original.reg;reg['limits']={**reg.get('limits',{}),'wifi_min_max_frame':112}
    tags={'oep.fixture.logic':{'mode':64,'rate_range':65,'channels':68,'trigger':69,'multirate':96},'oep.fixture.analog':{'mode':64,'rate_range':65,'channels':68,'trigger':69,'frontend':70},'oep.fixture.capture-group':{'tracks':64},'oep.probe.config':{'items':65,'slots_max':66,'wifi_max':70}}
    for s in reg['interface']:
        if s['name'] in tags:s['tlv']={'describe':tags[s['name']]}
        if s['name']=='oep.probe.config':s['tlv']['item']={'wifi':8}
    model=CaptureMetadataModel(lambda:0,boot_id=17);return model,Checks(model.handle,reg,'virtual-resource-1')


def replace(model,fn,tag,value):
    rows=model.extension.metadata[fn];rows[:]=[v for v in rows if v[0]!=tag]
    if value is not None:rows.append(tlv(tag,value))


def test_metadata_passes_read_only():
    model,c=setup();r=CaptureMetadataChecks(c,model.metadata_selection).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==10 and not r['full_conformance']
    assert all(bytes.fromhex(row['request_hex'])[6:10]==b'\0'*4 for row in c.trace)


@pytest.mark.parametrize('fn,tag,value',[
    *[(fn,tag,None) for fn in (14,15) for tag in (5,64,65,68,69)],
    *[(fn,tag,b'\0') for fn in (14,15) for tag in (5,64,65,69)],
    *[(fn,5,struct.pack('<I',bit)) for fn in (14,15,16) for bit in (1,2,4)],
    (14,65,struct.pack('<IIB',1,2,2)),(14,68,b'\0\0'),(15,70,None),(15,70,b'\0'),
    (16,64,None),(16,64,b''),(16,64,b'\2\x0e\0'),(16,5,None),(16,5,b'\0'),
    (17,65,None),(17,66,None),(17,66,b''),(17,66,b'\0\0'),
    (14,96,b'\0'),(14,96,struct.pack('<IIIB',0,2,8,0)),(14,96,struct.pack('<IIIB',1,1,8,0)),(14,96,struct.pack('<IIIB',1,8,2,0)),(14,96,struct.pack('<IIIB',1,2,8,2)),
])
def test_metadata_defects(fn,tag,value):
    model,c=setup();replace(model,fn,tag,value);r=CaptureMetadataChecks(c,model.metadata_selection).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-CAPTURE-METADATA-'+str(fn))['status']=='failed',r['checks']


@pytest.mark.parametrize('fn,tag',[(14,64),(15,70)])
def test_repeated_identifier_cannot_duplicate(fn,tag):
    model,c=setup();model.extension.metadata[fn].append(next(v for v in model.extension.metadata[fn] if v[0]==tag))
    assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='failed'


def test_optional_multirate_can_be_absent():
    model,c=setup();replace(model,14,96,None);assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('fn',[14,15,16])
def test_future_feature_bits_not_reserved_low_bits(fn):
    model,c=setup();replace(model,fn,5,struct.pack('<I',8));assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('value',[None,b'',b'\0',b'\0\0'])
def test_wifi_capacity_condition_and_shape(value):
    model,c=setup();replace(model,17,70,value)
    r=CaptureMetadataChecks(c,model.metadata_selection).run()
    assert r['status']==('passed' if value is None else 'failed')


@pytest.mark.parametrize('frame,other,expected',[(111,False,'failed'),(112,False,'passed'),(128,False,'passed'),(128,True,'failed')])
def test_wifi_all_route_minimum(frame,other,expected):
    model,c=setup();model.ep.probe.max_frame=frame;replace(model,17,65,b'\x08');replace(model,17,70,b'\1')
    if other:model.ep.static[0]+=(tlv(73,b'\1\6\xff'),)
    r=CaptureMetadataChecks(c,model.metadata_selection).run();assert r['status']==expected,r['checks']


def test_wifi_max_not_made_mandatory_by_registry_comment():
    model,c=setup();model.ep.probe.max_frame=112;replace(model,17,65,b'\x08')
    assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('tag,value',[(65,b''),(66,b'\0')])
def test_no_invented_positive_config_minimum(tag,value):
    model,c=setup();replace(model,17,tag,value);assert CaptureMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==137


def test_registered_ops():
    from oep_client.conformance_registered_ops import RegisteredOpsChecks
    model,c=setup();r=RegisteredOpsChecks(c,model.selection).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==18


@pytest.mark.parametrize('invalid',['selection','revision','tag'])
def test_capture_metadata_preflight(invalid):
    model,c=setup();selection=model.metadata_selection
    if invalid=='selection':selection={1:'oep.probe.plan'}
    elif invalid=='revision':next(s for s in c.reg['interface'] if s['name']=='oep.fixture.logic')['revision']=2
    else:next(s for s in c.reg['interface'] if s['name']=='oep.fixture.logic')['tlv']['describe']['channels']=67
    assert CaptureMetadataChecks(c,selection).run()['status']=='failed'
