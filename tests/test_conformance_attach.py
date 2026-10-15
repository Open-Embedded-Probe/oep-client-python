import copy
import pytest
from oep_client import registry as current
from oep_client.conformance import Checks
from oep_client.conformance_attach import AttachChecks
from oep_client.conformance_attach_sample import SampleAttachAdapter
from oep_client.virtual_attach_model import AttachModel
from test_conformance_oep_declarations import SPECS
from test_conformance import REG


def setup():
    reg=copy.deepcopy(REG);reg['interface']=copy.deepcopy(SPECS)
    # a09fa45's attach TLVs (rvswd idle_clock / reset, swio reset, swd targetsel / reset) and console mechanisms
    for row in reg['interface']:
        spec=current.INTERFACES[row['name']]
        if 'attach' in spec.tlv:row['tlv']['attach']=dict(spec.tlv['attach'])
        if 'mechanism' in spec.enum:row['enum']['mechanism']=dict(spec.enum['mechanism'])
    model=AttachModel(lambda:0,boot_id=17);c=Checks(model.handle,reg,'virtual-resource-1')
    return model,c,SampleAttachAdapter(model)


def test_standard_attach_and_console_contracts_pass():
    _,c,a=setup();r=AttachChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==22 and not r['full_conformance']
    assert all(row['level']=='oep-interface' for row in r['checks'])


