"""Read-only describe constraints for explicitly selected fixture interfaces."""
import struct
from .conformance import require, ops
from .conformance_registered_ops import RegisteredOpsChecks


class FixtureMetadataChecks:
    supported={'oep.fixture.gpio','oep.fixture.uart','oep.fixture.i2c-target','oep.fixture.spi-target'}
    def __init__(self,checks,selection):self.c,self.selection=checks,selection
    def metadata(self,fn,name):
        spec=self.base.specs[name];values=dict(reversed(self.base.rows[fn]));tags=spec['tlv']['describe'];common=self.c.reg['describe_common']
        def fixed(tag,width,label):
            require(tag in values and len(values[tag])==width,label+' required fixed width')
            return int.from_bytes(values[tag],'little')
        if name=='oep.fixture.gpio':
            modes=fixed(tags['modes'],4,'modes');require(modes&1,'GPIO input mode is mandatory');result=dict(modes=modes)
        elif name=='oep.fixture.uart':
            tag=tags['formats'];require(tag in values and values[tag] and len(values[tag])==1+values[tag][0],'formats required count/shape')
            formats=list(values[tag][1:]);require(0 in formats,'8N1 mandatory')
            require(all(v&0xe0==0 and v&3 in (0,1) and (v>>2)&3 in (0,1,2) for v in formats),'formats contain undefined values or reserved bits')
            result=dict(formats=formats)
            # a fn without write takes no TX role and declares none in role_channels / channel_group (fixture §2)
            codes={r['name']:r['code'] for r in spec['op']};tx=spec.get('enum',{}).get('role',{}).get('tx',2)
            if codes['write'] not in ops(values[common['ops']]):
                for t,v in self.base.rows[fn]:
                    if t==common['role_channels']:require(not v or v[0]!=tx,'UART without write declares the TX role in role_channels')
                    elif t==common['channel_group']:require(all(v[i]!=tx for i in range(2,len(v),3)),'UART without write declares the TX role in a channel_group')
        else:
            length=fixed(common['max_length'],2,'max_length');clock=fixed(common['max_clock_hz'],4,'max_clock_hz');features=int.from_bytes(values.get(common['features'],b'\0'*4),'little');require(len(values.get(common['features'],b'\0'*4))==4,'features fixed width')   # optional: absent = no bits (core §7.4; fixture §3/§4)
            queue=fixed(tags['queue_depth'],1,'queue_depth')
            request,response=(12,8) if name=='oep.fixture.i2c-target' else (14,12)
            bound=max(request,response)+length
            require(bound<=self.c.max_frame,'fixture max_length does not fit negotiated route')
            tag=self.c.reg['core']['tlv']['describe']['transport'];indices={bytes.fromhex(r['value_hex'])[0] for r in self.c.observed['core_describe'] if r['tag']==tag}
            require(bound<=64 or indices=={self.c.confirm_transport},'other route limits are unconfirmed; cannot prove global fixture max_length')
            result=dict(max_length=length,max_clock_hz=clock,features=features,queue_depth=queue,route_bound='protocol minimum 64' if bound<=64 else 'sole advertised route')
            if name=='oep.fixture.i2c-target':
                codes={r['name']:r['code'] for r in spec['op']};offered=ops(values[common['ops']])
                if codes['stretch'] in offered:
                    maximum=fixed(tags['max_stretch_us'],4,'max_stretch_us');require(maximum>=1,'stretch max must be positive');result['max_stretch_us']=maximum
            else:
                modes=fixed(tags['modes'],1,'modes');require(1<=modes<=15,'SPI modes is bit k = mode k, bits 4-7 0, at least one')   # required (fixture §4)
                result['modes']=modes
                if tags['cs_setup_ns'] in values:result['cs_setup_ns']=fixed(tags['cs_setup_ns'],4,'cs_setup_ns')
        self.c.observed.setdefault('fixture_metadata',{})[str(fn)]=dict(name=name,**result)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.c.reg['constants']['min_max_frame']==64,'selected protocol minimum frame differs')
            require(type(self.selection)is dict and self.selection and all(type(n)is str and n in self.supported for n in self.selection.values()),'explicit supported fixture metadata selection required')
        self.c.check('OEP-FIXTURE-METADATA-CONTEXT','fixture §1–4; explicit selection',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        self.base=RegisteredOpsChecks(self.c,self.selection);self.base.run()
        for fn,name in (self.selection.items() if type(self.selection)is dict else []):self.c.check('OEP-FIXTURE-METADATA-'+str(fn),'fixture §1–4; core §7.4',lambda fn=fn,name=name:self.metadata(fn,name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,scope='selected GPIO/UART/I2C/SPI describe constraints; read-only',unchecked=['pin assignment legality and electrical behavior','configure/set/read/write/plan lifetime and side effects','unconfirmed route limits beyond protocol minimum','metadata and behavior of other standard interfaces','physical transports and production Host integration'])
