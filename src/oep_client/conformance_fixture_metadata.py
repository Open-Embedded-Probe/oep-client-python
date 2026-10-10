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
            tag=tags['drive_levels'];result['drive_levels_present']=tag in values
            if tag in values:
                v=values[tag];require(len(v)>=2,'drive_levels header');default,count=v[:2]
                require(count>=2 and len(v)==2+2*count and default<count,'drive_levels count/default/shape')
                currents=[struct.unpack_from('<H',v,2+2*i)[0] for i in range(count)]
                require(all(a<b for a,b in zip(currents,currents[1:])),'drive_levels must be strictly ascending')
                result.update(default=default,ma=currents)
        elif name=='oep.fixture.uart':
            tag=tags['formats'];require(tag in values and values[tag] and len(values[tag])==1+values[tag][0],'formats required count/shape')
            formats=list(values[tag][1:]);require(0 in formats,'8N1 mandatory')
            require(all(v&0xe0==0 and v&3 in (0,1) and (v>>2)&3 in (0,1,2) for v in formats),'formats contain undefined values or reserved bits')
            result=dict(formats=formats)
        else:
            length=fixed(common['max_length'],2,'max_length');clock=fixed(common['max_clock_hz'],4,'max_clock_hz');features=fixed(common['features'],4,'features');queue=fixed(tags['queue_depth'],1,'queue_depth')
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
            elif tags['cs_setup_ns'] in values:result['cs_setup_ns']=fixed(tags['cs_setup_ns'],4,'cs_setup_ns')
        self.c.observed.setdefault('fixture_metadata',{})[str(fn)]=dict(name=name,**result)
    def drive_consistency(self):
        declarations=[]
        for fn,name in self.selection.items():
            if name=='oep.fixture.gpio':
                tag=self.base.specs[name]['tlv']['describe']['drive_levels'];declarations.append(dict(reversed(self.base.rows[fn])).get(tag))
        require(not declarations or all(v==declarations[0] for v in declarations),'selected GPIO fns must declare identical drive_levels')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.c.reg['constants']['min_max_frame']==64,'selected protocol minimum frame differs')
            require(type(self.selection)is dict and self.selection and all(type(n)is str and n in self.supported for n in self.selection.values()),'explicit supported fixture metadata selection required')
        self.c.check('OEP-FIXTURE-METADATA-CONTEXT','fixture §1–4; explicit selection',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        self.base=RegisteredOpsChecks(self.c,self.selection);self.base.run()
        for fn,name in (self.selection.items() if type(self.selection)is dict else []):self.c.check('OEP-FIXTURE-METADATA-'+str(fn),'fixture §1–4; core §7.4',lambda fn=fn,name=name:self.metadata(fn,name))
        self.c.check('OEP-FIXTURE-DRIVE-CONSISTENCY','fixture §1.1; selected GPIO fns',self.drive_consistency)
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,scope='selected GPIO/UART/I2C/SPI describe constraints; read-only',unchecked=['unselected GPIO fns drive consistency','pin assignment legality and electrical behavior','configure/set/read/write/plan lifetime and side effects','unconfirmed route limits beyond protocol minimum','metadata and behavior of other standard interfaces','physical transports and production Host integration'])
