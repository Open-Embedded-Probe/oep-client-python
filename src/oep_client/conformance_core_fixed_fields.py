"""Reviewed core fixed-field presence; zero values never imply omitted bytes."""
import struct
from .conformance import require
from .conformance_host_core import HostCoreShapeChecks,CORE_OPS,fixed_vectors,result,field

# Reviewed from core §1.1/4.3/6.4/7 (oep-spec a09fa45: confirm and list entries carry no flags), not inferred from a generated schema.
# Each tuple lists all mandatory request fields and fixed response prefixes.
CORE_SHAPES=(
 ('confirm',1,(('magic',4),('min_rev',1),('max_rev',1)),(('magic',4),('revision',1),('max_frame',2),('window',4),('max_inflight',1),('boot_id',4))),
 ('list',2,(('first',2),),(('total',2),('count',1))),
 ('describe',3,(('fn',2),('first',2)),(('more',1),)),
 ('clock',4,(),(('boot_id',4),('uptime_ns',8))),
 ('open',16,(('lease_ms',4),('force',1)),(('lease_ms',4),('boot_id',4))),
 ('end',17,(),()),('keepalive',18,(),()),
 ('lock_state',19,(),(('locked',1),('remaining_ms',4))),
)
REQUESTS={1:b'OEP?\1\1',2:b'\0\0',3:b'\0'*4,4:b'',16:b'\0'*5,17:b'',18:b'',19:b''}


def reviewed_shapes():
    require({name:op for name,op,_,_ in CORE_SHAPES}==CORE_OPS,'review inventory must cover all8 core operations')
    require([sum(n for _,n in req) for _,_,req,_ in CORE_SHAPES]==[6,2,4,0,5,0,0,0],'reviewed request widths differ')
    require([sum(n for _,n in reply) for _,_,_,reply in CORE_SHAPES]==[16,3,1,12,8,0,0,5],'reviewed result prefixes differ')
    return [dict(name=name,op=op,request=[dict(name=k,width=n,optional=False) for k,n in req],
                 response_prefix=[dict(name=k,width=n,optional=False) for k,n in reply]) for name,op,req,reply in CORE_SHAPES]


def drop_fields(body,fields):
    offset=0
    for name,n in fields:
        yield name,body[:offset]+body[offset+n:]
        offset+=n


class CoreFixedFieldChecks(HostCoreShapeChecks):
    cases=('REVIEW','REQUEST-FIELDS','RESPONSE-FIELDS','ZERO-VALUES','OWNER-TLV')
    def __init__(self,decoder,probe_factory,*,spec):
        super().__init__(decoder,spec=spec);self.probe_factory=probe_factory;self.probe_traces=[]
    def review(self):
        inventory=reviewed_shapes()
        require(all(not field['optional'] for row in inventory for kind in ('request','response_prefix') for field in row[kind]),'optional core fixed field')
        self.observations.append(dict(action='review',inventory=inventory,all_core_operations=True,interface_shapes_reviewed=False))
    def request_fields(self):
        for name,op,fields,_ in CORE_SHAPES:
            c=self.probe_factory();c.confirm();c.identity()
            try:
                body=REQUESTS[op]
                for field_name,short in drop_fields(body,fields):
                    req=c.request(op,short,session=42 if op==16 else 0)
                    c.rejected(req,'malformed')
                    self.observations.append(dict(action='missing-request-field',op=op,field=field_name,request_hex=req.hex()))
                if op in (17,18):
                    with c.holding() as sid:
                        c.success(c.request(op,session=sid))
                        if op==17:c.session=None
                else:
                    payload=c.success(c.request(op,body,session=42 if op==16 else 0))
                    if op==16:
                        c.session=42
                        require(len(payload)>=8 and 1000<=int.from_bytes(payload[:4],'little')<=60000,'lease0 default response shape')
                        c.success(c.request(c.core['end'],session=42));c.session=None
                    self.observations.append(dict(action='complete-request',op=op,payload_hex=payload.hex()))
            finally:self.probe_traces.extend(c.trace)
    def response_fields(self):
        vectors=fixed_vectors()
        for index,(name,op,_,fields) in enumerate(CORE_SHAPES):
            ctx=vectors[index][0]
            # Empty list still carries total and count; element audit is separate.
            if op==2:ctx=result(2,b'\0'*3)
            for field_name,short in drop_fields(ctx[0][5:],fields):
                require(self.read((ctx[0][:5]+short,*ctx[1:])) is None,'omitted result fixed field accepted: '+name+'.'+field_name)
        body=struct.pack('<HBHHBB',1,1,2,0,1,1)+b'a';ctx=result(2,body)
        entry_fields=(('fn',2),('instance',2),('revision',1),('name_len',1),('name',1))
        for field_name,short in drop_fields(body[3:],entry_fields):
            require(self.read(result(2,body[:3]+short)) is None,'omitted list entry field accepted: '+field_name)
        for ctx,_ in vectors[8:]:
            if ctx[0][4] in (8,11):require(self.read((ctx[0][:5],*ctx[1:])) is None,'omitted reject fixed field accepted')
    def zero_values(self):
        self.expect(result(2,b'\0'*3),dict(total=0,entries=[]))
        self.expect(result(3,b'\0'),dict(more=False))
        self.expect(result(4,b'\0'*12),dict(boot=0,uptime_ns=0))
        self.expect(result(19,b'\0'*5),dict(locked=False,remaining_ms=0))
        self.expect(result(4,b'\0',reason=11),dict(tag=0))
        for op in (17,18):self.expect(result(op),{})
    def owner_tlv(self):
        for ctx,fixed in ((result(19,struct.pack('<BI',1,900)),dict(locked=True,remaining_ms=900)),
                          (result(4,struct.pack('<I',900),reason=8),dict(remaining_ms=900))):
            self.expect(ctx,fixed)
            self.expect(self.append(ctx,field(1,b'owner')),fixed,[(1,b'owner')])
            self.expect(self.append(ctx,field(127,b'extension')),fixed)
    def run(self):
        rows=[]
        for name in self.cases:
            at=len(self.observations);row=dict(id='CORE-FIXED-FIELDS-'+name,status='passed',clause='core §2.3/4.3/6.4/7')
            try:getattr(self,name.lower().replace('-','_'))()
            except Exception as e:row.update(status='failed',error=f'{type(e).__name__}: {e}')
            row['observations']=self.observations[at:];rows.append(row)
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',spec=self.identity,full_conformance=False,
                    checks=rows,inventory=reviewed_shapes(),probe_wire_trace=self.probe_traces,
                    unchecked=['interface/event/data/config item fixed shapes and optional fields','TLV nested shapes beyond prior core reader review','all internal omission ambiguity with arbitrary tails','production implementations and physical transport'])
