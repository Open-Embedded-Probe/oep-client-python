"""Independent decoder faults against actual core shape vectors."""
import hashlib
import pytest
from oep_client import conformance_host_core as cc
from oep_client.conformance import Violation
from oep_client.conformance_host_core_sample import decode

SPEC='/explicit/spec-input'
ORIGINAL_IDENTITY=cc.reviewed_identity


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    def identity(path):
        assert path==SPEC
        return dict(commit='unit-test-fixture',dirty=False,registry_sha256='fixture',reviewed_core_sha256=cc.REVIEWED_CORE_SHA256)
    monkeypatch.setattr(cc,'reviewed_identity',identity)


def test_core_reader_contracts():
    r=cc.HostCoreShapeChecks(decode,spec=SPEC).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
    assert len(r['checks'])==9 and r['adopted_shapes']=={'core_success_operations':8,'core_reject_reasons':11,'known_tlv_context_entries':33}
    assert not r['full_conformance']


@pytest.mark.parametrize('index',range(19))
def test_wrong_fixed_response_fields_are_detected(index):
    target=cc.fixed_vectors()[index][0]
    def broken(frame,op,**kw):
        out=decode(frame,op,**kw)
        if (frame,op,kw['described_fn'])==target:out['fixed']={'wrong':True}
        return out
    r=cc.HostCoreShapeChecks(broken,spec=SPEC).run()
    assert next(x for x in r['checks'] if x['id']=='HOST-CORE-FIXED')['status']=='failed'


@pytest.mark.parametrize('index',[i for i,row in enumerate(cc.known_vectors()) if row[3]])
def test_each_broken_known_context_is_detected(index):
    ctx,tag,value,bad,_=cc.known_vectors()[index];target=cc.HostCoreShapeChecks.append(ctx,cc.field(tag,bad[0]))
    def broken(frame,op,**kw):
        if (frame,op,kw['described_fn'])==target:
            out=decode(*ctx[:2],described_fn=ctx[2]);out['tlvs']=[(tag,bad[0])];return out
        return decode(frame,op,**kw)
    r=cc.HostCoreShapeChecks(broken,spec=SPEC).run()
    assert next(x for x in r['checks'] if x['id']=='HOST-CORE-KNOWN-WIDTH')['status']=='failed'


@pytest.mark.parametrize('index',[i for i,row in enumerate(cc.known_vectors()) if row[3]])
def test_each_broken_later_duplicate_is_not_used(index):
    # core §2.3 at a09fa45: the later same-tag TLV is validated too; a reader using it unvalidated fails.
    ctx,tag,value,bad,_=cc.known_vectors()[index];target=cc.HostCoreShapeChecks.append(ctx,cc.field(tag,value)+cc.field(tag,bad[0]))
    def broken(frame,op,**kw):
        if (frame,op,kw['described_fn'])==target:
            out=decode(*ctx[:2],described_fn=ctx[2]);out['tlvs']=[(tag,value),(tag,bad[0])];return out
        return decode(frame,op,**kw)
    r=cc.HostCoreShapeChecks(broken,spec=SPEC).run()
    row=next(x for x in r['checks'] if x['id']=='HOST-CORE-KNOWN-WIDTH')
    assert row['status']=='failed' and 'later duplicate' in row['error'],row


def test_unsupported_index_is_not_a_core_tag(tmp_path,monkeypatch):
    # a09fa45 moved unsupported/unavailable index to each interface's 0x40-0x7F space.
    core=tmp_path/'docs/oep-core.ja.md';core.parent.mkdir();core.write_bytes(b'reviewed unit core')
    monkeypatch.setattr(cc,'REVIEWED_CORE_SHA256',hashlib.sha256(core.read_bytes()).hexdigest())
    registry=registry_fixture();registry['core']['tlv']['unsupported_payload']['index']=64
    monkeypatch.setattr(cc,'spec_identity',lambda path:(registry,{'commit':'unit-fixture'}))
    with pytest.raises(Exception,match='tag spaces differ'):ORIGINAL_IDENTITY(tmp_path)


@pytest.mark.parametrize('fault,case',[
    ('critical_alias','TAIL'),('unknown_tag_refusal','TAIL'),('zero_id','TAIL'),
    ('broken_unknown','TAIL'),('short_fixed','SHORT'),('entry_count','FIXED'),
    ('element_length','FIXED'),('last_owner','DUPLICATES'),('sort_channels','DUPLICATES'),
    ('unsupported_cause','CONTEXT'),('describe_space','CONTEXT'),('cross_call_cache','CONTEXT'),
    ('strict_bool','FLAGS-BOOL'),
    ('reject_unknown_revision','FIXED'),('length_u8','VARIABLE'),('tiny_limit','VARIABLE')])
