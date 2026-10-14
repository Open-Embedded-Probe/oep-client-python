import copy
import struct
import pytest
from oep_client import registry as registry
from oep_client.conformance import Checks
from oep_client.conformance_analog import AnalogChecks
from oep_client.conformance_analog_sample import SampleAnalogAdapter
from oep_client.virtual_analog_model import AnalogModel
from oep_client.virtual_core import tlv
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,checks=registered_setup();reg=checks.reg
    for spec in reg['interface']:
        if spec['name']=='oep.probe.plan':spec['tlv']=copy.deepcopy(registry.PROBE_PLAN.tlv)
        if spec['name']=='oep.fixture.analog':
            spec['tlv']=copy.deepcopy(registry.FIXTURE_ANALOG.tlv);spec['enum']=copy.deepcopy(registry.FIXTURE_ANALOG.enum)
    model=AnalogModel(boot_id=17)
    return model,Checks(model.handle,reg,'virtual-resource-1')


def test_analog_operations():
    model,checks=setup();result=AnalogChecks(checks,SampleAnalogAdapter(model)).run()
    assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
    assert len([r for r in result['checks'] if r['id'].startswith('OEP-ANALOG-')])==len(AnalogChecks.cases)+1
    assert not result['full_conformance']


@pytest.mark.parametrize('checker_name',['declarations','metadata'])
def test_analog_declarations(checker_name):
    from oep_client.conformance_interfaces import InterfaceChecks
    from oep_client.conformance_capture_metadata import CaptureMetadataChecks
    model,checks=setup()
    result=(InterfaceChecks(checks) if checker_name=='declarations' else CaptureMetadataChecks(checks,{2:'oep.fixture.analog'})).run()
    assert result['status']=='passed',result['checks']


