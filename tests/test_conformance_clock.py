import copy
import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_clock import ClockChecks
from oep_client.conformance_clock_sample import SampleClockAdapter
from oep_client.virtual_clock_model import ClockModel
from test_conformance import REG


def setup():
    model=ClockModel();return model,Checks(model.handle,copy.deepcopy(REG),'virtual-resource-1'),SampleClockAdapter(model)


def test_clock_contracts_pass():
    _,c,a=setup();r=ClockChecks(c,a).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==9 and all(x['level']=='core' for x in r['checks']) and not r['full_conformance']


@pytest.mark.parametrize('fault,case',[
    ('frozen','U64'),('u32','U64'),('signed','U64'),('boot','U64'),('tail','U64'),
    ('estimate','READ-POINT'),('before','READ-POINT'),('after','READ-POINT'),('zero_cache','SIDZERO-FRESH'),
    ('renew','SIDZERO-STATE'),('history','SIDZERO-STATE'),('replay','REPLAY')])
def test_clock_checker_detects_independent_faults(fault,case):
    model,c,a=setup();handle=model.handle;last_zero=None
    def corrupt(request):
        nonlocal last_zero
        before=len(model.reads);frame=handle(request)
        if request[3:6]!=b'\0\0\x04' or frame[3:5]!=b'\x01\0':return frame
        sid=int.from_bytes(request[6:10],'little');boot,value=struct.unpack_from('<IQ',frame,5)
        if fault=='frozen':value=0
        elif fault=='u32':value&=0xffffffff
        elif fault=='signed':value&=0x7fffffffffffffff
        elif fault=='boot':boot=(boot+1)&0xffffffff
        elif fault=='tail':return frame+b'\x7f\x05\0x'
        elif fault=='estimate':value+=1
        elif fault=='before' and model.exchanges[-1]['received_ns']>0:value=model.exchanges[-1]['received_ns']-1
        elif fault=='after':value=model.exchanges[-1]['sent_ns']+1
        elif fault=='zero_cache' and not sid:
            if last_zero is not None and last_zero[0]==request:return last_zero[1]
            last_zero=(request,frame)
        elif fault=='renew' and not sid and model.ep.current_core.holder is not None:model.ep.current_core.deadline+=1
        elif fault=='history' and not sid and model.ep.current_core.holder is not None:model.ep.current_core.high+=1
        elif fault=='replay' and sid and len(model.reads)==before:value=model.ep.now_ns()
        return frame[:5]+struct.pack('<IQ',boot,value)+frame[17:]
    c.send=corrupt;r=ClockChecks(c,a).run()
    assert next(x for x in r['checks'] if x['id']=='CORE-CLOCK-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','reference','registry'])
def test_clock_preflight_blocks_before_wire(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='reference':a.reference=''
    else:c.core['clock']=3
    r=ClockChecks(c,a).run()
    assert r['checks'][0]['status']=='failed' and not c.trace and not a.trace
    assert all(x['status']=='blocked' for x in r['checks'][1:])


@pytest.mark.parametrize('phase',['replay','refusal'])
def test_clock_checker_allows_incidental_internal_clock_reads(phase):
    model,c,a=setup();handle=model.handle;seen=[]
    def observed(request):
        first=len(model.reads);frame=handle(request)
        if request[3:6]==bytes((0,0,4)):
            sid=int.from_bytes(request[6:10],'little')
            if (phase=='replay' and sid and len(model.reads)==first) or (phase=='refusal' and frame[3]==0):
                model.ep.now_ns();seen.append(request)
        return frame
    c.send=observed;r=ClockChecks(c,a).run()
    assert r['status']=='passed' and seen,r['checks']
