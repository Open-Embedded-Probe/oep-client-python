"""Selected-SPEC cross-rate one-shot acquisition checks with independent waveform oracle."""
import struct
from fractions import Fraction
from .conformance import require,tlvs
from .conformance_capture_group import CaptureGroupChecks,NO_TIME
from .conformance_registered_ops import RegisteredOpsChecks
from .conformance_analog_payload import AnalogPayloadChecks
from .conformance_capture_group_geometry import CaptureGroupGeometryChecks


class CaptureTimelineChecks(CaptureGroupChecks):
    scenarios=tuple((source,rates,pre,event) for source in (2,3) for rates in ((1000,333),(333,1000),(1000,500)) for pre in (0,2) for event in ('rise1','rise3','rise7','force0','force2500000','force6500000'))
    def context(self):
        expected={1:'oep.probe.plan',2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'}
        require(self.a.reset_scope=='logical-model' and self.a.selection==expected,'explicit timeline selection')
        base=RegisteredOpsChecks(self.c,expected);require(base.run()['status']=='passed','registered timeline group/track ops')
        self.codes={fn:{r['name']:r['code'] for r in base.specs[name]['op']} for fn,name in expected.items()}
        self.tags={fn:base.specs[expected[fn]]['tlv']['configure'] for fn in (2,3)};self.answers={fn:base.specs[expected[fn]]['tlv']['configure_answer'] for fn in (2,3)}
        self.trigger_tag=base.specs[expected[4]]['tlv']['bind']['trigger_track'];self.assignment=base.specs[expected[1]]['tlv']['plan_apply']['role_assignment']
        self.maximum=int.from_bytes(dict(reversed(self.c.describe(0)))[self.c.reg['core']['tlv']['describe']['max_op_ms']],'little')
        self.payload=AnalogPayloadChecks(self.c);require(self.a.state()['clock_scope']=='explicit-logical-ns','explicit timeline ns clock')
    @staticmethod
    def values(fn,index,rise):
        high=rise is not None and index>=rise
        return [index*17%4096,3000 if high else 0] if fn==2 else [int(high),(index//2)%2]
    def scenario(self,source,rates,pre,event):
        self.fresh()
        with self.c.holding(1000) as sid:
            self.plan(sid);forced=event.startswith('force');rise=None if forced else int(event[4:]);other=5-source
            actual={};payloads={};self.config={}
            for fn,rate in zip((source,other),rates):
                body=self.settings(fn,rate=rate,samples=16,trigger=(3,3,1500) if fn==2 else (2,0,0),pre=pre) if fn==source else self.settings(fn,rate=rate,samples=16)
                p=self.c.success(self.req(fn,'configure',sid,body));name='oep.fixture.analog' if fn==2 else 'oep.fixture.logic';payloads[fn]=(name,p)
                tags=dict(tlvs(p));actual[fn]=Fraction(*struct.unpack('<II',tags[self.answers[fn]['actual_rate']]))
                require(actual[fn]==rate,'explicit exact timeline rate')
                if fn==2:self.config[2]=self.payload.configure(p,roles=(1,3),frontends={0,1},mode=1,max_op_ms=self.maximum)
                self.a.program(fn,rise if fn==source else None)
            self.bind(sid,(source,other),source);generation,gens,_,start=self.start(sid,(source,other));origin=self.status()[0][1]
            when=Fraction(int(event[5:])) if forced else Fraction(rise*1000000000)/actual[source]
            trigger_ns=origin+int(when)
            if forced:
                self.a.advance_to(trigger_ns);require(self.status()[0][0]==2,'waveform triggered before force');self.ok(self.req(4,'force',sid))
            else:
                ceiling=-(-when.numerator//when.denominator)
                self.a.advance_to(origin+ceiling-1);require(self.status()[0][0]==2 and self.status()[0][2]==NO_TIME,'timeline observed future source sample')
                self.a.advance_to(origin+ceiling)
            require(self.status()[0][2]==trigger_ns,'timeline common trigger time')
            self.a.advance_to(origin+100000000)
            status=self.c.success(self.req(4,'status'));require(status[0]==4,'asynchronous tracks did not complete')
            records={}
            for fn in (source,other):
                rows=self.segments(fn);require(len(rows)==1,'one-shot timeline segment count');records[fn]=struct.pack('<IQIQIIBI',*rows[0])
            geometry=CaptureGroupGeometryChecks(self.c).run(configure_payloads=payloads,start_payload=start,status_payload=status,first_segments=records,members=(source,other),source_fn=source,pretrigger=pre,sample_origins={fn:origin for fn in (source,other)},expected_trigger_ns=trigger_ns,forced=forced,max_op_ms=self.maximum,continuous_history=True)
            require(geometry['status']=='passed','acquired cross-rate geometry')
            for fn in (source,other):
                meta=geometry['observed']['group_geometry'][str(fn)];first=meta['accepted_indices'][0]-meta['trigger_index'];samples=[self.values(fn,i,rise if fn==source else None) for i in range(first,first+meta['samples'])]
                position,flags,data=self.read(fn,gens[fn]);require(position==0 and flags==0,'timeline raw read position/flags')
                if fn==2:self.payload.data(data,self.config[2],samples)
                else:
                    require(len(data)==(len(samples)*3+7)//8,'timeline logic packed byte count')
                    for i,row in enumerate(samples):
                        for bit,value in enumerate(row):require((data[(i*3+bit)//8]>>((i*3+bit)%8))&1==value,'timeline logic raw values')
            self.ok(self.c.request(self.c.core['end'],session=sid));self.c.session=None
            state=self.a.state();require(not state['timeline'] and not state['bound'] and not state['plans'] and state['pins']==state['idle'],'timeline session closure retained acquisition/cache')
    def run(self):
        start=len(self.c.results);self.c.check('OEP-TIMELINE-CONTEXT','capture §4; core §7',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for at,args in enumerate(self.scenarios):
            first=len(self.a.trace);self.c.check('OEP-TIMELINE-'+str(at),'capture §2/3.3/4.1; core §9',lambda args=args:self.scenario(*args));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,
                    scope='independent cross-rate one-shot ADC/logic acquisition, first geometry and known raw values',
                    scenarios=[dict(source=source,rates=rates,pretrigger=pre,event=event) for source,rates,pre,event in self.scenarios],
                    unchecked=['repeat/streaming/notifications and other group topologies','other rates/ADC layouts/skew policies, max pretrigger/retention/serial bounds','error/replay/resource races on asynchronous schedule','physical timing/skew/accuracy and production Host'])
