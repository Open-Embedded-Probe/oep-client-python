"""Read-only standard-interface declarations, separate from generic conformance."""
import struct
from .conformance import require, ops


class OepDeclarationChecks:
    def __init__(self, checks, selection):self.c,self.selection=checks,selection

    def offered(self, fn):
        values=dict(reversed(self.rows[fn]));tag=self.c.reg['describe_common']['ops']
        require(tag in values,'ops declaration missing');return ops(values[tag])

    def mandatory(self, fn, name):
        spec=self.specs[name]
        required={row['code'] for row in spec['op'] if row['name']!='scan'}
        require(required<=self.offered(fn),'mandatory standard operation missing')
        self.c.observed.setdefault('oep_required_ops',{})[str(fn)]=sorted(required)

    def wire_capacity(self, fn, name):
        tag=self.specs[name]['tlv']['describe']['max_connections'];values=dict(reversed(self.rows[fn]))
        if tag in values:require(len(values[tag])==1,'max_connections must be u8')
        value=values.get(tag,b'\x01')[0]
        self.c.observed.setdefault('wire_max_connections',{})[str(fn)]=value

    def wire_pins(self, fn, name):
        tags=self.c.reg['describe_common'];roles={};groups=[]
        core=dict((row['tag'],bytes.fromhex(row['value_hex'])) for row in reversed(self.c.observed['core_describe']))
        count=core.get(self.c.reg['core']['tlv']['describe']['channels'],b'\0\0')
        require(len(count)==2,'channels must be u16');count=int.from_bytes(count,'little')
        for tag,value in self.rows[fn]:
            if tag==tags['role_channels']:
                require(len(value)>=3,'role_channels header');role,base=struct.unpack_from('<BH',value)
                pins={base+i for i in range(8*(len(value)-3)) if value[3+i//8]&(1<<(i%8))}
                require(all(pin<count for pin in pins),'role candidate out of range');roles.setdefault(role,set()).update(pins)
            elif tag==tags['channel_group']:
                require(len(value)>=2 and len(value)==2+3*value[1],'channel_group shape')
                members=[struct.unpack_from('<BH',value,i) for i in range(2,len(value),3)]
                require(all(pin<count for _,pin in members),'group pin out of range');groups.append(members)
        required={self.specs[name]['enum']['pin_role']['swdio']}
        if name!='oep.wire.swio':required.add(self.specs[name]['enum']['pin_role']['swclk'])
        valid_group=any(required<={role for role,_ in members} and len({pin for _,pin in members})==len(members) for members in groups)
        if len(required)==1:valid_roles=bool(roles.get(next(iter(required))))
        else:
            first,second=sorted(required);valid_roles=any(a!=b for a in roles.get(first,()) for b in roles.get(second,()))
        require(valid_group or valid_roles,'no explicitly declared usable wire pin combination')

    def swio_clock(self, fn, name):
        values=dict(reversed(self.rows[fn]));tag=self.c.reg['describe_common']['min_clock_hz']
        require(tag in values and len(values[tag])==4 and struct.unpack('<I',values[tag])[0]>0,'SWIO requires its fixed bit timing as min_clock_hz')

    def mechanisms(self, fn, name):
        spec=self.specs[name];tag=spec['tlv']['describe']['mechanisms'];values=dict(reversed(self.rows[fn]))
        require(tag in values,'console mechanisms declaration missing')
        known={value for key,value in spec['enum']['mechanism'].items() if key!='none'}
        require(all(value in known for value in values[tag]),'unknown or preset-only console mechanism declared')
        self.c.observed.setdefault('console_mechanisms',{})[str(fn)]=list(values[tag])

    def run(self):
        start=len(self.c.results)
        def context():
            require(type(self.selection)is dict and self.selection and all(type(fn)is int and 1<=fn<=65535 for fn in self.selection),'explicit fn/name selection required')
            names={'oep.wire.rvswd','oep.wire.swio','oep.wire.swd','oep.target.console'}
            require(all(name in names for name in self.selection.values()),'unsupported standard declaration family')
            self.specs={row['name']:row for row in self.c.reg['interface']}
            require(all(name in self.specs for name in self.selection.values()),'selected standard absent from SPEC')
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot();self.rows={}
            for fn,name in self.selection.items():
                require(any(row['fn']==fn and row['name']==name and row['revision']==1 for row in entries),'selected fn/name/revision mismatch')
                require(self.specs[name]['revision']==1,'selected SPEC revision unsupported by checker')
                self.rows[fn]=self.c.describe(fn)
            self.c.observed['oep_declaration_selection']={str(fn):name for fn,name in self.selection.items()}
        self.c.check('OEP-DECLARATION-CONTEXT','debug §0; console §1; explicit selection',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for fn,name in (self.selection.items() if type(self.selection)is dict else []):
            cases=[('OPS',self.mandatory,'debug §0 / console §1')]
            cases += [('CAPACITY',self.wire_capacity,'debug §1'),('PINS',self.wire_pins,'debug §1; core §7.4')] if name.startswith('oep.wire.') else [('MECHANISMS',self.mechanisms,'console §1/3')]
            if name=='oep.wire.swio':cases.append(('CLOCK',self.swio_clock,'debug §3.2'))
            for suffix,method,clause in cases:
                self.c.check('OEP-DECLARATION-'+str(fn)+'-'+suffix,clause,lambda fn=fn,name=name,method=method:method(fn,name))
        rows=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in rows) else 'failed','full_conformance':False,
                'scope':'explicit standard wire/console mandatory ops and describe declarations; read-only',
                'levels':{'core':'discovery prerequisites','interface':'run generic checks separately','oep-interface':'selected standard declarations'},
                'checks':rows,'observed':self.c.observed,
                'unchecked':['offered operation behavior','attach critical TLVs and electrical sequence','scan behavior','console mechanism framing/delivery',
                             'resource lifetime/common stream/status contracts','physical pins/targets','production Host integration']}
