"""Independent encoder faults must fail the relevant Host policy checks."""
import hashlib

import pytest
from oep_client.conformance_host_requests import HostRequestChecks, contexts, REVIEWED, REVIEWED_REGISTRY_SHA256, reviewed_identity
from oep_client.conformance_host_request_sample import encode

SPEC='/explicit/request-policy-spec'


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_host_requests.reviewed_identity',
                        lambda path: {'commit':'unit-fixture'} if path==SPEC else None)


def test_adopted_policy_passes():
    r=HostRequestChecks(encode,spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert r['contexts']==12 and len(r['checks'])==5 and not r['full_conformance']


def map_tags(raw, fixed_len, transform):
    out=raw[:fixed_len];pos=fixed_len
    while pos<len(raw):
        n=int.from_bytes(raw[pos+1:pos+3],'little')
        out+=bytes((transform(raw[pos]),))+raw[pos+1:pos+3+n];pos+=3+n
    return out


POLICIES=[(name,op,tag,critical) for name,op,fixed,fields in contexts()
          for tag,critical in dict((t,c) for t,v,c in fields).items()]


@pytest.mark.parametrize('interface,operation,tag,critical',POLICIES)
def test_each_adopted_field_policy_is_checked(interface,operation,tag,critical):
    def broken(name,op,fixed,fields):
        raw=encode(name,op,fixed,fields)
        if (name,op)==(interface,operation):
            raw=map_tags(raw,len(fixed),lambda t:t^128 if t&127==tag else t)
        return raw
    r=HostRequestChecks(broken,spec=SPEC).run()
    row=next(x for x in r['checks'] if x['id']=='HOST-REQUEST-'+('CRITICAL' if critical else 'NONCRITICAL'))
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('defect,case',[
    ('all_critical','NONCRITICAL'),('none_critical','CRITICAL'),
    ('sort','ORDER'),('drop_repeat','NONCRITICAL'),('drop_fixed','CRITICAL'),
    ('zero_value','CRITICAL'),('big_endian_length','CRITICAL'),
    ('u8_length','VALUES'),('drop_empty','VALUES'),
    ('accept_highbit_input','INVALID'),('accept_zero_input','INVALID'),
    ('truncate_overflow','INVALID'),('tag_only_cache','NONCRITICAL'),
])
def test_semantic_faults(defect,case):
    cache={}
    def broken(name,op,fixed,fields):
        if defect=='sort': fields=sorted(fields,key=lambda row:row[0])
        if defect=='drop_repeat': fields=list(dict(fields).items())
        if defect=='drop_fixed': fixed=b''
        if defect=='zero_value': fields=[(t,b'\0'*len(v)) for t,v in fields]
        if defect=='drop_empty': fields=[(t,v) for t,v in fields if v]
        if defect=='accept_highbit_input': fields=[(t&127 or 1,v) for t,v in fields]
        if defect=='accept_zero_input': fields=[(t or 1,v) for t,v in fields]
        if defect=='truncate_overflow': fields=[(t,v[:65535]) for t,v in fields]
        raw=encode(name,op,fixed,fields)
        if defect in ('all_critical','none_critical','tag_only_cache'):
            def change(t):
                if defect=='all_critical':return t|128
                if defect=='none_critical':return t&127
                return cache.setdefault(t&127,t)
            raw=map_tags(raw,len(fixed),change)
        if defect in ('big_endian_length','u8_length'):
            out=raw[:len(fixed)];pos=len(fixed)
            while pos<len(raw):
                n=int.from_bytes(raw[pos+1:pos+3],'little')
                size=n.to_bytes(2,'big') if defect=='big_endian_length' else bytes((n&255,0))
                out+=raw[pos:pos+1]+size+raw[pos+3:pos+3+n];pos+=3+n
            raw=out
        return raw
    r=HostRequestChecks(broken,spec=SPEC).run()
    row=next(x for x in r['checks'] if x['id']=='HOST-REQUEST-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('changed',list(REVIEWED))
def test_changed_normative_policy_requires_review(tmp_path,monkeypatch,changed):
    # Exercise the actual guard function; fixtures need not copy neighboring repos.
    hashes={}
    for name in REVIEWED:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name)
        hashes[name]=hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setattr('oep_client.conformance_host_requests.REVIEWED',hashes)
    registry=registry_fixture()
    monkeypatch.setattr('oep_client.conformance_host_requests.spec_identity',lambda p:(registry,{'registry_sha256':REVIEWED_REGISTRY_SHA256}))
    reviewed_identity(tmp_path)
    (tmp_path/changed).write_text('changed policy')
    with pytest.raises(Exception,match='request policy text changed'):reviewed_identity(tmp_path)


def registry_fixture():
    entries={}
    for name,op,fixed,fields in contexts():
        entry=entries.setdefault(name,dict(name=name,revision=1,op=[],tlv={}))
        entry['op'].append(dict(name=op,code=16 if op in ('scan','configure','bind','open') else 17 if op=='attach' else 24))
        entry['tlv']['configure' if op=='query' else op]={str(t):t for t,v,c in fields}
    return dict(protocol=dict(revision=1,byte_order='little'),core=entries.pop('core'),interface=list(entries.values()))


@pytest.mark.parametrize('change',['registry_hash','protocol','revision','operation','tag'])
def test_selected_schema_guard(tmp_path,monkeypatch,change):
    hashes={}
    for name in REVIEWED:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name)
        hashes[name]=hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setattr('oep_client.conformance_host_requests.REVIEWED',hashes)
    registry=registry_fixture();identity={'registry_sha256':REVIEWED_REGISTRY_SHA256}
    monkeypatch.setattr('oep_client.conformance_host_requests.spec_identity',lambda p:(registry,identity))
    reviewed_identity(tmp_path)
    if change=='registry_hash':identity['registry_sha256']='changed'
    if change=='protocol':registry['protocol']['revision']=2
    if change=='revision':registry['interface'][0]['revision']=2
    if change=='operation':registry['interface'][0]['op'][0]['code']=32
    if change=='tag':registry['interface'][0]['tlv']['scan']['1']=7
    with pytest.raises(Exception,match='request'):reviewed_identity(tmp_path)
