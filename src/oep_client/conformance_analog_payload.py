"""Selected-SPEC analog success payload and raw sample checks, independent of Host.

Callers supply explicit completed-success bodies and known source samples. This
checks payload contracts, not execution, session ownership or physical accuracy.
"""
import re
import struct
from fractions import Fraction
from .conformance import require, tlvs


class AnalogPayloadChecks:
    def __init__(self,checks):
        self.c=checks
        self.spec=next(s for s in checks.reg['interface'] if s['name']=='oep.fixture.analog')
        self.tags=self.spec['tlv']['configure_answer']
    def configure(self,payload,*,roles,frontends,mode):
        require(mode in (1,2,3) and roles and len(set(roles))==len(roles),'explicit mode and unique planned roles')
        require(all(type(role)is int and 0<=role<=63 for role in roles),'analog planned role numbers 0..63')
        rows=tlvs(payload);values={}
        repeated={self.tags[name] for name in ('scale','skew','frontend_used')}
        for tag,value in rows:
            if tag not in repeated:values.setdefault(tag,value)
        def fixed(name,width):
            value=values.get(self.tags[name]);require(value is not None and len(value)==width,name+' required fixed shape');return value
        numerator,denominator=struct.unpack('<II',fixed('actual_rate',8));require(numerator and denominator,'actual_rate positive numerator/denominator')
        layout=values.get(self.tags['layout'],b'');require(len(layout)>=4,'analog layout header')
        slot,offset,bits,count=layout[:4];order=list(layout[4:])
        require(slot in (8,16,32) and 1<=bits<=31 and offset+bits<=slot,'analog slot/value bit bounds')
        require(count==len(roles) and len(order)==count and sorted(order)==sorted(roles),'analog layout channel permutation/count')
        config=dict(slot=slot,offset=offset,bits=bits,order=order,roles=list(sorted(roles)),rate=Fraction(numerator,denominator))
        for name,needed in [('actual_samples',mode in (1,2)),('actual_segments',mode==2)]:
            if needed:value=struct.unpack('<I',fixed(name,4))[0];require(value>0,name+' positive');config[name]=value
        for name,width,format_ in [('scale',9,'<Bii'),('skew',5,'<BI'),('frontend_used',2,'<BB')]:
            found={}
            for tag,value in rows:
                if tag!=self.tags[name]:continue
                require(len(value)==width,name+' row fixed shape');row=struct.unpack(format_,value);role=row[0]
                require(role in roles and role not in found,name+' unknown/duplicate planned role');found[role]=row[1:]
            if name=='scale':require(set(found)==set(roles),'scale one per planned role')
            if name=='frontend_used':require(all(row[0] in frontends for row in found.values()),'frontend_used not declared')
            config[name]=found
        config['reference']=None
        if self.tags['reference'] in values:
            source,mv,how=struct.unpack('<BIB',fixed('reference',6))
            require(source in self.spec['enum']['reference_source'].values() and how in self.spec['enum']['reference_how'].values(),'reference enum')
            config['reference']=(source,mv,how)
        return config
    def calibration(self,payload,*,frontends):
        tags=self.spec['tlv']['calibration_answer'];seen=set();vref_seen=False;result=[]
        for tag,value in tlvs(payload):
            if tag==tags['factory']:
                require(len(value)>=4,'factory header');fe,size=value[:2]
                require(fe==255 or fe in frontends,'factory frontend not declared')
                require(fe not in seen,'one factory per frontend');seen.add(fe)
                require(1<=size<=64 and len(value)>=4+size,'factory scheme length')
                scheme=value[2:2+size];require(re.fullmatch(rb'[a-z0-9.\-]+',scheme) is not None,'factory scheme namespace alphabet')
                raw_size=struct.unpack_from('<H',value,2+size)[0];require(len(value)==4+size+raw_size,'factory raw length')
                result.append((fe,scheme,value[4+size:]))
            elif tag==tags['vrefint'] and not vref_seen:
                require(len(value)==16,'vrefint fixed shape');struct.unpack('<IQI',value);vref_seen=True
        return result
    @staticmethod
    def data(payload,config,expected_samples):
        count=len(config['roles']);width=config['slot']//8;mask=(1<<config['bits'])-1
        require(type(expected_samples)is list and all(type(row)is list and len(row)==count and all(type(value)is int and 0<=value<=mask for value in row) for row in expected_samples),'explicit raw samples in increasing plan role order')
        require(len(payload)==len(expected_samples)*count*width,'analog raw data byte length')
        for i,expected in enumerate(expected_samples):
            for frame,channel in enumerate(config['order']):
                at=(i*count+frame)*width;actual=(int.from_bytes(payload[at:at+width],'little')>>config['offset'])&mask
                require(actual==expected[config['roles'].index(channel)],'analog raw sample/order/little endian/value bits')
    def run(self,*,configure_payload,calibration_payload,data,expected_samples,roles,frontends,mode):
        start=len(self.c.results);self.config=None
        def configure():self.config=self.configure(configure_payload,roles=roles,frontends=frontends,mode=mode)
        self.c.check('OEP-ANALOG-PAYLOAD-CONFIGURE','capture §1.2/3.2/3.3; core §2.3',configure)
        self.c.check('OEP-ANALOG-PAYLOAD-CALIBRATION','capture §3.7; core §2.3',lambda:self.calibration(calibration_payload,frontends=frontends))
        def samples():
            require(self.config is not None,'valid configure required to interpret samples');self.data(data,self.config,expected_samples)
        self.c.check('OEP-ANALOG-PAYLOAD-DATA','capture §1.2',samples)
        rows=self.c.results[start:]
        return dict(status='passed' if all(row['status']=='passed' for row in rows) else 'failed',checks=rows,full_conformance=False,
                    scope='explicit analog success bodies and known raw samples',
                    payloads_hex=dict(configure=configure_payload.hex(),calibration=calibration_payload.hex(),data=data.hex()),
                    unchecked=['wire execution, rejection/session and replay','capture generations, trigger, error, notification and resource lifetime','factory scheme raw interpretation and physical reference/skew accuracy','complete analog operation and capture-group contracts'])
