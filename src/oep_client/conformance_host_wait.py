"""Independent lower-bound arithmetic: save waits max_op_ms, fn 0 confirm waits the quick bound (core §4.4)."""
import hashlib
from pathlib import Path
from .conformance import require,spec_identity
from . import registry as _reg
SAVE=_reg.CORE.op['save']  # fn 0's save (core §13.7)


REVIEWED={'docs/oep-core.ja.md':'d63f99f1809146ad41f1643526c81417b366d7a9d85af3845daee6654a229a14',
          'docs/oep-transports.ja.md':'2117de20712f7dfad6c81e247cf1b0b206ccf7528fcdf70702cbfce1c17dbadc'}
QUICK_MS=1000  # core_quick_op_max_ms


def reviewed_identity(path):
    registry,identity=spec_identity(path)
    require(registry['timing']['host_wait_add_ms']==1000 and registry['timing']['notify_pending_max_frames']==2 and registry['constants']['min_max_frame']==64 and registry['limits']['max_op_ms_max']==600000 and registry['timing'].get('core_quick_op_max_ms')==QUICK_MS,'reviewed Host wait numbers changed')
    require(next(row['code'] for row in registry['core']['op'] if row['name']=='save')==SAVE,'reviewed save operation changed')
    for name,h in REVIEWED.items():require(hashlib.sha256((Path(path)/name).read_bytes()).hexdigest()==h,'reviewed Host wait document changed: '+name)
    return identity


class HostWaitChecks:
    def __init__(self,a,*,spec):self.a=a;self.identity=reviewed_identity(spec);self.results=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='HOST-WAIT-'+name,level='core',clause='core §4.4/7.5/13.7; transports §6')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.results.append(row)
    def context(self):
        for name in ('save','limits','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit Host wait controls required')
    def budget(self,record,argument,kind,frame):
        require(record['expect_ms']==argument and record['frame']==frame,'Host processing bound/latest frame differs')
        transfer=(record['tx_len']+frame*3)*10/record['baud'] if kind=='cobs' else 0
        minimum=argument/1000+1+transfer
        require(record['transfer_s']+1e-12>=transfer and record['floor_s']+1e-12>=minimum and record['wait_s']+1e-12>=minimum,'Host wait is below processing bound+margin+transfer floor')
        return minimum
    def save(self,failure=False):
        cases=[dict(kind=kind,baud=baud,maximum=maximum,frame=frame,outer_ms=outer,timeout=0.05,failure=failure)
               for maximum in (1,5000,600000) for frame in (64,256,65535) for outer in (0,7000)
               for kind,baud in (('length',None),('cobs',1200),('cobs',115200),('cobs',3000000))]
        if failure:cases=cases[::12]
        for case in cases:
            result=self.a.save(case);record=result['record'];expected=max(case['maximum'],case['outer_ms'])
            require(result['save_code']==SAVE,'save is fn 0 save (core §13.7)')
            minimum=self.budget(record,expected,case['kind'],case['frame']);raw=bytes.fromhex(record['request_hex'])
            require(len(raw)==10 and raw[:1]==b'\1' and raw[3:6]==b'\0\0'+bytes([SAVE]) and int.from_bytes(raw[6:10],'little')==7,'Host save wire request differs')
            require(record['tx_len']==(15 if case['kind']=='cobs' else 12),'save wire length differs')
            require(result['restored_outer_ms']==case['outer_ms'] and result['final_expect_ms']==0,'Host expecting scope leaked or discarded outer wait')
            require((result['error']=='Failed' and result['value'] is None) if failure else (result['error'] is None and result['value']==0x12345678),'Host save outcome differs')
            self.observations.append(dict(action='failure' if failure else 'save',case=case,result=result,minimum_s=minimum))
    def limits(self):
        for kind,baud in (('length',None),('cobs',115200),('cobs',3000000)):
            rows=self.a.limits(kind,baud);require(len(rows)==3,'Host latest-confirm sequence differs')
            for record,frame in zip(rows,(64,256,64)):
                # fn 0 confirm: min(max_op_ms, core_quick_op_max_ms), core_quick_op_max_ms while max_op_ms is unknown.
                maximum=record.get('max_op_ms');quick=QUICK_MS if maximum is None else min(maximum,QUICK_MS)
                minimum=self.budget(record,quick,kind,frame);raw=bytes.fromhex(record['request_hex'])
                require(raw[3:6]==b'\0\0\1' and raw[6:10]==b'\0'*4,'wait limit sequence not confirm')
                self.observations.append(dict(action='limits',kind=kind,record=record,minimum_s=minimum))
    def run(self):
        self.check('CONTEXT',self.context)
        if self.results[-1]['status']=='passed':
            self.check('SAVE',lambda:self.save());self.check('FAILURE',lambda:self.save(True));self.check('LIMITS',self.limits)
        else:
            for name in ('SAVE','FAILURE','LIMITS'):self.results.append(dict(id='HOST-WAIT-'+name,status='blocked'))
        return dict(status='passed' if all(r['status']=='passed' for r in self.results) else 'failed',checks=self.results,full_conformance=False,
                    spec=self.identity,observations=self.observations,implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    production_opcode_alignment_proven=True,production_quick_bound_proven=True,
                    unchecked=['elapsed wall-clock deadline/timeout and pipeline predecessor timing','physical transport/flash','own-link negotiation waits',
                               'other operation processing bounds and full production Host integration'])
