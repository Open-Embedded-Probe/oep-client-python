"""Generic interface declaration checks; no standard-interface op expectations."""
import struct
from .conformance import require, ops, tlvs


class InterfaceChecks:
    def __init__(self, checks):
        self.c=checks
        self.snapshots={}

    def name(self, entry):
        labels=entry['name'].split('.')
        require(len(labels)>=2 and all(label and not label.startswith('-') and not label.endswith('-') for label in labels),
                'interface name requires at least two nonempty labels without edge hyphens')

    def declared_ops(self, entry):
        values=dict(reversed(self.snapshots[entry['fn']]))
        tag=self.c.reg['describe_common']['ops']
        require(tag in values,'mandatory ops declaration missing')
        offered=ops(values[tag])
        require(0 not in offered and not offered.intersection(range(3,16)),'unassigned common op declared')
        require((1 in offered)==(2 in offered),'subscribe/unsubscribe must be offered together')
        return offered

    def fixed(self, entry):
        values=dict(reversed(self.snapshots[entry['fn']]))
        tags=self.c.reg['describe_common']
        for name,width in [('max_clock_hz',4),('max_length',2),('min_clock_hz',4),('features',4)]:
            if tags[name] in values:require(len(values[tags[name]])==width,name+' fixed width')

    def channels(self, entry):
        core=dict((row['tag'],bytes.fromhex(row['value_hex'])) for row in reversed(self.c.observed['core_describe']))
        count=core.get(self.c.reg['core']['tlv']['describe']['channels'],b'\0\0')
        require(len(count)==2,'core channels u16')
        count=int.from_bytes(count,'little');tags=self.c.reg['describe_common']
        candidates,groups={},[]
        for tag,value in self.snapshots[entry['fn']]:
            if tag==tags['role_channels']:
                require(len(value)>=3,'role_channels fixed header')
                role,base=struct.unpack_from('<BH',value)
                selected={base+i for i in range(8*(len(value)-3)) if value[3+i//8] & (1<<(i%8))}
                require(all(channel<count for channel in selected),'role_channels outside declared channels')
                candidates.setdefault(role,set()).update(selected)
            elif tag==tags['channel_group']:
                require(len(value)>=2 and len(value)==2+3*value[1],'channel_group count/shape')
                members=[struct.unpack_from('<BH',value,at) for at in range(2,len(value),3)]
                require(all(channel<count for _,channel in members),'channel_group outside declared channels')
                groups.append({'group':value[0],'members':members})
        entry['channel_declarations']={'role_candidates':{str(role):sorted(pins) for role,pins in candidates.items()},'groups':groups}

    def pages(self, entry):
        fn=entry['fn'];rows=self.snapshots[fn]
        for first in range(len(rows)+1):
            payload=self.c.success(self.c.request(self.c.core['describe'],struct.pack('<HH',fn,first)))
            require(payload and payload[0] in (0,1),'describe page flag')
            page=tlvs(payload[1:]);suffix=rows[first:]
            require(page==suffix[:len(page)] and len(page)<=len(suffix),'describe cursor does not select TLV suffix')
            require(payload[0]==int(len(page)<len(suffix)) and (not payload[0] or page),'describe more/progress')
        require(self.c.success(self.c.request(self.c.core['describe'],struct.pack('<HH',fn,65535)))==b'\0','describe past end')
        require(all(len(value)+9<=self.c.max_frame for _,value in rows),'declaration TLV exceeds frame limit')

    def stable(self, entry):
        fn=entry['fn'];rows=self.snapshots[fn]
        expected=[{key:row[key] for key in ('fn','instance','revision','name')} for row in self.entries]
        require(self.c.list_snapshot()==expected,'interface list changed within boot')
        require(self.c.describe(fn)==rows,'declarations changed while unlocked')
        with self.c.holding(3000):
            require(self.c.describe(fn)==rows,'declarations changed while session held')
        require(self.c.describe(fn)==rows,'declarations changed after end')

    def absent_ops(self, entry):
        offered=self.declared_ops(entry)
        for op in sorted(set(range(256))-offered):
            self.c.rejected(self.c.request(op,fn=entry['fn']),'unknown_operation')

    def run(self):
        start=len(self.c.results)
        def prepare():
            self.c.confirm();self.c.identity();self.c.declarations()
            self.entries=self.c.list_snapshot()
            for entry in self.entries:
                rows=self.c.describe(entry['fn']);self.snapshots[entry['fn']]=rows
                entry['describe']=[{'tag':tag,'value_hex':value.hex()} for tag,value in rows]
            self.c.observed['interfaces']=self.entries
        self.c.check('IF-DECLARATION-CONTEXT','core §7.1–7.5; explicit probe',prepare)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for entry in getattr(self,'entries',[]):
            for name,method,clause in [('NAME',self.name,'13 rule 1'),('OPS',self.declared_ops,'1.2/7.4/12'),
                                      ('FIXED',self.fixed,'2.3/7.4'),('CHANNELS',self.channels,'7.4/7.5'),
                                      ('PAGES',self.pages,'7.3'),('STABLE',self.stable,'7.2/7.3'),
                                      ('ABSENT-OPS',self.absent_ops,'1.2/4.3')]:
                self.c.check('IF-'+name+'-'+str(entry['fn']),'core §'+clause,
                             lambda entry=entry,method=method:method(entry))
            def instances(entry=entry):
                peers=sorted((row for row in self.entries if (row['name'],row['revision'])==(entry['name'],entry['revision'])),key=lambda row:row['fn'])
                require(entry['instance']==peers.index(entry),'instance must follow fn order within name/revision')
            self.c.check('IF-INSTANCE-'+str(entry['fn']),'core §7.2',instances)
        checks=self.c.results[start:]
        return {'status':'passed' if all(row['status']=='passed' for row in checks) else 'failed',
                'full_conformance':False,'scope':'generic interface declarations and unoffered operations',
                'levels':{'core':'discovery/session prerequisites only','interface':'selected generic declaration contracts',
                          'oep-interface':'not executed'},'checks':checks,'observed':self.c.observed,
                'interface_count':len(getattr(self,'entries',[])),
                'not_applicable':['per-function declaration checks: no interfaces exposed'] if not getattr(self,'entries',[]) else [],
                'unchecked':['offered operation behavior/mandatory standard-interface operations','physical pins/selection constraints',
                             'common stream/status components unless explicitly adopted','resources/subscriptions/notifications',
                             'firmware-version stability','other-route minimum frame size','concurrent writers']}
