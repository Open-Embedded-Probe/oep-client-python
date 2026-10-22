"""Read-only selected capture describe contracts, not op behavior."""
import struct
from .conformance import require
from .conformance_registered_ops import RegisteredOpsChecks


class CaptureMetadataChecks:
    supported={'oep.fixture.logic','oep.fixture.analog','oep.fixture.capture-group'}
    def __init__(self,checks,selection):self.c,self.selection=checks,selection
    def metadata(self,fn,name):
        rows=self.base.rows[fn];values=dict(reversed(rows));spec=self.base.specs[name];tags=spec['tlv']['describe'];common=self.c.reg['describe_common']
        def fixed(tag,width,label):
            require(tag in values and len(values[tag])==width,label+' required fixed width');return values[tag]
        def repeated(tag,width,label):
            found=[v for t,v in rows if t==tag];require(found and all(len(v)==width for v in found),label+' required repeated fixed shape');return found
        if True:
            # Revision 1 defines no feature bits; features may be left out when 0 (capture §3.5/4.3).
            features=values.get(common['features'],b'\0'*4);require(len(features)==4,'features is u32 when present');result=dict(features=int.from_bytes(features,'little'))
            if name=='oep.fixture.capture-group':
                require(tags['tracks'] in values,'tracks declaration required');v=values[tags['tracks']];require(v and v[0]>=1 and len(v)==1+2*v[0],'tracks: n>=1 and n u16 fn');tracks=[struct.unpack_from('<H',v,1+2*i)[0] for i in range(v[0])];result['tracks']=tracks
            else:
                require(common['role_channels'] in values or common['channel_group'] in values,'plan role_channels/channel_group declaration required')
                modes=[struct.unpack('<BII',v) for v in repeated(tags['mode'],9,'mode')];require(len({m[0] for m in modes})==len(modes),'one describe row per mode')
                require(all(m[2]==0 for m in modes if m[0] in (1,3)),'max_segments is 0 for mode 1/3');result['modes']=[list(m) for m in modes]
                minimum,maximum=struct.unpack('<II',fixed(tags['rate_range'],8,'rate_range'));require(1<=minimum<=maximum,'rate_range: 1 <= min_hz <= max_hz');result['rate_range']=[minimum,maximum]
                result['channels']=fixed(tags['channels'],1,'channels')[0];types,pretrigger=struct.unpack('<II',fixed(tags['trigger'],8,'trigger'))
                require(types&1,'trigger types must include immediate (type 0)');result['trigger']=[types,pretrigger]
                if name=='oep.fixture.analog':
                    frontend=[struct.unpack('<Bii',v) for v in repeated(tags['frontend'],9,'frontend')];require(len({v[0] for v in frontend})==len(frontend),'one frontend declaration per number');result['frontend']=[list(v) for v in frontend]
        self.c.observed.setdefault('capture_metadata',{})[str(fn)]=dict(name=name,**result)
    def run(self):
        start=len(self.c.results)
        def context():require(type(self.selection)is dict and self.selection and all(type(n)is str and n in self.supported for n in self.selection.values()),'explicit supported capture selection required')
        self.c.check('OEP-CAPTURE-METADATA-CONTEXT','capture §3.5/4.3',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        self.base=RegisteredOpsChecks(self.c,self.selection);self.base.run()
        for fn,name in (self.selection.items() if type(self.selection)is dict else []):self.c.check('OEP-CAPTURE-METADATA-'+str(fn),'capture §3.5/4.3',lambda fn=fn,name=name:self.metadata(fn,name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,scope='selected capture describe shapes, mode rows, rate range, immediate trigger and plan declarations; read-only',unchecked=['capture operation behavior, notifications and lifetime','capture capacity/rate/frontends operational semantics and track binding','role_channels/channel_group contents against plan roles','metadata of other standard interfaces and physical behavior'])
