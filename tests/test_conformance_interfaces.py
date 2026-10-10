import copy
import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_interfaces import InterfaceChecks
from oep_client.virtual_declaration_model import DeclarationModel
from oep_client.virtual_core import tlv
from test_conformance import REG


def setup():
    reg=copy.deepcopy(REG)
    reg['describe_common'].update(role_channels=1,max_clock_hz=2,max_length=3,min_clock_hz=4,features=5,channel_group=6)
    now=[0];model=DeclarationModel(lambda:now[0],boot_id=17)
    c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c,InterfaceChecks(c)


def test_generic_declarations_pass():
    model,c,runner=setup();report=runner.run()
    assert report['status']=='passed',[(row['id'],row.get('error')) for row in report['checks']]
    assert len(report['checks'])==25 and report['interface_count']==3
    assert not report['full_conformance'] and model.ep.current_core.holder is None
    assert all(row['level']=='interface' for row in report['checks'])
    assert [row['instance'] for row in c.observed['interfaces']]==[1,0,0]
    assert all(row['channel_declarations']['role_candidates']=={'1':[0,1,2]} for row in c.observed['interfaces'])


@pytest.mark.parametrize('name',[b'one',b'.foo',b'foo.',b'foo..bar',b'-foo.bar',b'foo-.bar',b'foo.-bar',b'foo.bar-'])
def test_name_label_mutants_fail(name):
    model,_,runner=setup();model.extension.name=name
    report=runner.run()
    assert any(row['id'].startswith('IF-NAME-') and row['status']=='failed' for row in report['checks'])


@pytest.mark.parametrize('tag,value,case',[(2,b'\0','FIXED'),(3,b'\0'*4,'FIXED'),(4,b'\0'*2,'FIXED'),(5,b'\0'*8,'FIXED'),
    (1,b'\x01','CHANNELS'),(1,b'\x01\0\0\x10','CHANNELS'),(6,b'\x01\x02\x01\0\0','CHANNELS'),
    (6,b'\x01\x01\x01\x04\0','CHANNELS'),(7,b'\xf9\x01','OPS'),(7,b'\0\x08','OPS'),(7,b'\0\x02','OPS')])
def test_common_tag_mutants_fail(tag,value,case):
    model,_,runner=setup();describe=model.extension.describe
    model.extension.describe=lambda fn:(tlv(tag,value),)+tuple(row for row in describe(fn) if row[0]!=tag)
    report=runner.run()
    assert any(row['id'].startswith('IF-'+case+'-') and row['status']=='failed' for row in report['checks'])


def test_wrong_cursor_despite_successful_sequential_walk_fails():
    model,c,runner=setup();send=c.send
    def wrong(request):
        if request[5]==3 and int.from_bytes(request[10:12],'little') and int.from_bytes(request[12:14],'little')==1:
            request=request[:12]+b'\0\0'+request[14:]
        return send(request)
    c.send=wrong;report=runner.run()
    assert any(row['id'].startswith('IF-PAGES-') and row['status']=='failed' for row in report['checks'])


def test_declaration_changes_only_while_locked_fail():
    model,_,runner=setup();describe=model.extension.describe
    model.extension.describe=lambda fn:describe(fn)+((tlv(0x41,b'held'),) if model.ep.current_core.holder else ())
    report=runner.run()
    assert any(row['id'].startswith('IF-STABLE-') and row['status']=='failed' for row in report['checks'])


def test_unoffered_operation_refusal_precedes_session_fail():
    _,c,runner=setup();send=c.send
    def bad(request):
        response=send(request)
        if int.from_bytes(request[3:5],'little') and response[3:5]==b'\0\x02':return response[:4]+b'\x09'+response[5:]
        return response
    c.send=bad;report=runner.run()
    assert any(row['id'].startswith('IF-ABSENT-OPS-') and row['status']=='failed' for row in report['checks'])


def test_wrong_instance_fails_separately_from_revision_group():
    model,_,runner=setup();listing=model.extension.list_page
    def wrong(first,limit):
        page=listing(first,limit)
        if page[2]:page=page[:5]+b'\x09\0'+page[7:]
        return page
    model.extension.list_page=wrong;report=runner.run()
    assert any(row['id'].startswith('IF-INSTANCE-') and row['status']=='failed' for row in report['checks'])


def test_missing_ops_declaration_fails():
    model,_,runner=setup();describe=model.extension.describe
    model.extension.describe=lambda fn:tuple(row for row in describe(fn) if row[0]!=7)
    report=runner.run()
    assert any(row['id'].startswith('IF-OPS-') and row['status']=='failed' for row in report['checks'])


@pytest.mark.parametrize('name',[b'a.'+b'b'*46,b'oep.fixture.gpio',b'org.example.test'])
def test_valid_names_do_not_require_oep_registry_membership(name):
    model,_,runner=setup();model.extension.name=name;report=runner.run()
    assert report['status']=='passed'
    assert report['levels']['oep-interface']=='not executed'


def test_core_only_marks_interface_cases_not_applicable():
    from oep_client.virtual_resource_model import ResourceModel
    _,c,_=setup();model=ResourceModel();model.ep.current_core.extension=None
    c.send=model.handle;report=InterfaceChecks(c).run()
    assert report['status']=='passed' and len(report['checks'])==1 and report['interface_count']==0
    assert report['not_applicable'] and not report['full_conformance']
