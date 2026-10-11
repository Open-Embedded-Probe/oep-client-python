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
