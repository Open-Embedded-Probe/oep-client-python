"""Selected-SPEC first-segment geometry of an immediate group start from explicit raw observations.

No imports of probe encoders or Host decoders. Timing tolerances belong to the
caller's controlled fixture, not a guarantee inferred from uncertainty metadata.
"""
import struct
from fractions import Fraction
from .conformance import require, tlvs

NO_TIME=(1<<64)-1
NONE=(1<<32)-1


class CaptureGroupGeometryChecks:
    def __init__(self,checks):self.c=checks
    @staticmethod
    def first(payload):
        values={}
        for tag,value in tlvs(payload):values.setdefault(tag,value)
        return values
    def configs(self,payloads):
        require(type(payloads)is dict and payloads,'explicit configure observations required')
        specs={s['name']:s for s in self.c.reg['interface']};result={}
        for fn,(name,payload) in payloads.items():
            require(type(fn)is int and 1<=fn<=65535 and name in ('oep.fixture.logic','oep.fixture.analog'),'explicit capture fn/name')
            tags=specs[name]['tlv']['configure_answer'];values=self.first(payload)
            rate=values.get(tags['actual_rate'],b'');size=values.get(tags['actual_samples'],b'')
            require(len(rate)==8 and len(size)==4,'actual rate/samples fixed shape')
            num,den=struct.unpack('<II',rate);samples=struct.unpack('<I',size)[0]
            require(num and den and samples,'positive actual rate and samples')
            result[fn]=dict(name=name,num=num,den=den,rate=Fraction(num,den),samples=samples)
        return result
    def start(self,payload,members):
        require(len(payload)>=13,'group start header');ns,generation,count=struct.unpack_from('<QIB',payload)
        require(generation and ns!=NO_TIME and count==len(members),'group start time/count/generation')
        require(len(payload)>=13+6*count,'group start track array')
        rows=[struct.unpack_from('<HI',payload,13+6*i) for i in range(count)];tlvs(payload[13+6*count:])
        require([fn for fn,gen in rows]==list(members) and all(gen for fn,gen in rows),'group bind order and nonzero track generations')
        return dict(ns=ns,generation=generation,tracks=dict(rows))
    def status(self,payload,start):
        require(len(payload)>=13,'group status header');state,ns,generation=struct.unpack_from('<BQI',payload);tlvs(payload[13:])
        require(state==4 and ns==start['ns'] and generation==start['generation'],'completed one-shot group status/start agreement')
    def segments(self,records,configs,start,origins,tolerance):
        require(set(records)==set(configs)==set(origins)==set(start['tracks']),'complete first-segment observations and origins')
        observed={}
        for fn,cfg in configs.items():
            raw=records[fn];require(len(raw)==37,'first segment fixed record shape')
            serial,position,samples,ns,uncertainty,index,flags,generation=struct.unpack('<IQIQIIBI',raw)
            require(serial==0 and position==0 and generation==start['tracks'][fn],'first segment serial/position/generation')
            require(samples==cfg['samples'] and flags&~4==0,'complete immediate segment: actual samples, no gap/short/reserved flags')
            require(index in (0,NONE),'immediate segment trigger_index')
            require(type(origins[fn])is int and start['ns']<=origins[fn]<NO_TIME,'explicit first sample origin not before group start')
            require(abs(ns-origins[fn])<=tolerance and ns+tolerance>=start['ns'],'first sample time against known origin and group start')
            observed[str(fn)]=dict(samples=samples,start_ns=ns,offset_ns=ns-start['ns'],trigger_index=index,slipped=bool(flags&4),reported_uncertainty_ns=uncertainty)
        self.c.observed['group_geometry']=observed
    def run(self,*,configure_payloads,start_payload,status_payload,first_segments,members,sample_origins,timestamp_tolerance_ns=1):
        start=len(self.c.results);self.decoded=None;self.started=None
        def context():
            require(type(members) in (list,tuple) and members and len(set(members))==len(members) and all(type(fn)is int and 1<=fn<=65535 for fn in members),'unique explicit bind members')
            require(type(sample_origins)is dict and type(timestamp_tolerance_ns)is int and 0<=timestamp_tolerance_ns<=0xffffffff,'explicit sample origins and fixture timestamp tolerance')
            self.decoded=self.configs(configure_payloads);require(set(self.decoded)==set(members),'configure observations match bind')
            self.started=self.start(start_payload,members)
        self.c.check('OEP-GROUP-GEOMETRY-CONTEXT','capture §3.3/4.1',context)
        def status():
            require(self.started is not None,'valid geometry context required');self.status(status_payload,self.started)
        self.c.check('OEP-GROUP-GEOMETRY-STATUS','capture §4.1',status)
        def records():
            require(self.started is not None and self.decoded is not None,'valid geometry context required')
            self.segments(first_segments,self.decoded,self.started,sample_origins,timestamp_tolerance_ns)
        self.c.check('OEP-GROUP-GEOMETRY-SEGMENTS','capture §2/3.3/4.1',records)
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,
                    scope='one-shot first segments of an immediate group start with independently known sample origins',
                    inputs_hex=dict(configure={str(fn):dict(name=name,payload=body.hex()) for fn,(name,body) in configure_payloads.items()},start=start_payload.hex(),status=status_payload.hex(),segments={str(fn):body.hex() for fn,body in first_segments.items()}),
                    fixture=dict(members=list(members),sample_origins=sample_origins,timestamp_tolerance_ns=timestamp_tolerance_ns),
                    unchecked=['wire execution/rejection, bind feasibility and generation progression','config layout/raw data/notifications','repeat/streaming and stopped short segments','physical timing accuracy; reported uncertainty is not a guarantee','all group resources/topologies and production Host'])
