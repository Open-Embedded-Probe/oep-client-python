import pytest
from oep_client.conformance_tcp_recovery import TcpRecoveryChecks
from oep_client.conformance_tcp_recovery_sample import SampleTcpRecoveryAdapter

SPEC='/explicit/tcp-recovery-spec'
@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_tcp_recovery.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)

def test_tcp_recovery():
    report=TcpRecoveryChecks(SampleTcpRecoveryAdapter(),spec=SPEC).run()
    assert report['status']=='passed',report['checks']
    assert len(report['checks'])==7 and len(report['observations'])==6

@pytest.mark.parametrize('missing',['run_case','source_sha256'])
def test_missing_controls(missing):
    adapter=SampleTcpRecoveryAdapter();setattr(adapter,missing,None)
    assert TcpRecoveryChecks(adapter,spec=SPEC).run()['checks'][0]['status']=='failed'

@pytest.mark.parametrize('fault,case',[
    ('request','LENGTH'),('result','SHAPE'),('lost_live','EOF'),('holder','LENGTH'),
    ('next_id','SHAPE'),('pause_reconnect','PAUSE-HEADER'),('pause_discard','PAUSE-BODY'),
    ('pause_early','PAUSE-BODY'),('no_error','EOF'),('no_close','LENGTH'),
    ('reset_cache','SHAPE'),('no_confirm','EOF'),('repeat_before_confirm','LENGTH'),
    ('reexecute','LENGTH'),('lease','SHAPE'),('blocked_replay','BLOCKED'),
    ('boot','LENGTH'),('confirm_corr','SHAPE'),('confirm_magic','EOF')])
def test_observation_faults(fault,case):
    adapter=SampleTcpRecoveryAdapter();run=adapter.run_case
    def broken(name):
        r=run(name)
        if name!=case:return r
        events=r['events'];alloc=[e for e in events if e['action']=='request' and bytes.fromhex(e['request_hex'])[3:6]==b'\1\0\x10']
        if fault=='request':alloc[-1]['request_hex']=alloc[-1]['request_hex'][:-2]+'02'
        if fault=='result':next(e for e in events if e['action']=='result')['reply_hex']='02020001000200'
        if fault=='lost_live':r['state']['live']={}
        if fault=='holder':r['state']['holder']=None
        if fault=='next_id':r['state']['next_id']=3
        if fault=='pause_reconnect':r['connections']=2
        if fault=='pause_discard':next(e for e in events if e['action']=='wait')['buffered']=''
        if fault=='pause_early':next(e for e in events if e['action']=='result')['time_ns']=200_000_000
        if fault=='no_error':next(e for e in events if e['action']=='broken')['error']=None
        if fault=='no_close':next(e for e in events if e['action']=='close')['closed']=False
        if fault=='reset_cache':[e for e in events if e['action']=='connect'][1]['state']['cache']={}
        if fault=='no_confirm':next(e for e in events if e['action']=='request' and e['epoch']==1)['request_hex']=alloc[0]['request_hex']
        if fault=='repeat_before_confirm':events.remove(alloc[-1]);events.insert(0,alloc[-1])
        if fault=='reexecute':alloc[-1]['after']['next_id']=3
        if fault=='lease':next(e for e in events if e['action']=='preserved')['after']['deadline']+=1
        if fault=='blocked_replay':events.append(dict(alloc[0],epoch=1))
        if fault in ('boot','confirm_corr','confirm_magic'):
            confirm=next(e for e in events if e['action']=='confirm');raw=bytearray.fromhex(confirm['reply_hex'])
            raw[{'boot':18,'confirm_corr':1,'confirm_magic':5}[fault]]^=1
            confirm['reply_hex']=raw.hex()
        return r
    adapter.run_case=broken
    report=TcpRecoveryChecks(adapter,spec=SPEC).run()
    row=next(r for r in report['checks'] if r['id']=='TCP-RECOVERY-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row

@pytest.mark.parametrize('fault,case',[
    ('stall_tcp','PAUSE-HEADER'),('discard_partial','PAUSE-BODY'),
    ('forget_cache','LENGTH'),('retry_renews','SHAPE'),('no_close','EOF')])
def test_actual_frames_core_faults(monkeypatch,fault,case):
    from oep_client import frames
    from oep_client.virtual_core import Core
    from oep_client.conformance_tcp_recovery_sample import TcpFixtureStream
    adapter=SampleTcpRecoveryAdapter();run=adapter.run_case
    def broken(name):
        if name!=case:return run(name)
        with monkeypatch.context() as m:
            if fault=='stall_tcp':
                init=frames.LengthFrames.__init__
                def bad_init(self,*args,**kwargs):init(self,*args,**kwargs);self.stall_s=.05
                m.setattr(frames.LengthFrames,'__init__',bad_init)
            if fault=='discard_partial':
                recv=frames.LengthFrames.recv
                def bad_recv(self,*args):
                    r=recv(self,*args)
                    if r is None:self._buffer.clear()
                    return r
                m.setattr(frames.LengthFrames,'recv',bad_recv)
            if fault in ('forget_cache','retry_renews'):
                handle=Core.handle
                def bad_handle(core,data,*args,**kwargs):
                    replay=data[3:6]==b'\1\0\x10' and int.from_bytes(data[1:3],'little') in core.cache
                    if replay and fault=='forget_cache':core.cache.clear();core.high=0
                    result=handle(core,data,*args,**kwargs)
                    if replay and fault=='retry_renews':core.deadline+=1
                    return result
                m.setattr(Core,'handle',bad_handle)
            if fault=='no_close':m.setattr(TcpFixtureStream,'close',lambda self:None)
            return run(name)
    adapter.run_case=broken
    report=TcpRecoveryChecks(adapter,spec=SPEC).run()
    assert next(r for r in report['checks'] if r['id']=='TCP-RECOVERY-'+case)['status']=='failed'
