"""Selected-SPEC first-segment group geometry from explicit raw observations.

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
    def start(self,payload,members,maximum):
        require(len(payload)>=17,'group start header');blocking,ns,generation,count=struct.unpack_from('<IQIB',payload)
        require(blocking<=maximum and generation and ns!=NO_TIME and count==len(members),'group start bounds/count/generation')
        require(len(payload)>=17+6*count,'group start track array')
        rows=[struct.unpack_from('<HI',payload,17+6*i) for i in range(count)];tlvs(payload[17+6*count:])
        require([fn for fn,gen in rows]==list(members) and all(gen for fn,gen in rows),'group bind order and nonzero track generations')
        return dict(ns=ns,generation=generation,tracks=dict(rows))
    def status(self,payload,start,source,forced,trigger_ns):
        require(len(payload)>=23,'group status header');state,ns,trigger,fn,generation=struct.unpack_from('<BQQHI',payload);tlvs(payload[23:])
        require(state==4 and ns==start['ns'] and generation==start['generation'],'completed one-shot group status/start agreement')
        require(trigger_ns>=ns and trigger==trigger_ns and fn==(0 if forced else source),'common trigger instant/source agreement')
    @staticmethod
    def nearest(origin,rate,trigger_ns):
        coordinate=Fraction(trigger_ns-origin)*rate/1000000000
        lower=coordinate.numerator//coordinate.denominator
        candidates={max(0,lower),max(0,lower+1)}
        distance={i:abs(Fraction(origin)+Fraction(i*1000000000)/rate-trigger_ns) for i in candidates}
        best=min(distance.values())
        return {i for i in candidates if distance[i]==best}
    def segments(self,records,configs,start,source,pre,origins,trigger_ns,tolerance):
        require(set(records)==set(configs)==set(origins)==set(start['tracks']),'complete first-segment observations and origins')
        src=configs[source];observed={}
        for fn,cfg in configs.items():
            p=-(-(pre*cfg['num']*src['den'])//(cfg['den']*src['num']))
            require(p<cfg['samples'],'valid group pretrigger below actual sample capacity')
            raw=records[fn];require(len(raw)==37,'first segment fixed record shape')
            serial,position,samples,ns,uncertainty,index,flags,generation=struct.unpack('<IQIQIIBI',raw)
            require(serial==0 and position==0 and generation==start['tracks'][fn],'first segment serial/position/generation')
            require(0<samples<=cfg['samples'] and index!=NONE and index<samples and flags&~7==0,'segment sample/index/flags bounds')
            require(type(origins[fn])is int and start['ns']<=origins[fn]<NO_TIME,'explicit first sample grid origin')
            choices=self.nearest(origins[fn],cfg['rate'],trigger_ns);valid=[]
            for closest in choices:
                available=min(p,closest);begin=closest-available
                expected_ns=Fraction(origins[fn])+Fraction(begin*1000000000)/cfg['rate']
                expected_samples=cfg['samples']-p+available
                if index==available and samples==expected_samples and abs(ns-expected_ns)<=tolerance and bool(flags&2)==(expected_samples<cfg['samples']):valid.append(closest)
            require(valid,'group first-segment ceil pretrigger/available history/nearest index/start/short geometry')
            observed[str(fn)]=dict(pretrigger=p,nearest_candidates=sorted(choices),accepted_indices=valid,trigger_index=index,samples=samples,start_ns=ns,reported_uncertainty_ns=uncertainty)
        self.c.observed['group_geometry']=observed
    def run(self,*,configure_payloads,start_payload,status_payload,first_segments,members,source_fn,pretrigger,sample_origins,expected_trigger_ns,forced=False,max_op_ms=1000,timestamp_tolerance_ns=1,continuous_history=False):
        start=len(self.c.results);self.decoded=None;self.started=None
        def context():
            require(type(members) in (list,tuple) and members and len(set(members))==len(members) and all(type(fn)is int and 1<=fn<=65535 for fn in members),'unique explicit bind members')
            require(type(source_fn)is int and source_fn in members and type(pretrigger)is int and 0<=pretrigger<=NONE,'explicit source/pretrigger')
            require(type(expected_trigger_ns)is int and 0<=expected_trigger_ns<NO_TIME and type(forced)is bool,'explicit observed trigger instant/force')
            require(type(timestamp_tolerance_ns)is int and 0<=timestamp_tolerance_ns<=0xffffffff and type(max_op_ms)is int and max_op_ms>=0,'explicit fixture timestamp tolerance/blocking budget')
            require(continuous_history is True,'independently known continuous history required')
            self.decoded=self.configs(configure_payloads);require(set(self.decoded)==set(members),'configure observations match bind')
            self.started=self.start(start_payload,members,max_op_ms)
        self.c.check('OEP-GROUP-GEOMETRY-CONTEXT','capture §3.3/4.1',context)
        def status():
            require(self.started is not None,'valid geometry context required');self.status(status_payload,self.started,source_fn,forced,expected_trigger_ns)
        self.c.check('OEP-GROUP-GEOMETRY-STATUS','capture §4.1',status)
        def records():
            require(self.started is not None and self.decoded is not None,'valid geometry context required')
            self.segments(first_segments,self.decoded,self.started,source_fn,pretrigger,sample_origins,expected_trigger_ns,timestamp_tolerance_ns)
        self.c.check('OEP-GROUP-GEOMETRY-SEGMENTS','capture §2/3.3/4.1',records)
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,
                    scope='one-shot first segments with independently known sample origins, continuous history and trigger time',
                    inputs_hex=dict(configure={str(fn):dict(name=name,payload=body.hex()) for fn,(name,body) in configure_payloads.items()},start=start_payload.hex(),status=status_payload.hex(),segments={str(fn):body.hex() for fn,body in first_segments.items()}),
                    fixture=dict(members=list(members),source_fn=source_fn,pretrigger=pretrigger,sample_origins=sample_origins,expected_trigger_ns=expected_trigger_ns,forced=forced,timestamp_tolerance_ns=timestamp_tolerance_ns,continuous_history=continuous_history),
                    unchecked=['wire execution/rejection, bind feasibility and max_pretrigger budgets','config layout/raw data/notifications and all generation progression','repeat/streaming and interrupted history','physical timing accuracy; reported uncertainty is not a guarantee','all group resources/topologies and production Host'])
