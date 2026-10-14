import copy
import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_discovery import DiscoveryChecks
from oep_client.virtual_declaration_model import DeclarationModel
from oep_client.virtual_core import tlv
from test_conformance import REG


def setup(kind='short'):
    model = DeclarationModel(lambda:0, boot_id=17)
    model.extension.name = b'io.test' if kind=='short' else b'io.'+b'a'*45
    if kind=='empty': model.extension.functions = ()
    reg=copy.deepcopy(REG);reg['core']['tlv']['describe']['label']=0x46
    model.ep.static[0] += (tlv(0x46, struct.pack('<H',0)+b'UART\nRX'),tlv(0x46,struct.pack('<H',3)+'端子'.encode()))
    c=Checks(model.handle,reg,'virtual-resource-1'); return model,c,DiscoveryChecks(c)


@pytest.mark.parametrize('kind',['short','long','empty'])
def test_discovery_cursors_pass(kind):
    model,c,runner=setup(kind);report=runner.run()
    assert report['status']=='passed',report['checks']
    assert len(report['checks'])==6 and all(x['level']=='core' for x in report['checks'])
    assert not report['full_conformance'] and model.ep.current_core.holder is None


@pytest.mark.parametrize('fault,case', [('list_cursor','LIST-CURSORS'),('core_cursor','CORE-CURSORS'),
    ('locked_list','LOCKED'),('locked_core','LOCKED'),('unknown_end','UNKNOWN-FN')])
def test_discovery_cursor_faults_are_detected(fault,case):
    model,c,runner=setup();send=c.send
    def wrong(request):
        changed=request
        if request[5]==2:
            first=int.from_bytes(request[10:12],'little')
            if (fault=='list_cursor' and first==1) or (fault=='locked_list' and model.ep.current_core.holder):
                changed=request[:10]+b'\0\0'
        if request[5]==3:
            target,first=struct.unpack_from('<HH',request,10)
            if not target and ((fault=='core_cursor' and first==1) or (fault=='locked_core' and model.ep.current_core.holder)):
                changed=request[:12]+b'\0\0'
            if fault=='unknown_end' and target==1 and first==65535:
                return request[:1].replace(b'\x01',b'\x02')+request[1:3]+b'\x01\0\0'
        return send(changed)
    c.send=wrong;report=runner.run()
    assert next(x for x in report['checks'] if x['id']=='CORE-DISCOVERY-'+case)['status']=='failed',report['checks']


@pytest.mark.parametrize('value', [b'',b'\0',b'\x04\0bad',b'\xff\xffbad',b'\0\0\xff'])
def test_invalid_channel_labels_fail(value):
    model,c,runner=setup();model.ep.static[0] += (tlv(0x46,value),)
    report=runner.run()
    assert next(x for x in report['checks'] if x['id']=='CORE-DISCOVERY-LABELS')['status']=='failed'



def test_revision_range_profile_checks_all_boundary_pairs():
    model, c, runner = setup()
    runner.supported_revisions = (1,1)
    report = runner.run()
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 7
    rows = report['observed']['confirm_revision_ranges']
    assert {(r['minimum'],r['maximum']) for r in rows} == {(a,b) for a in (0,1,2,254,255) for b in (0,1,2,254,255)}
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('fault', ['revision','unsupported_tag','supported_missing','supported_width','supported_value','reverse','clear_owner','high_poison'])
def test_confirm_range_faults_are_detected(fault):
    model,c,runner=setup();c.confirm();c.identity();send=c.send
    def broken(request):
        answer=send(request)
        if request[5]==1:
            minimum,maximum=request[14:16]
            if fault=='revision' and minimum<=1<=maximum:answer=answer[:9]+b'\x02'+answer[10:]
            if minimum<=maximum and not minimum<=1<=maximum:
                if fault=='unsupported_tag':answer=answer[:5]+b'\xfe'+answer[6:]
                if fault=='supported_missing':answer=answer[:6]
                if fault=='supported_width':answer=answer[:6]+b'\x01\x01\0\x01'
                if fault=='supported_value':answer=answer[:6]+b'\x01\x02\0\x01\x02'
            if fault=='reverse' and minimum>maximum:answer=answer[:3]+b'\0\x0b\0\x01\x02\0\x01\x01'
            if fault=='clear_owner':model.ep.current_core.release()
            if fault=='high_poison':model.ep.current_core.high=0x8001   # half the space ahead: later corrs look old (core §5.1)
        return answer
    c.send=broken;runner.supported_revisions=(1,1)
    c.check('CORE-DISCOVERY-REVISIONS','core §7.1',runner.revisions)
    assert c.results[-1]['status']=='failed',c.results
    expected={'revision':'highest supported','unsupported_tag':'fixed tag','supported_missing':'interval','supported_width':'interval','supported_value':'interval','reverse':'expected rejected malformed','clear_owner':'expected completed success','high_poison':'expected completed success'}
    assert expected[fault] in c.results[-1]['error'],c.results


@pytest.mark.parametrize('profile',[(2,2),(1,0),(1,256),(True,1),(1,),[1,1]])
def test_invalid_revision_profiles_fail_before_session_work(profile):
    model,c,runner=setup();c.confirm();c.identity();runner.supported_revisions=profile
    c.check('CORE-DISCOVERY-REVISIONS','explicit profile',runner.revisions)
    assert c.results[-1]['status']=='failed' and model.ep.current_core.last is None



def test_revision_range_profile_allows_unretained_clock_reply():
    model,c,runner=setup();model.ep.remember_max=16;runner.supported_revisions=(1,1)
    report=runner.run()
    assert report['status']=='passed',report['checks']
    assert any(bytes.fromhex(row['response_hex'])[3:5]==b'\0\x0c' for row in c.trace)



@pytest.mark.parametrize('profile',[(2,2),(1,0),(1,256),(True,1),(1,),[1,1]])
def test_invalid_revision_profiles_block_the_complete_run(profile):
    model,c,runner=setup();runner.supported_revisions=profile
    report=runner.run()
    assert report['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in report['checks'][1:])
    assert c.trace==[] and model.ep.current_core.last is None
