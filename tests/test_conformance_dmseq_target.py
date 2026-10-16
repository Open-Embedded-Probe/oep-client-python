from oep_client.conformance import Checks
from oep_client.conformance_dmseq_target import TargetDmseqChecks
from oep_client.conformance_dmseq_target_sample import SampleTargetDmseqAdapter
from oep_client.virtual_dmseq_target_model import TargetDmseqModel
from test_conformance_dmseq import setup as probe_setup


def setup():
    _,c,_=probe_setup();model=TargetDmseqModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleTargetDmseqAdapter(model)


def test_target_dmseq_logical_contracts_pass():
    _,c,a=setup();r=TargetDmseqChecks(c,a).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==18 and not r['full_conformance']
    assert all(x['level']=='oep-interface' for x in r['checks'])


import pytest


@pytest.mark.parametrize('fault,case',[
    ('data1_after_data0','LENGTHS'),('short_early','TIMEOUT-SHORT'),('long_early','TIMEOUT-LONG'),
    ('zero_ack','ZERO-SHORT'),('foreign_wait','FOREIGN'),('invalid_m_ack','M'),
    ('duplicate_input','DUPLICATE-INPUT'),('full_input_ack','BACKPRESSURE'),
    ('empty_changes_a','EMPTY-ACK'),('timeout_s_flip','TIMEOUT-SHORT'),
    ('hold_no_reemit','TIMEOUT-SHORT'),('accept_timeout_output','TIMEOUT-RECOVERY'),
    ('syn_retained','SEEDS'),('output_reverse','OUTPUT-ORDER'),('linked_input_reverse','LINKED'),
])
def test_target_checker_detects_independent_faults(fault,case):
    model,c,a=setup();begin=model.target_begin;link=model.target_link_poll
    def faulty_begin(s,ack,data):
        begin(s,ack,data);target=model.target;service=target.service;publish=target.publish;write=target.write_output
        if fault=='short_early':target.deadline=19
        if fault=='long_early':target.long_ms=999
        def broken_publish():
            if fault=='timeout_s_flip' and target.to:target.s^=1
            if fault=='syn_retained' and not target.syn:target.syn=True
            publish()
            if fault=='data1_after_data0' and len(target.payload)>=3:target.events[-2:]=target.events[-2:][::-1]
        def broken_service(advance):
            word=target.data0;was_to=target.to;old_a=target.a
            if fault=='foreign_wait' and word&128 and word!=target.word:
                target.now+=advance;target.events=[('read',4,word)];return
            if fault=='zero_ack' and not word:target.data0=0xf300
            service(advance)
            if fault=='invalid_m_ack' and not word&128 and word&7>=3:target.s^=1
            elif fault=='duplicate_input' and word and not word&128 and (word&7) and (word>>4)&1==old_a:target.input.extend(word.to_bytes(4,'little')[1:1+(word&7)])
            elif fault=='full_input_ack' and word and not word&128 and word&7 and len(target.input)==2:target.a=(word>>4)&1
            elif fault=='empty_changes_a' and word and not word&128 and not word&7:target.a=(word>>4)&1
            elif fault=='hold_no_reemit' and was_to:target.events=target.events[:1]
            elif fault=='output_reverse' and target.payload==b'123456':target.payload=target.payload[::-1];target.publish()
        def broken_write(data):
            if fault=='accept_timeout_output' and target.to:
                target.to=False;value=write(data);target.to=True;return value
            return write(data)
        target.publish=broken_publish;target.service=broken_service;target.write_output=broken_write
        if fault=='data1_after_data0' and len(data)>=3:target.events[-2:]=target.events[-2:][::-1]
    def broken_link(rid):
        result=link(rid)
        if fault=='linked_input_reverse':result['target']['input']=result['target']['input'][::-1]
        return result
    model.target_begin=faulty_begin;model.target_link_poll=broken_link
    r=TargetDmseqChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-DMSEQ-TARGET-'+case)
    assert row['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','short','long','capacity','registry'])
def test_target_preflight_blocks(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='short':a.short_ms=19
    elif invalid=='long':a.long_ms=999
    elif invalid=='capacity':a.input_capacity=3
    else:c.reg['interface'][3]['names']['mechanism'].pop('dmseq')   # a SPEC without dmseq
    r=TargetDmseqChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(x['status']=='blocked' for x in r['checks'][1:]) and not a.trace


@pytest.mark.parametrize('method,args',[
    ('target_begin',(True,1,b'')),('target_begin',(0,2,b'')),('target_begin',(0,1,b'1234567')),
    ('target_host_word',(-1,)),('target_host_word',(True,)),('target_service',(-1,)),
    ('target_service',(1.5,)),('target_read_input',(-1,)),('target_write_output',('x',)),
])
def test_target_controls_reject_invalid_without_mutation(method,args):
    model,_,_=setup();model.target_begin(0,1,b'x');before=model.target_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.target_state()==before
