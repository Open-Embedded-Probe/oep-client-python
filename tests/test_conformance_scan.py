import copy
import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_scan import ScanChecks
from oep_client.conformance_scan_sample import SampleScanAdapter
from oep_client.virtual_scan_model import ScanModel
from test_conformance_attach import SPECS
from test_conformance import REG


def setup():
    reg=copy.deepcopy(REG);reg['interface']=copy.deepcopy(SPECS)
    for i,row in enumerate(reg['interface'][:3]):
        row['tlv']['attach']={'max_speed':1,'pins':3};row['tlv']['scan']={'max_speed':1}
        row['enum']['scan_kind']={'arm_adi' if i==2 else 'riscv_dm':2 if i==2 else 1}
    model=ScanModel(lambda:0,boot_id=17);c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c,SampleScanAdapter(model)


def test_finite_scan_contracts_pass():
    _,c,a=setup();r=ScanChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==24 and not r['full_conformance']   # LATER-LOW (core §2.3)
    assert all(row['level']=='oep-interface' for row in r['checks'])


@pytest.mark.parametrize('invalid',['scope','registry','scan_absent'])
def test_invalid_scan_preflight_blocks(invalid):
    model,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='registry':c.reg['interface'][0]['enum']['scan_kind']['riscv_dm']=3
    else:
        describe=model.extension.describe
        model.extension.describe=lambda fn:tuple(row[:4]+bytes((row[4]&~1,)) if row[0]==7 else row for row in describe(fn))
    r=ScanChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])
    assert not a.trace


@pytest.mark.parametrize('fault,case',[
    ('zero_tried','SINGLE'),('wrong_kind','SINGLE'),('wrong_pin','SINGLE'),
    ('wrong_id','SINGLE'),('allocate','SINGLE'),('retain_pin','SINGLE'),
    ('skip_pin','SINGLE'),('extra_candidate','SINGLE'),('touch_refusal','UNDECLARED-LATE'),
    ('unbounded','PREFIX'),('accept_version','VERSIONS'),('repeat_scan','REPLAY'),
])
def test_scan_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request
        def broken(fn,op,payload,tags):
            if fn>3 or op!=16:return dispatch(fn,op,payload,tags)
            if fault=='unbounded':e.budget=3
            if fault=='touch_refusal':
                pairs=[struct.unpack_from('<HH',payload,1+4*i) for i in range(payload[0])]
                if any(pair not in e.pairs[fn] for pair in pairs):e.touch.append(('bad-preflight',fn))
            value=dispatch(fn,op,payload,tags)
            if fault=='zero_tried':value=b'\0'+value[1:]
            elif fault=='wrong_kind' and value[1]:value=value[:2]+b'\x03'+value[3:]
            elif fault=='wrong_pin' and value[1]:value=value[:3]+struct.pack('<H',7)+value[5:]
            elif fault=='wrong_id' and value[1]:value=value[:7]+struct.pack('<I',0xffffffff)+value[11:]
            elif fault=='allocate':e.next_id+=1
            elif fault=='retain_pin':e.scan_live.add(0)
            elif fault=='skip_pin':e.scan_pin_events.pop()
            elif fault=='extra_candidate':e.scan_pin_events.extend([('reserve',fn,(7,7)),('idle',fn,(7,7))])
            elif fault=='accept_version' and not value[1]:
                pair=struct.unpack_from('<HH',payload,1);identity=e.ids.get((fn,pair))
                if identity is not None:value=bytes((value[0],1))+struct.pack('<BHHI',1,*pair,identity)+value[2:]
            return value
        e.dispatch_request=broken
    model.install=faulty_install
    if fault=='repeat_scan':
        handle=model.handle
        def uncached(req):
            if len(req)>=6 and req[5]==16 and struct.unpack_from('<H',req,3)[0] in (1,2,3):
                # Corrupt a cached response while retaining the request identity.
                value=handle(req)
                if model.extension.ids and all(v is None for v in model.extension.ids.values()):return value[:5]+b'\x01\0'+value[16:]
                return value
            return handle(req)
        c.send=uncached
    r=ScanChecks(c,a).run();row=next(row for row in r['checks'] if row['id']=='OEP-SCAN-'+case)
    assert row['status']=='failed',r['checks']


@pytest.mark.parametrize('method,args',[
    ('scan_budget',(0,)),('scan_budget',(True,)),('scan_budget',(4,)),
    ('scan_id',(1,(7,7),0)),('scan_id',(1,(0,1),-1)),('scan_id',(1,(0,1),True)),
    ('pin_mode',(8,'output')),('pin_mode',(0,'unknown')),
])
def test_scan_fixture_rejects_invalid_controls(method,args):
    model,_,_=setup();before=model.scan_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.scan_state()==before
