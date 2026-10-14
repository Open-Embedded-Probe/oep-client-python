"""Read-only mandatory/optional standard op declarations from selected registry.

Optionality comes from normative interface text, not op lock bits or numbers.
No operation behavior or mandatory describe metadata is certified here.
"""
from .conformance import require, ops


# An optional op that requires another: state with save, marks with mark.
IMPLIED={'oep.probe.config':(('save','state'),),
         'oep.target.console':(('mark','marks'),),'oep.fixture.uart':(('mark','marks'),)}
POLICIES={
    'oep.probe.plan':((),(),'plan §1'),
    'oep.probe.restart':((),(),'restart §1'),
    'oep.probe.link':(('port_speed','port_state'),(),'link §1'),
    **{n:(('scan',),(),'debug §1/3/5') for n in ('oep.wire.rvswd','oep.wire.swio','oep.wire.swd')},
    'oep.target.riscv-dm':(('halt','resume','reset','read_block','write_block','run','step'),(('read_block','write_block'),),'debug §4'),
    'oep.target.arm-adi':((),(),'debug §6'),
    'oep.target.console':(('marks','clear','mark','write'),(),'console §1'),
    'oep.fixture.gpio':((),(),'fixture §1'),
    'oep.fixture.uart':(('marks','clear','mark','write'),(),'fixture §2'),
    'oep.fixture.i2c-target':(('stretch',),(),'fixture §3'),
    'oep.fixture.spi-target':((),(),'fixture §4'),
    **{n:(('force','query','subscribe','unsubscribe'),(('subscribe','unsubscribe'),),'capture §3') for n in ('oep.fixture.logic','oep.fixture.analog')},
    'oep.fixture.capture-group':(('force','subscribe','unsubscribe'),(('subscribe','unsubscribe'),),'capture §4'),
    'oep.probe.config':(('save','erase','state'),(('save','erase'),),'probe-config §2, §4'),
}


class RegisteredOpsChecks:
    def __init__(self,checks,selection):self.c,self.selection=checks,selection
    def mandatory(self,fn,name):
        spec=self.specs[name];optional,pairs,_=POLICIES[name];codes={row['name']:row['code'] for row in spec['op']}
        values=dict(reversed(self.rows[fn]));tag=self.c.reg['describe_common']['ops']
        require(tag in values,'mandatory ops declaration missing');offered=ops(values[tag]);required={code for op,code in codes.items() if op not in optional}
        require(required<=offered,'mandatory standard op missing')
        for first,second in pairs:require((codes[first] in offered)==(codes[second] in offered),'paired optional standard ops must be offered together')
        for first,then in IMPLIED.get(name,()):require(codes[first] not in offered or codes[then] in offered,'an optional op requires its companion op')
        if name=='oep.fixture.logic':
            analog={row['name']:row['code'] for row in self.specs['oep.fixture.analog']['op']}
            require(analog['calibration'] not in offered,'logic must not advertise analog-only calibration')
        self.c.observed.setdefault('registered_op_contracts',{})[str(fn)]=dict(name=name,required=sorted(required),optional=sorted(codes[n] for n in optional),offered=sorted(offered))
    def run(self):
        start=len(self.c.results)
        def context():
            require(type(self.selection)is dict and self.selection and all(type(fn)is int and 1<=fn<=65535 and type(name)is str and name in POLICIES for fn,name in self.selection.items()),'explicit supported standard fn/name selection required')
            self.specs={s['name']:s for s in self.c.reg['interface']}
            require(all(name in self.specs and self.specs[name]['revision']==1 for name in self.selection.values()),'selected standard absent or unsupported revision')
            for name in self.selection.values():
                optional,pairs,_=POLICIES[name];names={row['name'] for row in self.specs[name]['op']}
                require(set(optional)<=names,'selected registry differs from optional-op policy')
            if 'oep.fixture.logic' in self.selection.values():require('oep.fixture.analog' in self.specs and any(row['name']=='calibration' for row in self.specs['oep.fixture.analog']['op']),'selected analog calibration reference missing')
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot();self.rows={}
            for fn,name in self.selection.items():
                require(any(row['fn']==fn and row['name']==name and row['revision']==1 for row in entries),'selected fn/name/revision mismatch')
                self.rows[fn]=self.c.describe(fn)
        self.c.check('OEP-REGISTERED-OPS-CONTEXT','core §1.2/7.4; explicit standard selection',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for fn,name in (self.selection.items() if type(self.selection)is dict else []):
            self.c.check('OEP-REGISTERED-OPS-'+str(fn),'core §1.2; '+POLICIES.get(name if type(name)is str else '',((),(),'selected policy'))[2],lambda fn=fn,name=name:self.mandatory(fn,name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(row['status']=='passed' for row in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,scope='explicit registered standard mandatory ops, optional pairs and analog-only calibration; read-only',unchecked=['mandatory interface-specific describe metadata','operation payloads/state/side effects','optional feature semantics and notifications','physical interfaces and production Host integration'])
