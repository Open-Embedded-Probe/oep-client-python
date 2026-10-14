"""Core revision-1 fixed result and known TLV contracts with an independent oracle."""
import hashlib
from pathlib import Path
import struct
from .conformance import require,spec_identity

REVIEWED_CORE_SHA256='4c5dcdcc779c5ba0ee85060a33b2b629deddad3e8f8f1a22e18bdeea9d471bdc'
CORE_OPS={'confirm':1,'list':2,'describe':3,'clock':4,'open':16,'end':17,'keepalive':18,'lock_state':19}
CORE_REASONS={'unknown_function':1,'unknown_operation':2,'malformed':3,'unavailable':4,'window_exceeded':6,'no_session':7,'locked':8,'session_required':9,'no_resource':10,'unsupported':11,'result_lost':12}


def reviewed_identity(spec):
    registry,identity=spec_identity(spec);core_sha=hashlib.sha256((Path(spec)/'docs/oep-core.ja.md').read_bytes()).hexdigest()
    require(core_sha==REVIEWED_CORE_SHA256,'core text changed: review result shapes before reusing vectors')
    require(registry['protocol']['revision']==1 and registry['protocol']['byte_order']=='little' and {r['name']:r['code'] for r in registry['core']['op']}==CORE_OPS and registry['reject_reasons']==CORE_REASONS,'selected core revision/numbers differ')
    require(registry['roles']['result']==2 and registry['resolutions']=={'rejected':0,'completed':1} and registry['outcomes']=={'success':0,'failed':1,'partial':2},'selected result header meanings differ')
    tags=registry['core']['tlv'];require(tags['confirm_answer']=={'transport':1} and tags['locked_payload']=={'owner':1} and tags['lock_state_answer']=={'owner':1} and tags['unavailable_payload']=={'cause':1,'channel':2,'fn':5} and tags['unsupported_payload']=={'supported':1,'channel':2,'fn':5},'selected core response tag spaces differ')
    require(registry['describe_common']=={'role_channels':1,'max_clock_hz':2,'max_length':3,'min_clock_hz':4,'features':5,'channel_group':6,'ops':7} and tags['describe']=={'firmware':64,'model':65,'unit_id':66,'channels':67,'label':70,'transport':73,'chip':76,'max_op_ms':77},'selected declaration tag spaces differ')
    return dict(identity,reviewed_core_sha256=core_sha)


def field(tag,value):return struct.pack('<BH',tag,len(value))+value

def result(op,body=b'',*,reason=None,described_fn=0):
    return struct.pack('<BHBB',2,7,int(reason is None),0 if reason is None else reason)+body,op,described_fn


def fixed_vectors():
    # These expected dictionaries are reviewed from core tables, not obtained from the reader.
    rows=[]
    def add(op,body,fixed,reason=None):rows.append((result(op,body,reason=reason),fixed))
    add(1,struct.pack('<4sBHIBI',b'OEP!',1,1024,4096,4,0x12345678),dict(magic=b'OEP!',revision=1,maximum=1024,window=4096,inflight=4,boot=0x12345678))
    entries=[(2,0,1,b'io.example.a'),(9,4,7,b'io.example.b')]
    add(2,struct.pack('<HB',13,2)+b''.join(struct.pack('<HHBB',fn,instance,revision,len(name))+name for fn,instance,revision,name in entries),dict(total=13,entries=[dict(fn=fn,instance=instance,revision=revision,name=name) for fn,instance,revision,name in entries]))
    add(3,b'\0',dict(more=False));add(4,struct.pack('<IQ',0x87654321,0x0102030405060708),dict(boot=0x87654321,uptime_ns=0x0102030405060708))
    add(16,struct.pack('<II',3000,0xabcdef01),dict(lease_ms=3000,boot=0xabcdef01));add(17,b'',{});add(18,b'',{});add(19,struct.pack('<BI',1,1234),dict(locked=True,remaining_ms=1234))
    for reason in (1,2,3,4,6,7,8,9,10,11,12):
        body=struct.pack('<I',4567) if reason==8 else b'\x91' if reason==11 else b''
        add(1,body,dict(remaining_ms=4567) if reason==8 else dict(tag=145) if reason==11 else {},reason)
    return rows


