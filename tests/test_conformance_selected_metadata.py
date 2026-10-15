import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_selected_metadata import SelectedMetadataChecks
from oep_client.virtual_selected_metadata_model import SelectedMetadataModel
from oep_client.virtual_core import tlv
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,original=registered_setup();reg=original.reg;reg.setdefault('constants',{})['min_max_frame']=64
    for s in reg['interface']:
        if s['name']=='oep.probe.restart':s['tlv']={'describe':{'restart_max_ms':64}}
        if s['name']=='oep.probe.config':s['tlv']={'describe':{'storage':64}}
    model=SelectedMetadataModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,reg,'virtual-resource-1')


def test_selected_metadata_passes_read_only():
    model,c=setup();r=SelectedMetadataChecks(c,model.metadata_selection).run()
    assert r['status']=='passed',r['checks'];assert len(r['checks'])==10 and not r['full_conformance']
    assert all(bytes.fromhex(row['request_hex'])[6:10]==b'\0'*4 for row in c.trace)


@pytest.mark.parametrize('fn,rows',[
 (2,[]),(2,[tlv(64,b'\0')]),(7,[]),(7,[tlv(3,b'\0')]),(7,[tlv(3,struct.pack('<H',6))]),
 (8,[]),(8,[tlv(3,struct.pack('<H',8)),tlv(5,b'\0'*4)]),
 (17,[tlv(64,b'\0')]),(17,[tlv(64,b'\0'*4)]),(17,[]),
])
def test_metadata_defects_are_detected(fn,rows):
    model,c=setup();model.extension.metadata[fn]=rows;r=SelectedMetadataChecks(c,model.metadata_selection).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-METADATA-'+str(fn))['status']=='failed',r['checks']


def test_storage_without_save_ops_is_rejected():
    model,c=setup();model.extension.offered[17].difference_update((18,19))
    r=SelectedMetadataChecks(c,model.metadata_selection).run();assert r['status']=='failed'


@pytest.mark.parametrize('fn,tag,width',[(2,64,4),(7,3,2),(8,3,2)])
def test_no_invented_positive_minimum(fn,tag,width):
    model,c=setup();model.extension.metadata[fn]=[tlv(tag,b'\0'*width)]
    r=SelectedMetadataChecks(c,model.metadata_selection).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('fn', [7,17])
def test_inactive_optional_metadata_can_be_absent(fn):
    model,c=setup();model.extension.metadata[fn]=[]
    model.extension.offered[fn].difference_update((20,21) if fn==7 else (18,19))
    assert SelectedMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


def test_large_single_route_length_is_supported():
    model,c=setup();model.ep.probe.max_frame=128;model.extension.metadata[7]=[tlv(3,struct.pack('<H',108))]
    assert SelectedMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


@pytest.mark.parametrize('frame,length,routes',[(64,48,()),(128,108,(tlv(73,b'\x01\x06\xff'),)),(64,4096,())])
def test_a_max_length_past_a_route_frame_is_not_a_defect(frame,length,routes):
    """debug §4.5 (a09fa45): max_length is the function's limit; the host takes min(max_length, max_frame - 8 / - 20)
    per route, and "大きい経路を小さい経路の転送量へ一律に制限しない" - a max_length past this or another route's frame passes
    (the old rule that it fit every route's frame is gone)."""
    model,c=setup();model.ep.probe.max_frame=frame;model.ep.static[0]+=routes
    model.extension.metadata[7]=[tlv(3,struct.pack('<H',length))]
    assert SelectedMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


def test_protocol_minimum_is_enough_for_small_multiroute_block_length():
    model,c=setup();model.ep.static[0]+=(tlv(73,b'\x01\x06\xff'),)
    assert SelectedMetadataChecks(c,model.metadata_selection).run()['status']=='passed'


def test_metadata_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==137


def test_metadata_preserves_registered_op_contracts():
    from oep_client.conformance_registered_ops import RegisteredOpsChecks
    model,c=setup();r=RegisteredOpsChecks(c,model.selection).run();assert r['status']=='passed',r['checks'];assert len(r['checks'])==18


@pytest.mark.parametrize('invalid',['minimum','selection','revision','tag'])
def test_metadata_preflight_stops(invalid):
    model,c=setup();selection=model.metadata_selection
    if invalid=='minimum':c.reg['constants']['min_max_frame']=32
    elif invalid=='selection':selection={1:'oep.probe.plan'}
    elif invalid=='revision':next(s for s in c.reg['interface'] if s['name']=='oep.probe.restart')['revision']=2
    else:next(s for s in c.reg['interface'] if s['name']=='oep.probe.restart')['tlv']['describe']['restart_max_ms']=65
    r=SelectedMetadataChecks(c,selection).run();assert r['status']=='failed'
