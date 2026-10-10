from oep_client.conformance import Checks
from oep_client.conformance_mailbox import MailboxChecks
from oep_client.conformance_mailbox_sample import SampleMailboxAdapter
from oep_client.virtual_mailbox_model import MailboxModel
from test_conformance_scan import setup as scan_setup


def setup():
    _,c,_=scan_setup();model=MailboxModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleMailboxAdapter(model)


def test_logical_mailbox_contracts_pass():
    _,c,a=setup();report=MailboxChecks(c,a).run()
    assert report['status']=='passed',[(r['id'],r.get('error')) for r in report['checks'] if r['status']!='passed']
    assert len(report['checks'])==23 and not report['full_conformance']
    assert all(row['level']=='oep-interface' for row in report['checks'])


import pytest


@pytest.mark.parametrize('fault,case',[
    ('sdi_skip_data1','SDI-LENGTHS'),('sdi_clear_invalid','SDI-INVALID'),
    ('dm_read_data1_short','DM-LENGTHS'),('dm_skip_data1_long','DM-LENGTHS'),
    ('dm_write_data1','DM-LENGTHS'),('dm_consume_invalid','DM-INVALID'),
    ('dm_overwrite_own','DM-OWN-TURN'),('dm_send_reverse','DM-QUEUE-3'),
    ('dm_send_short','DM-QUEUE-3'),('halt_access','DM-HALTED'),
    ('busy_access','DM-DEBUG'),('status_after_data','DM-DEBUG'),
    ('sdi_zero_success','SDI-WRITE'),('bit6_reject','DM-BIT6'),
    ('ack_not_written','SDI-ACK'),
])
def test_mailbox_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;poll=e.poll;dispatch=e.dispatch_request
        def broken_poll(rid):
            m=e.mailboxes[rid];s=e.streams[rid][1];start=0;previous=bytes(s.tx);d0=m['data0'];mechanism=e.meta[rid]
            poll(rid);events=m['events'];delta=events[start:];length=d0&255
            if fault=='sdi_skip_data1' and not mechanism and 1<=length<=3:events[start:]=[row for row in delta if row[:2]!=('read',5)]
            elif fault=='sdi_clear_invalid' and not mechanism and length>=8:events.append(('write',4,0));m['data0']=0
            elif fault=='dm_read_data1_short' and mechanism and 5<=length&63<=7:events.insert(start+2,('read',5,m['data1']))
            elif fault=='dm_skip_data1_long' and mechanism and 8<=length&63<=11:events[start:]=[row for row in delta if row[:2]!=('read',5)]
            elif fault=='dm_write_data1' and mechanism:events.append(('write',5,0))
            elif fault=='dm_consume_invalid' and mechanism and length&128 and not 4<=length&63<=11:del s.tx[:1]
            elif fault=='dm_overwrite_own' and mechanism and not length&128:events.append(('write',4,0));m['data0']=0
            elif fault in ('dm_send_reverse','dm_send_short') and mechanism and len(previous)>=3:
                reply=previous[:3][::-1] if fault=='dm_send_reverse' else previous[:2]
                events[-1]=('write',4,(len(reply)+4)|int.from_bytes(reply,'little')<<8)
                if fault=='dm_send_short':s.tx[:]=previous[2:]
            elif fault=='halt_access' and m['halted']:events.append(('read',4,d0))
            elif fault=='busy_access' and m['debug_busy']:events.append(('read',4,d0))
            elif fault=='status_after_data' and len(delta)>=2:events[start],events[start+1]=events[start+1],events[start]
            elif fault=='bit6_reject' and mechanism and length&64:s.data.clear();s.position=0
            elif fault=='ack_not_written' and not mechanism and 1<=length<=7:m['data0']=d0
        def broken_dispatch(fn,op,payload,tags):
            value=dispatch(fn,op,payload,tags)
            if fault=='sdi_zero_success' and fn==4 and op==21 and payload[2:4]==b'\0\0':return bytes(value)
            return value
        e.poll=broken_poll;e.dispatch_request=broken_dispatch
    model.install=faulty_install
    report=MailboxChecks(c,a).run();row=next(r for r in report['checks'] if r['id']=='OEP-MAILBOX-'+case)
    assert row['status']=='failed',report['checks']


@pytest.mark.parametrize('invalid',['scope','registry','mechanisms'])
def test_mailbox_preflight_blocks(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='registry':c.reg['interface'][3]['enum']['mechanism']['sdi']=3
    else:a.mechanisms=[0]
    report=MailboxChecks(c,a).run()
    assert report['checks'][0]['status']=='failed'
    assert all(r['status']=='blocked' for r in report['checks'][1:])
    assert not a.trace


def test_owned_mailbox_fixture_cannot_restart():
    model,c,_=setup();c.confirm();c.identity()
    with c.holding(3000):
        before=model.mailbox_state()
        with pytest.raises(ValueError):model.restart_fixture()
        assert model.mailbox_state()==before


@pytest.mark.parametrize('method,args',[
    ('mailbox_frame',(1,0,0)),('mailbox_halted',(1,True)),('mailbox_debug_busy',(1,False)),
])
def test_mailbox_controls_require_live_resource(method,args):
    model,_,_=setup();before=model.mailbox_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.mailbox_state()==before
