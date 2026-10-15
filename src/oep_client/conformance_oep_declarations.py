"""Read-only standard-interface declarations, separate from generic conformance."""
import struct
from .conformance import require, ops


class OepDeclarationChecks:
    def __init__(self, checks, selection):self.c,self.selection=checks,selection

    def offered(self, fn):
        values=dict(reversed(self.rows[fn]));tag=self.c.reg['describe_common']['ops']
        require(tag in values,'ops declaration missing');return ops(values[tag])

    def mandatory(self, fn, name):
        spec=self.specs[name];code={row['name']:row['code'] for row in spec['op']}
        # optional: a wire's scan (debug §1); the console's marks / clear / mark / write (console §1, the op table)
        optional={'scan'}|({'marks','clear','mark','write'} if name=='oep.target.console' else set())
        required={c for n,c in code.items() if n not in optional};offered=self.offered(fn)
        require(required<=offered,'mandatory standard operation missing')
        if name=='oep.target.console':   # mark is offered only with marks (console §1)
            require(code['mark'] not in offered or code['marks'] in offered,'console mark offered without marks')
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
        required=[self.specs[name]['enum']['pin_role']['swdio']]
        if name!='oep.wire.swio':required.append(self.specs[name]['enum']['pin_role']['swclk'])
        # core §7.4: a role a group lists is that group's channel (and in role_channels' candidates when they list the
        # role); a role the group does not list comes from role_channels alone; no groups: role_channels alone
        def options(group,role):
            if role in group:return [group[role]] if role not in roles or group[role] in roles[role] else []
            return sorted(roles.get(role,()))
        def usable(group):
            choices=[options(group,role) for role in required]
            return bool(choices[0]) if len(choices)==1 else any(a!=b for a in choices[0] for b in choices[1])
        require(any(usable(dict(members)) for members in (groups or [[]])),'no explicitly declared usable wire pin combination')

    def swio_clock(self, fn, name):
        values=dict(reversed(self.rows[fn]));tag=self.c.reg['describe_common']['min_clock_hz']
        require(tag in values and len(values[tag])==4 and struct.unpack('<I',values[tag])[0]>0,'SWIO requires its fixed bit timing as min_clock_hz')

    def mechanisms(self, fn, name):
        spec=self.specs[name];tag=spec['tlv']['describe']['mechanisms'];values=dict(reversed(self.rows[fn]))
        require(tag in values,'console mechanisms declaration missing')
        raw=values[tag];require(len(raw)>=1 and raw[0]>=1 and len(raw)==1+raw[0],'console mechanisms is n(u8) n x mechanism, n >= 1')
        listed=list(raw[1:]);require(len(set(listed))==len(listed),'console mechanisms listed twice')
        known={value for key,value in spec['enum']['mechanism'].items() if key!='none'}
        require(all(value in known for value in listed),'unknown or preset-only console mechanism declared')
        write={row['name']:row['code'] for row in spec['op']}['write'];both={spec['enum']['mechanism'][k] for k in ('dmdata','dmseq')}
        require(write not in self.offered(fn) or any(value in both for value in listed),'console write offered without mechanism 1 or 2')   # console §1
        self.c.observed.setdefault('console_mechanisms',{})[str(fn)]=listed

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