def known_vectors():
    # context, tag, one valid value, structurally wrong lengths/counts, repeated
    desc=result(3,b'\1');other_desc=result(3,b'\1',described_fn=9)
    rows=[]
    common=[(1,b'\2\x34\x12\xa5',(b'',b'\2',b'\2\0'),True),
            (2,b'\x04\x03\x02\1',(b'',b'xxx',b'xxxxx'),False),
            (3,b'\x34\x12',(b'',b'x',b'xxx'),False),
            (4,b'\x08\x07\x06\5',(b'',b'xxx',b'xxxxx'),False),
            (5,b'\xff\xee\xdd\xcc',(b'',b'xxx',b'xxxxx'),False),
            (6,b'\7\2\1\x34\x12\3\x78\x56',(b'',b'\7',b'\7\2\1\x34\x12',b'\7\1\1\x34\x12x'),True),
            (7,b'\x10\xff',(b'',b'\x10',b'\xf9\xff'),False)]
    for ctx in (desc,other_desc):rows.extend((ctx,*row) for row in common)
    rows.extend((desc,*row) for row in [(64,b'firmware',(),False),(65,b'model',(),False),(66,b'unit-0123',(b'',b'x'*33),False),(67,b'\x34\x12',(b'',b'x',b'xxx'),False),(70,b'\x34\x12NRST',(b'',b'x'),True),(73,b'\2\4\1',(b'',b'xx',b'xxxx'),True),(76,b'chip',(),False),(77,b'\x04\3\2\1',(b'',b'xxx',b'xxxxx'),False)])
    confirm=fixed_vectors()[0][0];locked=result(4,struct.pack('<I',900),reason=8);lock_state=result(19,struct.pack('<BI',1,900));unavailable=result(4,reason=4)
    rows.append((confirm,1,b'\3',(b'',b'xx'),False))
    for ctx in (locked,lock_state):rows.append((ctx,1,b'owner',(b'',b'x'*33),False))
    for tag,n,repeated in ((1,1,False),(2,2,True),(5,2,False)):
        rows.append((unavailable,tag,bytes(range(1,n+1)),(b'',b'x'*(n+1)),repeated))
    for op in (1,4):
        ctx=result(op,b'\0',reason=11)
        for tag in ((1,2,5) if op==1 else (2,5)):rows.append((ctx,tag,b'\1\2',(b'',b'x',b'xxx'),tag==2))
    return rows


