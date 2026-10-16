import pytest
from spec_names import SPEC
from oep_client.conformance import Checks
from oep_client.conformance_dmseq import DmseqChecks, crc_reference
from oep_client.conformance_dmseq_sample import SampleDmseqAdapter
from oep_client.virtual_dmseq_model import DmseqModel
from test_conformance_mailbox import setup as mailbox_setup


def setup():
    _,c,_=mailbox_setup();model=DmseqModel(lambda:0,boot_id=17)
    c.reg['common']=SPEC['common']                                  # the common enums by name (tests/spec_names.py)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleDmseqAdapter(model)


def test_logical_probe_dmseq_contracts_pass():
    _,c,a=setup();r=DmseqChecks(c,a).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==35 and not r['full_conformance']
    assert all(x['level']=='oep-interface' for x in r['checks'])


@pytest.mark.parametrize('data,expected',[(b'123456789',0xfb),(b'\0',0xf3)])
def test_normative_crc_vectors(data,expected):assert crc_reference(data)==expected


def test_unsynced_unused_fields_need_no_zero_seed():
    model,c,a=setup();install=model.install;reset=model.reset_target
    def seed_unused(p):p.update(last_s=1,last_syn=True,h=1)
    def seeded_install():
        install();e=model.extension;dispatch=e.dispatch_request
        def seeded_dispatch(fn,op,payload,tags):
            value=dispatch(fn,op,payload,tags)
            for p in e.protocols.values():
                if not p['synced']:seed_unused(p)
            return value
        e.dispatch_request=seeded_dispatch
    def seeded_reset(parent):
        reset(parent)
        for p in model.extension.protocols.values():
            if not p['synced']:seed_unused(p)
    model.install=seeded_install;model.reset_target=seeded_reset
    report=DmseqChecks(c,a).run()
    assert report['status']=='passed',report['checks']


@pytest.mark.parametrize('fault,case',[
    ('wrong_crc','VECTOR'),('skip_data1','LENGTHS'),('duplicate_delivery','DUP-SYN'),
    ('same_s_syn_lost','SAME-S-SYN'),('to_resync','DUP-TO'),
    ('duplicate_ack_ignored','DUP-ACK'),('pop_twice','DUP-PLAIN'),
    ('new_payload_lost','IDENTICAL-NEW'),('invalid_answer','N7'),
    ('host_turn_write','HOST-TURN'),('wrong_h','SYN'),('halt_access','HALTED'),
    ('debug_access','DEBUG'),('reset_keep_protocol','RESET'),('reset_drop_output','RESET'),
])
def test_dmseq_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install;reset=model.reset_target
    def faulty_install():
        install();e=model.extension;poll=e.poll
        def broken_poll(rid):
            import copy
            m=e.mailboxes[rid];p=e.protocols[rid];stream=e.streams[rid][1]
            before=copy.deepcopy(p);old_data=bytes(stream.data);old_pos=stream.position;old_tx=bytes(stream.tx);d0=m['data0'];s=(d0>>5)&1;syn=bool(d0&8)
            if fault=='to_resync' and d0&64:p.update(synced=False)
            poll(rid)
            if fault=='wrong_crc' and m['events'] and m['events'][-1][:2]==('write',4):
                m['data0']^=256;m['events'][-1]=('write',4,m['data0'])
            elif fault=='skip_data1':m['events']=[row for row in m['events'] if row[:2]!=('read',5)]
            elif fault=='duplicate_delivery' and before['synced'] and s==before['last_s'] and (not syn or before['last_syn']):stream.feed(b'x')
            elif fault=='same_s_syn_lost' and before['synced'] and syn and not before['last_syn'] and s==before['last_s']:
                stream.data[:]=old_data;stream.position=old_pos;p.update(before)
            elif fault=='duplicate_ack_ignored' and before['pending'] and s==before['last_s'] and (d0>>4)&1==before['h']:p.update(before);stream.tx[:]=old_tx
            elif fault=='pop_twice' and before['synced'] and s==before['last_s']:del stream.tx[:2]
            elif fault=='new_payload_lost' and before['synced'] and s!=before['last_s']:stream.data[:]=old_data;stream.position=old_pos
            elif fault=='invalid_answer' and d0&128 and d0&7==7:m['data0']=0;m['events'].append(('write',4,0))
            elif fault=='host_turn_write' and not d0&128:m['data0']=0;m['events'].append(('write',4,0))
            elif fault=='wrong_h':p['h']^=1
            elif fault=='halt_access' and m['halted']:m['events'].append(('read',4,d0))
            elif fault=='debug_access' and m['debug_busy']:m['events'].append(('read',4,d0))
        e.poll=broken_poll
    def broken_reset(parent):
        import copy
        protocols=copy.deepcopy(model.extension.protocols);reset(parent)
        if fault=='reset_keep_protocol':model.extension.protocols=protocols
        if fault=='reset_drop_output':
            for _,stream in model.extension.streams.values():stream.data.clear()
    model.install=faulty_install;model.reset_target=broken_reset
    r=DmseqChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-DMSEQ-'+case)
    assert row['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','mechanism','marks','absent'])
