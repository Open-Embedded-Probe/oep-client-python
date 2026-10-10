"""Selected standard describe constraints; never executes advertised ops."""
import struct
from .conformance import require, ops
from .conformance_registered_ops import RegisteredOpsChecks


class SelectedMetadataChecks:
    supported={'oep.probe.restart','oep.probe.config','oep.target.riscv-dm','oep.target.arm-adi'}
    def __init__(self,checks,selection):self.c,self.selection=checks,selection
    def metadata(self,fn,name):
        reg=self.base.specs[name];rows=self.base.rows[fn];values=dict(reversed(rows));common=self.c.reg['describe_common'];offered=ops(values[common['ops']]);codes={r['name']:r['code'] for r in reg['op']}
        if name=='oep.probe.restart':
            tag=reg['tlv']['describe']['restart_max_ms'];require(tag in values and len(values[tag])==4,'mandatory restart_max_ms u32 missing or malformed')
            result=dict(restart_max_ms=struct.unpack('<I',values[tag])[0])
        elif name=='oep.probe.config':
            tag=reg['tlv']['describe']['storage'];result=dict(storage_present=tag in values)
            if tag in values:
                require(codes['save'] in offered and codes['erase'] in offered,'storage advertised without save/erase')
                require(len(values[tag])==4 and struct.unpack('<I',values[tag])[0]>=1,'storage max_bytes must be positive u32');result['max_bytes']=struct.unpack('<I',values[tag])[0]
        else:
            if name=='oep.target.arm-adi':require(common['features'] not in values,'arm-adi must not advertise features')
            active=codes['read_block'] in offered or codes['write_block'] in offered;result=dict(blocks=active)
            if active:
                tag=common['max_length'];require(tag in values and len(values[tag])==2,'block max_length u16 missing or malformed')
                length=struct.unpack('<H',values[tag])[0];require(length%4==0,'block max_length unit is aligned bytes')
                require(18+length<=self.c.max_frame and 8+length<=self.c.max_frame,'block request/response do not fit negotiated route')
                transport=self.c.reg['core']['tlv']['describe']['transport'];indices={bytes.fromhex(r['value_hex'])[0] for r in self.c.observed['core_describe'] if r['tag']==transport}
                require(18+length<=64 or indices=={self.c.confirm_transport},'other route limits are unconfirmed; cannot prove global block max_length')
                result.update(max_length=length,route_bound='protocol minimum 64' if 18+length<=64 else 'sole advertised route')
        self.c.observed.setdefault('selected_metadata',{})[str(fn)]=dict(name=name,**result)
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.c.reg['constants']['min_max_frame']==64,'selected protocol minimum frame differs')
            require(type(self.selection)is dict and self.selection and all(type(n)is str and n in self.supported for n in self.selection.values()),'explicit supported metadata selection required')
        self.c.check('OEP-METADATA-CONTEXT','restart §1; debug §4.5/6; probe-config §4',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        self.base=RegisteredOpsChecks(self.c,self.selection);self.base.run()
        for fn,name in (self.selection.items() if type(self.selection)is dict else []):self.c.check('OEP-METADATA-'+str(fn),'restart §1; debug §4.5/6; probe-config §4',lambda fn=fn,name=name:self.metadata(fn,name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,full_conformance=False,scope='selected standard op and restart/block/storage describe constraints; read-only',unchecked=['metadata of other standard interfaces','unconfirmed route limits beyond protocol minimum','storage presence when save/erase exist is not made mandatory here','restart duration, bus operations and storage behavior','physical transports and production Host integration'])
