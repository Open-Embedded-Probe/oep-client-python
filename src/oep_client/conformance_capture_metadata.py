"""Read-only selected capture and config describe contracts, not op behavior."""
import struct
from .conformance import require
from .conformance_registered_ops import RegisteredOpsChecks


class CaptureMetadataChecks:
    supported={'oep.fixture.logic','oep.fixture.analog','oep.fixture.capture-group','oep.probe.config'}
    def __init__(self,checks,selection):self.c,self.selection=checks,selection
    def metadata(self,fn,name):
        rows=self.base.rows[fn];values=dict(reversed(rows));spec=self.base.specs[name];tags=spec['tlv']['describe'];common=self.c.reg['describe_common']
        def fixed(tag,width,label):
            require(tag in values and len(values[tag])==width,label+' required fixed width');return values[tag]
        def repeated(tag,width,label):
            found=[v for t,v in rows if t==tag];require(found and all(len(v)==width for v in found),label+' required repeated fixed shape');return found
        if name=='oep.probe.config':
            require(tags['items'] in values,'items declaration required');items=list(values[tags['items']])
            require(items and len(set(items))==len(items) and all(t&127 for t in items),'items: one or more item tags without duplicates or item 0');result=dict(items=items)
        else:
            features=int.from_bytes(fixed(common['features'],4,'features'),'little');require(features&7==0,'capture reserved features bit0–2 must be zero');result=dict(features=features)
            if name=='oep.fixture.capture-group':
                require(tags['tracks'] in values,'tracks declaration required');v=values[tags['tracks']];require(v and len(v)==1+2*v[0],'tracks count/shape');tracks=[struct.unpack_from('<H',v,1+2*i)[0] for i in range(v[0])];result['tracks']=tracks
            else:
                modes=[struct.unpack('<BII',v) for v in repeated(tags['mode'],9,'mode')];require(len({m[0] for m in modes})==len(modes),'one describe row per mode');result['modes']=[list(m) for m in modes]
                minimum,maximum,exact=struct.unpack('<IIB',fixed(tags['rate_range'],9,'rate_range'));require(exact in (0,1),'rate_range exact boolean');result['rate_range']=[minimum,maximum,exact]
                result['channels']=fixed(tags['channels'],1,'channels')[0];result['trigger']=list(struct.unpack('<II',fixed(tags['trigger'],8,'trigger')))
                if name=='oep.fixture.analog':
                    frontend=[struct.unpack('<BiiI',v) for v in repeated(tags['frontend'],13,'frontend')];require(len({v[0] for v in frontend})==len(frontend),'one frontend declaration per number');result['frontend']=[list(v) for v in frontend]
                elif tags['multirate'] in values:
                    policies,min_d,max_d,pow2=struct.unpack('<IIIB',fixed(tags['multirate'],13,'multirate'));require(policies&1 and 2<=min_d<=max_d and pow2 in (0,1),'broken multirate declaration; host must fall back');result['multirate']=[policies,min_d,max_d,pow2]
        self.c.observed.setdefault('capture_metadata',{})[str(fn)]=dict(name=name,**result)
    def run(self):
        start=len(self.c.results)
        def context():require(type(self.selection)is dict and self.selection and all(type(n)is str and n in self.supported for n in self.selection.values()),'explicit supported capture/config selection required')
        self.c.check('OEP-CAPTURE-METADATA-CONTEXT','capture §3.5/4.3/5.1; config §4',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        self.base=RegisteredOpsChecks(self.c,self.selection);self.base.run()
        for fn,name in (self.selection.items() if type(self.selection)is dict else []):self.c.check('OEP-CAPTURE-METADATA-'+str(fn),'capture §3.5/4.3/5.1; config §4',lambda fn=fn,name=name:self.metadata(fn,name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,scope='selected capture/config describe shapes, reserved features and multirate validity; read-only',unchecked=['capture/config operation behavior, notifications and lifetime','capture capacity/rate/frontends operational semantics and track binding','host fallback from broken multirate','config storage max_bytes and its presence with save/erase','metadata of other standard interfaces and physical behavior'])