class HostCoreShapeChecks:
    cases=('FIXED','SHORT','TAIL','KNOWN','KNOWN-WIDTH','DUPLICATES','CONTEXT','VARIABLE','FLAGS-BOOL')
    def __init__(self,decoder,*,spec):self.decoder=decoder;self.identity=reviewed_identity(spec);self.observations=[]
    def read(self,ctx):
        frame,op,fn=ctx;row=dict(frame_hex=frame.hex(),op=op,described_fn=fn);self.observations.append(row)
        try:decoded=self.decoder(frame,op,described_fn=fn)
        except ValueError as e:row['unusable']=str(e);return None
        require(isinstance(decoded,dict),'core reader mapping');row['decoded']=self.json_safe(decoded);return decoded
    @classmethod
    def json_safe(cls,v):
        if isinstance(v,bytes):return {'hex':v.hex()}
        if isinstance(v,dict):return {str(k):cls.json_safe(x) for k,x in v.items()}
        if isinstance(v,(list,tuple)):return [cls.json_safe(x) for x in v]
        return v
    def expect(self,ctx,fixed,rows=()):
        frame,_,_=ctx;require(self.read(ctx)==dict(corr=7,successful=frame[3:5]==b'\1\0',fixed=fixed,tlvs=list(rows)),'core fixed fields/TLV values or context changed')
    @staticmethod
    def append(ctx,tail):return (ctx[0]+tail,*ctx[1:])
    def fixed(self):
        for ctx,expected in fixed_vectors():self.expect(ctx,expected)
    def short(self):
        for ctx,_ in fixed_vectors():
            for n in range(5,len(ctx[0])):require(self.read((ctx[0][:n],*ctx[1:])) is None,'short core fixed/count accepted')
        for n in range(5):require(self.read((b'\2\7\0\1\0'[:n],4,0)) is None,'short result header accepted')
    def tail(self):
        for ctx,expected in fixed_vectors():
            self.expect(self.append(ctx,field(127,b'unknown')),expected)
            for tag in (0,128):require(self.read(self.append(ctx,field(tag,b''))) is None,'zero tail ID accepted')
            for tail in (b'\x7f',b'\x7f\0',b'\x7f\2\0x'):
                require(self.read(self.append(ctx,tail)) is None,'broken unknown tail accepted')
        for ctx,tag,value,_,_ in known_vectors():
            # A high-bit alias is unknown even if its low bits are implemented and its known width is wrong.
            raw=self.append(ctx,field(tag|128,b'')+field(tag,value));decoded=self.read(raw)
            require(decoded is not None and decoded['tlvs']==[(tag,value)],'response critical alias used/refused')
    def known(self):
        for ctx,tag,value,_,_ in known_vectors():
            base=self.read(ctx);require(base is not None,'valid fixed base');self.expect(self.append(ctx,field(tag,value)),base['fixed'],[(tag,value)])
    def known_width(self):
        for ctx,tag,good,bad,_ in known_vectors():
            base=self.read(ctx)
            for value in bad:
                decoded=self.read(self.append(ctx,field(tag,value)))
                require(decoded is None or decoded==base,'wrong known TLV width/count used')
                # core §2.3 (a09fa45): a later same-tag TLV is validated as well; a broken one is never used,
                # whether the reader then drops the whole response or keeps only the valid first value.
                decoded=self.read(self.append(ctx,field(tag,good)+field(tag,value)))
                require(decoded is None or decoded in (dict(base,tlvs=[(tag,good)]),base),'broken later duplicate TLV used')
    def duplicates(self):
        for ctx,tag,value,_,repeat in known_vectors():
            base=self.read(ctx);other=bytes(((value[0]+1)&255,))+value[1:]
            self.expect(self.append(ctx,field(tag,value)+field(127,b'unknown')+field(tag,other)),base['fixed'],[(tag,value),(tag,other)] if repeat else [(tag,value)])
        ctx=result(19,struct.pack('<BI',1,900));self.expect(self.append(ctx,field(1,b'first')+field(1,b'last')),dict(locked=True,remaining_ms=900),[(1,b'first')])
        ctx=result(4,reason=4);self.expect(self.append(ctx,field(2,b'\11\0')+field(2,b'\2\0')),{},[(2,b'\11\0'),(2,b'\2\0')])
    def context(self):
        # tag1 must not leak between u8, u16 and text contexts.
        seq=[(fixed_vectors()[0][0],1,b'\3'),(result(19,struct.pack('<BI',1,900)),1,b'owner'),(result(4,reason=4),1,b'\6'),(result(1,b'\0',reason=11),1,b'\1\2')]
        for _ in range(2):
            for ctx,tag,value in seq:
                base=self.read(ctx);self.expect(self.append(ctx,field(tag,value)),base['fixed'],[(tag,value)])
        for ctx in (result(4,b'\0',reason=11),result(4,struct.pack('<IQ',17,123))):
            base=self.read(ctx);self.expect(self.append(ctx,field(1,b'anything')),base['fixed'])
        ctx=result(3,b'\0',described_fn=9);self.expect(self.append(ctx,field(64,b'extension')),dict(more=False))
    def variable(self):
        for n in (0,1,48,255):
            name=b'x'*(48 if n else 1);entries=b''.join(struct.pack('<HHBB',i+1,0,1,len(name))+name for i in range(n));ctx=result(2,struct.pack('<HB',n,n)+entries)
            self.expect(ctx,dict(total=n,entries=[dict(fn=i+1,instance=0,revision=1,name=name) for i in range(n)]))
        for n in (0,1,255):
            value=bytes((7,n))+b'\1\2\0'*n;ctx=result(3,b'\0');self.expect(self.append(ctx,field(6,value)),dict(more=False),[(6,value)])
        ctx=result(3,b'\0')
        for tag,value in ((1,b'\2\0\0'),(7,b'\0'+b'\xff'*32),(7,b'\xf8\xff'),(70,b'\1\0'),(64,b'x'*65535)):
            self.expect(self.append(ctx,field(tag,value)),dict(more=False),[(tag,value)])
    def flags_bool(self):
        for value in range(256):
            self.expect(result(3,bytes((value,))),dict(more=bool(value)))
            self.expect(result(19,struct.pack('<BI',value,123)),dict(locked=bool(value),remaining_ms=123))
    def run(self):
        rows=[]
        for name in self.cases:
            at=len(self.observations);row=dict(id='HOST-CORE-'+name,status='passed',clause='core §2.2/2.3/2.4/4.3/6.4/7/11')
            try:getattr(self,name.lower().replace('-','_'))()
            except Exception as e:row.update(status='failed',error=f'{type(e).__name__}: {e}')
            row['observations']=self.observations[at:];rows.append(row)
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',level='core',subject='Host actual core result shape reader component',spec=self.identity,full_conformance=False,checks=rows,
                    adopted_shapes={'core_success_operations':8,'core_reject_reasons':11,'known_tlv_context_entries':len(known_vectors())},
                    sources_sha256={n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in ('conformance_host_core.py','conformance_host_core_sample.py')},
                    unchecked=['request encoders/critical policy and Probe generation','interface fixed shapes and TLVs','paging aggregation, mandatory declaration presence and metadata semantics','correlation, replay/recovery/notifications/transport bounds','production Host and physical behavior'])