@pytest.mark.parametrize('case',AnalogChecks.cases)
def test_bad_wire_result_is_detected(case):
    model,checks=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2:
            reply=reply[:4]+bytes(((reply[4]+1)&255,))+reply[5:]
        return reply
    checks.send=broken;checker=AnalogChecks(checks,SampleAnalogAdapter(model));checker.cases=(case,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-ANALOG-'+case)['status']=='failed'


@pytest.mark.parametrize('method,args',[('analog_feed',([[True]],)),('analog_feed',([[0,4096]],)),('analog_advance',(-1,)),('analog_fail',(2,)),('analog_seed',(True,))])
def test_invalid_controls_do_not_change_model(method,args):
    model,_=setup();before=model.analog_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.analog_state()==before


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_preflight_blocks_operations(invalid):
    model,checks=setup();adapter=SampleAnalogAdapter(model)
    if invalid=='scope':adapter.reset_scope='physical'
    else:adapter.selection={2:'oep.fixture.analog'}
    result=AnalogChecks(checks,adapter).run()
    assert result['checks'][0]['status']=='failed'
    assert all(r['status']=='blocked' for r in result['checks'][1:]) and not adapter.trace


@pytest.mark.parametrize('fault,case',[
 ('wrong_pack','RAW'),('wrong_order','RAW'),('generation_zero','GEN-EXHAUST'),('wrap_to_one','GEN-EXHAUST'),
 ('missing_scale','RAW'),('missing_scale','QUERY-STATE-3'),('truncate_factory','CALIBRATION-FIT'),('omit_vrefint','CALIBRATION-FIT'),
 ('partial_calibration','CALIBRATION-FIT'),('gone_without_gap','READ-GAP'),('mode3_accepted','RAW-TAGS'),
 ('keep_old_data','GENERATION'),('reset_generation','PLAN-RELEASE'),('no_resume','RELEASE'),
 ('no_gap','REPEAT'),('no_short','SHORT-STOP'),('wrong_stop_state','SHORT-STOP'),
 ('pin_output','RAW'),('discard_committed','ERROR'),('error_retains_partial','ERROR'),
 ('query_applies','QUERY-STATE-3'),('future_reference','CALIBRATION'),
 ('calibration_changes_data','CALIBRATION'),('ignore_frontend','FRONTEND'),
])
def test_semantic_faults_are_detected(fault,case):
    model,checks=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;finish=e.finish;release=e.release_plans
        def broken_dispatch(fn,op,payload,tags):
            from oep_client.virtual_core import Reject, Completed
            old_data=bytes(e.data);old_state=e.state
            if fn==2:
                if fault=='wrap_to_one' and op==17 and e.generation==0xffffffff and e.state in (1,4,6):e.generation=0
                if fault=='mode3_accepted' and op in (16,24) and tags.get(64,(0,b''))[1]==b'\3':tags={**tags,64:(64,b'\1'),67:(67,struct.pack('<I',8))}
                if op==25 and fault in ('truncate_factory','omit_vrefint','partial_calibration'):
                    raw,ns=e.factory_raw,e.calibration_ns
                    try:return dispatch(fn,op,payload,tags)
                    except Reject:
                        if fault=='omit_vrefint':e.calibration_ns=None
                        else:e.factory_raw=raw[:max(0,len(raw)-19)]
                        result=dispatch(fn,op,payload,tags)
                        return Completed(result,2) if fault=='partial_calibration' else result
                    finally:e.factory_raw,e.calibration_ns=raw,ns
            result=dispatch(fn,op,payload,tags)
            if fn==2:
                if fault=='generation_zero' and op==17:e.generation=0;result=b'\0'*4
                if fault=='missing_scale' and op in (16,24):result=result.replace(tlv(85,struct.pack('<Bii',3,-17,1000000)),b'').replace(tlv(85,struct.pack('<Bii',3,-17,-1000000)),b'')
                if fault=='gone_without_gap' and op==21 and result[8]&2 and result[9:11]==b'\0\0':result=result[:8]+bytes((result[8]&~2,))+result[9:]
                if fault=='keep_old_data' and op==17:e.data[:]=old_data
                if fault=='no_resume' and op==23:e.state=5
                if fault=='no_gap' and op==23:e.gap=False
                if fault=='wrong_stop_state' and op==18 and old_state==3:e.state=4
                if fault=='query_applies' and op==24:e.config=e.settings(tags);e.clear(1)
                if fault=='calibration_changes_data' and op==25:e.data.append(255)
                if fault=='future_reference' and op==25 and e.calibration_ns is not None:
                    from oep_client.conformance import tlvs
                    result=b''.join(tlv(tag,struct.pack('<IQI',1365,e.calibration_ns+1000,1100) if tag==2 else value) for tag,value in tlvs(result))
            return result
        def broken_finish(short=False):
            finish(short)
            if fault=='wrong_pack' and e.data:e.data[0]^=4
            if fault=='wrong_order' and len(e.data)>=4:e.data[:4]=e.data[2:4]+e.data[:2]
            if fault=='no_short' and e.segments:
                row=e.segments[-1];e.segments[-1]=row[:6]+(0,)+row[7:]
            if fault=='pin_output':e.pins[1]=(4,0)
        def broken_release(functions):
            release(functions)
            if fault=='reset_generation':e.generation=0
        e.dispatch_request=broken_dispatch;e.finish=broken_finish;e.release_plans=broken_release
        if fault=='ignore_frontend':
            original=e.answer
            def broken_answer(config):
                return original(dict(config,frontend={role:0 for role in e.roles}))
            e.answer=broken_answer
    model.install=faulty_install
    if fault in ('discard_committed','error_retains_partial'):
        original=model.analog_fail
        def broken_fail(error):
            original(error)
            if fault=='discard_committed':model.extension.data.clear();model.extension.segments.clear()
            else:model.extension.data.append(255)
        model.analog_fail=broken_fail
    checker=AnalogChecks(checks,SampleAnalogAdapter(model));checker.cases=(case,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-ANALOG-'+case)['status']=='failed'


@pytest.mark.parametrize('case',['REPLAY-START','REPLAY-RELEASE'])
def test_replay_cannot_repeat_adc_side_effects(case):
    model,checks=setup();original=model.handle;seen=set();op=17 if case=='REPLAY-START' else 23
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==op:
            if req in seen:
                if case=='REPLAY-START':model.extension.clear(3);model.extension.generation+=1
                else:model.extension.segments.clear();model.extension.data.clear()
            seen.add(req)
        return reply
    checks.send=broken;checker=AnalogChecks(checks,SampleAnalogAdapter(model));checker.cases=(case,)
    result=checker.run()
    assert next(r for r in result['checks'] if r['id']=='OEP-ANALOG-'+case)['status']=='failed'


@pytest.mark.parametrize('case',['QUERY','RAW','FRONTEND','QUERY-STATE-3'])
def test_optional_answer_rows_may_be_omitted(case):
    from oep_client.conformance import tlvs
    model,checks=setup();install=model.install
    def lean_install():
        install();e=model.extension;answer=e.answer
        e.answer=lambda config:b''.join(tlv(tag,value) for tag,value in tlvs(answer(config)) if tag not in (87,88,89))
    model.install=lean_install;checker=AnalogChecks(checks,SampleAnalogAdapter(model));checker.cases=(case,)
    result=checker.run();assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
