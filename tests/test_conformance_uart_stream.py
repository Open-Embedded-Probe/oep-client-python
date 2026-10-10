import pytest
from oep_client.conformance_uart_stream import UartStreamChecks
from oep_client.conformance_uart_stream_sample import SampleUartStreamAdapter
from test_conformance_uart import setup


def test_uart_common_stream():
    model,c=setup();r=UartStreamChecks(c,SampleUartStreamAdapter(model)).run();assert r['status']=='passed',[(r['id'],r.get('error')) for r in r['checks'] if r['status']!='passed'];assert len(r['checks'])==15 and not r['full_conformance'];assert all(r['level']=='interface' for r in r['checks']);assert model.ep.current_core.holder is None and model.extension.stream is None


@pytest.mark.parametrize('field,value',[('fn',1),('byte_capacity',63),('tx_capacity',8),('addressing','resource')])
def test_common_adoption_mismatch_stops(field,value):
    model,c=setup();adapter=SampleUartStreamAdapter(model);adapter.adoption=dict(adapter.adoption,**{field:value});r=UartStreamChecks(c,adapter).run();assert r['status']=='failed';assert all(row['status']=='blocked' for row in r['checks'][1:])


@pytest.mark.parametrize('position,serial',[(-1,0),(2**64,0),(0,-1),(0,2**32),(True,0)])
def test_stream_seed_control_rejects_invalid_input(position,serial):
    model,_=setup()
    with pytest.raises(ValueError):model.uart_seed(position,serial)


@pytest.mark.parametrize('name',['EMPTY','POSITION','FRAME','GAP','SELECTORS','PAGES','WRAP','EVICTION','MARK-EVICTED','MARK-GAP','LIMIT','CLEAR','REFUSALS','WRITE'])
def test_common_wrong_result_detected(name):
    model,c=setup();original=model.handle
    def broken(request):
        reply=original(request)
        if request[3:5]==b'\2\0' and reply[3]==1:reply=reply[:4]+b'\1'+reply[5:]
        return reply
    c.send=broken;r=UartStreamChecks(c,SampleUartStreamAdapter(model)).run();assert next(row for row in r['checks'] if row['id']=='IF-UART-STREAM-'+name)['status']=='failed'