def test_dmseq_preflight_blocks(invalid):
    model,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='mechanism':c.reg['interface'][3]['names']['mechanism'].pop('dmseq')   # a SPEC without dmseq
    elif invalid=='marks':c.reg['interface'][3]['enum']['mark_kind']['restart']=3
    else:model.extension.mechanisms=('sdi','dmdata')
    r=DmseqChecks(c,a).run()
    assert r['checks'][0]['status']=='failed'
    assert all(x['status']=='blocked' for x in r['checks'][1:])
    assert not a.trace


@pytest.mark.parametrize('kind,detail,case',[(0x41,2,'RESYNC-SAME'),(0x41,2,'RESYNC-NEW'),(1,0x40,'TO-NEW'),(1,0x40,'TO-DUP'),(1,2,'CRC-FLAGS')])
def test_event_checker_detects_missing_mark(kind,detail,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();poll=model.extension.poll
        def broken(rid):
            poll(rid);stream=model.extension.streams[rid][1]
            stream.marks[:]=[m for m in stream.marks if (m[2],m[4])!=(kind,detail)]
        model.extension.poll=broken
    model.install=faulty_install
    report=DmseqChecks(c,a).run()
    assert next(r for r in report['checks'] if r['id']=='OEP-DMSEQ-MARK-'+case)['status']=='failed'

@pytest.mark.parametrize('invalid_case', ['CRC-FLAGS','INVALID-FLAGS'])
def test_invalid_flags_do_not_trigger_resync_or_to(invalid_case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();poll=model.extension.poll
        def broken(rid):
            frame=model.extension.mailboxes[rid]['data0'];poll(rid)
            if frame&8:model.extension.streams[rid][1].mark(0x41,2)
            if frame&64:model.extension.streams[rid][1].mark(1,0x40)
        model.extension.poll=broken
    model.install=faulty_install
    report=DmseqChecks(c,a).run()
    assert next(r for r in report['checks'] if r['id']=='OEP-DMSEQ-MARK-'+invalid_case)['status']=='failed'


def test_repeated_mark_coalescing_is_allowed():
    model,c,a=setup();install=model.install
    def coalescing_install():
        install();e=model.extension;poll=e.poll;last={}
        def coalesced(rid):
            frame=(e.mailboxes[rid]['data0'],e.mailboxes[rid]['data1']);stream=e.streams[rid][1]
            old_marks=list(stream.marks);old_serial=stream.serial;poll(rid)
            if last.get(rid)==frame:stream.marks[:]=old_marks;stream.serial=old_serial
            last[rid]=frame
        e.poll=coalesced
    model.install=coalescing_install
    report=DmseqChecks(c,a).run()
    assert report['status']=='passed',report['checks']
