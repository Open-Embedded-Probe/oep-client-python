import pytest
from oep_client.conformance_rollover import RolloverChecks
from oep_client.conformance_rollover_sample import SampleRolloverHost
from test_conformance_recovery import setup as base


def setup():
    now,model,adapter,runner=base()
    return now,model,adapter,RolloverChecks(runner.c,adapter)


def test_rollover_passes():
    _,model,_,runner=setup()
    report=runner.run()
    assert report['status']=='passed',[(x['id'],x.get('error')) for x in report['checks']]
    assert len(report['checks'])==23
    assert len(report['rollover_observations'])==5
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('defect',['unsafe_last','ignore_pending','keep_binding','rewrite_unknown','reuse_sid','ignore_boot','skip_barrier'])
def test_rollover_host_mutants_fail(defect):
    _,_,_,runner=setup()
    class Broken(SampleRolloverHost):
        def request(self,op,payload=b'',*,fn=0):
            if defect=='unsafe_last' and self.c.corr==65534 and fn:
                request=self.c.request(op,payload,session=self.c.session,fn=fn)
                self.pending.track(self.route,request,self.boot)
                return request
            return super().request(op,payload,fn=fn)
        def choose_sid(self):
            return self.c.session if defect=='reuse_sid' else super().choose_sid()
        def switch_session(self):
            if defect=='keep_binding':
                class Kept(dict):
                    def clear(self):pass
                self.bindings=Kept(self.bindings)
            if defect=='rewrite_unknown':
                for row in self.pending.records:
                    if row['status']=='unknown':row['status']='completed'
            if defect=='skip_barrier':self.reader_barrier=lambda:True
            snapshot=self.pending.snapshot
            if defect=='ignore_pending':self.pending.snapshot=lambda:[row for row in snapshot() if row['status']!='pending']
            try:result=super().switch_session()
            finally:self.pending.snapshot=snapshot
            if defect=='ignore_boot' and result['status']=='boot-changed':result['status']='switched'
            return result
    runner.make_host=Broken
    report=runner.run()
    assert report['status']=='failed'
    assert any(x['id'].startswith('CORE-ROLLOVER-') and x['status']=='failed' for x in report['checks'])


@pytest.mark.parametrize('invalid',[0,-1,True,0x100000000,'reuse'])
def test_invalid_new_session_rejected_before_end(invalid):
    _,_,_,runner=setup();runner.c.confirm();runner.c.identity()
    with runner.c.holding(60000) as sid:
        host=runner.host();runner.c.corr=65534
        host.choose_sid=lambda:sid if invalid=='reuse' else invalid
        runner.assert_blocked(host,host.switch_session)


def test_too_late_never_wraps_and_does_not_send():
    _,_,_,runner=setup();runner.c.confirm();runner.c.identity()
    with runner.c.holding(60000):
        host=runner.host();previous=runner.c.corr
        runner.c.corr=65535
        try:runner.assert_blocked(host,host.switch_session)
        finally:runner.c.corr=previous  # fixture only; no large corr was actually sent


def test_abandoned_route_never_sends_reserved_end():
    _,_,_,runner=setup();runner.c.confirm();runner.c.identity()
    with runner.c.holding(60000):
        host=runner.host();runner.c.corr=65533
        host.request(16,b'\x01\x07',fn=1)  # host pending, not sent
        host.pending.abandon(host.route)
        runner.assert_blocked(host,host.switch_session)
        assert host.pending.snapshot()[0]['status']=='unknown'


def test_lost_end_stops_without_new_open_or_automatic_retry():
    _,model,adapter,runner=setup();runner.c.confirm();runner.c.identity()
    with runner.c.holding(60000) as sid:
        host=runner.host();runner.c.corr=65534
        send=runner.c.send
        def lost_end(request):
            send(request)
            raise TimeoutError('explicit logical end reply loss')
        runner.c.send=lost_end
        first=len(runner.c.trace)
        try:
            with pytest.raises(TimeoutError):host.switch_session()
        finally:runner.c.send=send
        assert host.state=='end-unknown' and runner.c.session is None
        assert host.route in host.pending.quarantined
        assert [x['status'] for x in host.pending.snapshot()]==['unknown']
        assert len(runner.c.trace[first:])==1 and runner.c.corr==65535
        assert model.ep.current_core.holder is None


def test_lost_open_stops_and_retires_attempted_session():
    now,model,adapter,runner=setup();runner.c.confirm();runner.c.identity()
    with runner.c.holding(60000):
        host=runner.host();runner.c.corr=65534
        send=runner.c.send
        def lost_open(request):
            import struct
            frame=send(request)
            if struct.unpack_from('<BHHBI',request)[3]==runner.c.core['open']:
                raise TimeoutError('explicit logical new-open reply loss')
            return frame
        runner.c.send=lost_open
        try:
            with pytest.raises(TimeoutError):host.switch_session()
        finally:runner.c.send=send
        assert host.state=='open-unknown' and runner.c.session is None and not host.bindings
        assert host.route in host.pending.quarantined
        assert [x['status'] for x in host.pending.snapshot()]==['completed','unknown']
        attempted=host.pending.snapshot()[-1]['session_id']
        assert attempted in host.pending.retired_sessions
        assert model.ep.current_core.holder==attempted
        now[0]+=60050;adapter.service()  # explicit fixture expiry; no automatic host retry/force
        assert model.ep.current_core.holder is None