def test_semantic_reader_faults_are_detected(fault,case):
    cache={}
    def broken(frame,op,**kw):
        body=frame[5:]
        if fault=='critical_alias':
            marker=b'\x81\0\0\1\1\0\3'
            if frame.endswith(marker):frame=frame[:-len(marker)]+b'\1\1\0\x63\1\1\0\3'
        if fault=='unknown_tag_refusal' and frame.endswith(cc.field(127,b'unknown')):raise ValueError('unknown tag')
        if fault=='zero_id' and frame[-3:] in (b'\0\0\0',b'\x80\0\0'):frame=frame[:-3]
        if fault=='broken_unknown' and frame.endswith(b'\x7f\2\0x'):frame=frame[:-4]
        if fault=='short_fixed':
            try:return decode(frame,op,**kw)
            except ValueError:return dict(corr=7,successful=True,fixed={},tlvs=[])
        if fault=='length_u8' and len(frame)>65535:raise ValueError('u8 length')
        if fault=='tiny_limit' and len(frame)>128:raise ValueError('transport bound applied to payload component')
        out=decode(frame,op,**kw)
        if fault=='entry_count' and op==2 and out['successful']:out['fixed']['entries']=out['fixed']['entries'][:1]
        if fault=='element_length' and op==2 and out['successful']:raise ValueError('expected entry length prefix')
        if fault=='reject_unknown_revision' and op==2 and out['successful'] and any(e['revision']!=1 for e in out['fixed']['entries']):raise ValueError('unknown revision aborts list')
        if fault=='last_owner' and frame.endswith(cc.field(1,b'first')+cc.field(1,b'last')):out['tlvs']=[(1,b'last')]
        if fault=='sort_channels' and frame[3:5]==b'\0\4':out['tlvs'].sort()
        if fault=='unsupported_cause' and frame[3:5]==b'\0\x0b' and op==1 and frame.endswith(cc.field(1,b'\1\2')):out['tlvs']=[(1,b'\1')]
        if fault=='describe_space' and op==3 and kw['described_fn']!=0 and frame.endswith(cc.field(64,b'extension')):out['tlvs']=[(64,b'extension')]
        if fault=='cross_call_cache' and out['tlvs']:
            tag,value=out['tlvs'][0]
            if tag in cache:out['tlvs'][0]=(tag,cache[tag])
            else:cache[tag]=value
        if fault=='strict_bool' and op in (3,19) and out['successful'] and body[0]>1:out['fixed']['more' if op==3 else 'locked']=False
        return out
    checker=cc.HostCoreShapeChecks(broken,spec=SPEC)
    # Cache fault is observed within the deliberately interleaved contexts, independent of earlier checks.
    if fault=='cross_call_cache':checker.cases=('CONTEXT',)
    r=checker.run()
    row=next(x for x in r['checks'] if x['id']=='HOST-CORE-'+case)
    assert row['status']=='failed',row


def test_broken_known_field_may_be_ignored_without_use():
    def tolerant(frame,op,**kw):
        try:return decode(frame,op,**kw)
        except ValueError as e:
            if str(e)!='known core TLV shape':raise
            for ctx,tag,_,bad,_ in cc.known_vectors():
                for value in bad:
                    if (frame,op,kw['described_fn'])==cc.HostCoreShapeChecks.append(ctx,cc.field(tag,value)):
                        return decode(*ctx[:2],described_fn=ctx[2])
            raise
    assert cc.HostCoreShapeChecks(tolerant,spec=SPEC).run()['status']=='passed'


@pytest.mark.parametrize('fault',['text','revision','op','tlv','common'])
def test_changed_reviewed_spec_stops_before_reader_use(tmp_path,monkeypatch,fault):
    core=tmp_path/'docs/oep-core.ja.md';core.parent.mkdir();core.write_bytes(b'reviewed unit core')
    monkeypatch.setattr(cc,'REVIEWED_CORE_SHA256',hashlib.sha256(core.read_bytes()).hexdigest())
    registry=registry_fixture()
    monkeypatch.setattr(cc,'spec_identity',lambda path:(registry,dict(commit='unit',dirty=False)))
    assert ORIGINAL_IDENTITY(tmp_path)['reviewed_core_sha256']==cc.REVIEWED_CORE_SHA256
    if fault=='text':core.write_bytes(b'changed core')
    elif fault=='revision':registry['protocol']['revision']=2
    elif fault=='op':registry['core']['op'][0]['code']=9
    elif fault=='tlv':registry['core']['tlv']['confirm_answer']['transport']=2
    else:registry['describe_common']['ops']=8
    with pytest.raises(Violation):ORIGINAL_IDENTITY(tmp_path)


def registry_fixture():
    return dict(protocol={'revision':1,'byte_order':'little'},roles={'result':2},resolutions={'rejected':0,'completed':1},outcomes={'success':0,'failed':1,'partial':2},reject_reasons=dict(cc.CORE_REASONS),describe_common={'role_channels':1,'max_clock_hz':2,'max_length':3,'min_clock_hz':4,'features':5,'channel_group':6,'ops':7},core={'op':[{'name':n,'code':v} for n,v in cc.CORE_OPS.items()], 'tlv':{'confirm_answer':{'transport':1},'locked_payload':{'owner':1},'lock_state_answer':{'owner':1},'unavailable_payload':{'cause':1,'channel':2,'fn':5},'unsupported_payload':{'supported':1,'channel':2,'fn':5},'describe':{'firmware':64,'model':65,'unit_id':66,'channels':67,'label':70,'transport':73,'chip':76,'max_op_ms':77}}})