@pytest.mark.parametrize('invalid',['scope','mechanisms','registry'])
def test_invalid_fixture_or_registry_blocks_before_wire(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='mechanisms':a.mechanisms=[0,1,2]
    else:c.reg['interface'][0]['tlv']['attach']['reset']=7
    r=AttachChecks(c,a).run()
    assert r['checks'][0]['status']=='failed' and not c.trace
    assert all(row['status']=='blocked' for row in r['checks'][1:])


@pytest.mark.parametrize('defect,case',[('zero_speed_taken','WIRE-ZERO-SPEED'),('critical_ignored','WIRE-TLV'),
    ('noncritical_rejected','WIRE-UNMARKED'),('raw_tag_lost','WIRE-TLV'),('last_wins','WIRE-FIRST-WINS'),
    ('later_copy_unchecked','WIRE-MALFORMED'),
    ('refusal_touches','WIRE-PINS'),('console_extra_byte','CONSOLE-RESOURCE'),('arm_console','CONSOLE-RESOURCE'),
    ('duplicate_stream','CONSOLE-DUPLICATE'),('streams_reversed','CONSOLE-INDEPENDENT'),('speed_exceeds','WIRE-BUSY'),('replay_executes','WIRE-REPLAY')])
def test_attach_and_console_mutants_fail(defect,case):
    model,c,a=setup();restart=model.restart_fixture
    def corrupt():
        result=restart();e=model.extension;parse=e.request_tags;dispatch=e.dispatch_request
        def bad_tags(fn,op,data):
            if defect=='critical_ignored':data=data.replace(b'\xfe\x01\0x',b'\x7e\x01\0x')
            values=parse(fn,op,data)
            if defect=='noncritical_rejected' and 5 in values and values[5][0]==5:
                from oep_client.virtual_core import Reject
                raise Reject(11,b'\x05')
            if defect=='last_wins' and fn==1 and op==17 and b'\x85\x04\0\x08\0\x14\0' in data:values[5]=(133,b'\x08\0\x14\0')
            return values
        def raw_lost(fn,op,data):
            from oep_client.virtual_core import Reject
            try:return bad_tags(fn,op,data)
            except Reject as r:
                if defect=='raw_tag_lost' and r.payload[:1] in (b'\x05',b'\x85'):raise Reject(r.reason if hasattr(r,'reason') else 11,bytes((r.payload[0]&127,)))
                raise
        def unchecked(fn,op,data):
            if defect=='later_copy_unchecked' and fn<=3 and op==17:
                first=data.find(b'\x85\x04\0')
                if first>=0 and data[first+7:first+10]==b'\x85\x01\0':data=data[:first+7]
            return raw_lost(fn,op,data)
        def bad_dispatch(fn,op,payload,tags):
            if defect=='zero_speed_taken' and fn<=3 and op==17 and payload[5:9]==b'\0'*4:payload=payload[:5]+b'\xd0\x07\0\0'+payload[9:]
            if defect=='arm_console' and fn==4 and op==16:
                parent=int.from_bytes(payload[:4],'little')
                if parent in e.connections and e.connections[parent]['fn']==3:e.connections[parent]['fn']=1
            if defect=='duplicate_stream' and fn==4 and op==16:
                parent=int.from_bytes(payload[:4],'little')
                for rid,(owner,_) in list(e.streams.items()):
                    if owner==parent:e.close_stream(rid)
            try:value=dispatch(fn,op,payload,tags)
            except Exception:
                if defect=='refusal_touches':e.touch.append(('refused',fn))
                raise
            if defect=='console_extra_byte' and fn==4 and op==16:value=value[:4]+b'\x01'+value[4:]   # an old flags byte
            if defect=='streams_reversed' and fn==4 and op==23 and value[1]>1:   # console §1: live streams in creation order
                rows=[value[2+9*i:11+9*i] for i in range(value[1])];value=value[:2]+b''.join(reversed(rows))+value[2+9*len(rows):]
            if defect=='speed_exceeds' and fn<=3 and op==17 and not getattr(value,'outcome',0):value=value[:9]+b'\xff\xff\xff\x7f'+value[13:]
            return value
        e.request_tags=unchecked;e.dispatch_request=bad_dispatch
        if defect=='replay_executes':
            core=model.ep.current_core;handle=core.handle
            def replay(req,route,**kwargs):
                corr=int.from_bytes(req[1:3],'little')
                if req[3:6]==b'\x01\0\x11' and corr in core.cache:core.cache.pop(corr);core.high=corr-1
                return handle(req,route,**kwargs)
            core.handle=replay
        return result
    model.restart_fixture=corrupt;r=AttachChecks(c,a).run()
    assert r['status']=='failed'
    assert next(row for row in r['checks'] if row['id']=='OEP-ATTACH-'+case)['status']=='failed'


def test_extension_tail_hook_is_after_session_and_replay_checks():
    model,c,_=setup();c.confirm();c.identity();e=model.extension;parse=e.request_tags;calls=[]
    def counted(fn,op,data):calls.append((fn,op));return parse(fn,op,data)
    e.request_tags=counted
    # Invalid critical tail must not be interpreted before SID 0 refusal.
    c.rejected(c.request(17,b'\0\xfe',fn=1),'session_required');assert not calls
    with c.holding(3000) as sid:
        foreign=sid%0xffffffff+1
        c.rejected(c.request(17,b'\0\xfe',fn=1,session=foreign),'locked');assert not calls
        req=c.request(17,b'\0\0\0\x01\0\xd0\x07\0\0',fn=1,session=sid)
        frame=c.exchange(req);assert frame[3:5]==b'\x01\0'
        assert c.exchange(req)==frame and calls==[(1,17)]


def test_owned_fixture_cannot_restart():
    _,c,a=setup();c.confirm();c.identity()
    with c.holding(3000):
        before=a.state()
        with pytest.raises(ValueError):a.restart_fixture()
        assert a.state()==before



def test_connections_metadata_must_match_attached_resource():
    model,c,a=setup();restart=model.restart_fixture
    def corrupt():
        result=restart();e=model.extension;page=e.connection_page
        def bad(fn,first):
            value=page(fn,first)
            if value[1]:value=value[:4]+b'\xff\xff'+value[6:]
            return value
        e.connection_page=bad;return result
    model.restart_fixture=corrupt
    assert AttachChecks(c,a).run()['status']=='failed'


def test_short_reads_are_the_probe_choice():
    """common §1.2 (console §1): the probe decides how much one read returns (max or less, more while bytes remain);
    the checker reads on from where an answer ended (every console read's max cut to 1)."""
    from test_conformance_logic import one_byte_reads
    _,c,a=setup();one_byte_reads(c,fn=4,op=17,at=23);r=AttachChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
