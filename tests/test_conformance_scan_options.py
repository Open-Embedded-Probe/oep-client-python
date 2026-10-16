from oep_client.conformance import Checks, ops
from oep_client.catalog import pack_ops
from oep_client.virtual_core import tlv
from oep_client.virtual_names import DC
from oep_client.virtual_scan_options_model import SC, KIND, WOP
from oep_client.conformance_scan_options import ScanOptionsChecks
from oep_client.conformance_scan_options_sample import SampleScanOptionsAdapter
from oep_client.virtual_scan_options_model import ScanOptionsModel
from test_conformance_wire_options import setup as wire_setup


def setup():
    _,c,_=wire_setup()
    model=ScanOptionsModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleScanOptionsAdapter(model)


def test_scan_options_contracts_pass():
    _,c,a=setup();r=ScanOptionsChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==29 and not r['full_conformance']


import pytest
import struct
from oep_client.virtual_core import Reject


@pytest.mark.parametrize('fault,case',[
    ('clock','IDLE-1'),('data','IDLE-None'),('selector_ignored','SELECTOR-16777217'),
    ('default_selected','SELECTOR-None'),('zero_ignored','ZERO-SELECTOR'),
    ('wrong_kind','SELECTOR-16777217'),('wrong_identity','FIRST-IDLE'),
    ('implicit_select','IDLE-None'),('release_leaked','IDLE-1'),
    ('allocated','PREFIX'),('prefix_ignored','PREFIX'),('existing_buffer','RETAINED'),
    ('early_release','IDLE-1'),('read_level','IDLE-1'),('speed','FIRST-IDLE'),
    ('bad_version','VERSIONS'),('early_touch','REFUSE-BAD-LATE'),
    ('idle_value_ignored','REFUSE-IDLE'),
])
def test_scan_option_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request
        def broken(fn,op,payload,tags):
            if fn>3 or op!=WOP['scan']:return dispatch(fn,op,payload,tags)
            start=len(e.scan_events)
            if fault=='selector_ignored' or fault=='zero_ignored' and SC['targetsel'] in tags and tags[SC['targetsel']][1]==b'\0'*4:tags=dict(tags);tags.pop(SC['targetsel'],None)
            if fault=='default_selected' and fn==3 and SC['targetsel'] not in tags:tags=dict(tags);tags[SC['targetsel']]=(SC['targetsel']|128,struct.pack('<I',0x01000001))
            if fault=='prefix_ignored':e.scan_limit=3
            if fault=='idle_value_ignored' and SC['idle_clock'] in tags:tags=dict(tags);tags[SC['idle_clock']]=(tags[SC['idle_clock']][0],b'\0')
            try:data=dispatch(fn,op,payload,tags)
            except Reject:
                if fault=='early_touch':e.scan_events.append(('invalid-read',fn))
                raise
            if fault=='wrong_kind' and data[1]:data=data[:2]+bytes((KIND['arm_adi'] if data[2]==KIND['riscv_dm'] else KIND['riscv_dm'],))+data[3:]
            elif fault=='wrong_identity' and data[1]:data=data[:7]+struct.pack('<I',0xdeadbeef)+data[11:]
            elif fault=='bad_version' and data[1]==0:
                pair=struct.unpack_from('<HH',payload,1);identity=e.identities.get((fn,pair,None))
                if identity is not None:data=bytes((data[0],1))+struct.pack('<BHHI',KIND['riscv_dm'],*pair,identity)+data[2:]
            elif fault=='release_leaked':e.scan_live.add(0);e.pin_levels[0]=dict(mode='wire',level=1)
            elif fault=='allocated':e.allocate()
            elif fault=='existing_buffer':
                for _,stream in e.streams.values():stream.data.clear()
            elif fault=='implicit_select':e.scan_events.append(('select',fn,0))
            elif fault=='early_release':
                for i in range(start,len(e.scan_events)-1):
                    if e.scan_events[i][0]=='read' and e.scan_events[i+1][0]=='release':e.scan_events[i],e.scan_events[i+1]=e.scan_events[i+1],e.scan_events[i]
            for i in range(start,len(e.scan_events)):
                row=e.scan_events[i]
                if row[0]=='drive' and fault in ('clock','data'):
                    levels=list(row[3]);levels[-1 if fault=='clock' else 0]^=1;e.scan_events[i]=row[:3]+(levels,)
                elif row[0]=='read' and fault=='read_level':e.scan_events[i]=row[:6]+([1,1],)
                elif row[0]=='read' and fault=='speed':e.scan_events[i]=row[:4]+(2000,)+row[5:]
            return data
        e.dispatch_request=broken
    model.install=faulty_install;r=ScanOptionsChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-SCAN-OPTIONS-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','idle','selector','kind','offer','budget'])
def test_scan_option_preflight_blocks(invalid):
    model,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='idle':del c.reg['interface'][0]['tlv']['scan']['idle_clock']          # a SPEC without what the checker uses
    elif invalid=='selector':del c.reg['interface'][2]['tlv']['scan']['targetsel']
    elif invalid=='kind':del c.reg['interface'][2]['enum']['scan_kind']['arm_adi']
    elif invalid=='budget':model.ep.max_op_ms=6
    else:
        describe=model.extension.describe;model.extension.describe=lambda fn:tuple(tlv(DC['ops'],pack_ops(set(ops(r[1]))-{WOP['scan']})) if fn==3 and r[0]==DC['ops'] else r for r in describe(fn))
    r=ScanOptionsChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])


@pytest.mark.parametrize('method,args',[
    ('scan_limit',(0,)),('scan_limit',(4,)),('scan_limit',(True,)),
    ('scan_identity',(True,(0,1),None,0)),('scan_identity',(1,(7,7),None,0)),
    ('scan_identity',(1,(0,1),1,0)),('scan_identity',(3,(4,5),True,0)),
    ('scan_identity',(3,(4,5),0,-1)),('scan_identity',(3,(4,5),0x100000000,0)),
])
def test_invalid_scan_option_controls_preserve_state(method,args):
    model,_,_=setup();before=model.scan_options_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.scan_options_state()==before


def test_scan_options_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();c.reg['describe_common'].update(role_channels=1,max_clock_hz=2,max_length=3,min_clock_hz=4,features=5,channel_group=6)
    r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==33


def test_scan_options_keep_attach_option_contracts():
    from oep_client.conformance_wire_options import WireOptionsChecks
    _,c,a=setup();r=WireOptionsChecks(c,a).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==40
