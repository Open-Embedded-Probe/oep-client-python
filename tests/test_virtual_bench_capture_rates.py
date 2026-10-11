"""Capture §3.3: nearest realizable rate, without assuming rounding direction.

The divisor clock is this virtual source's choice, not an OEP wire requirement.
The small oracle enumerates the complete feasible set independently of settle.
"""
import copy
import struct
from fractions import Fraction

import pytest

from oep_client import capture, core, endpoint
from oep_client.virtual_bench_capture import VirtualCapture, TLV
from test_virtual_bench_capture import bench


def settle(track, asked, channels=1):
    values={TLV['mode']:b'\1', TLV['rate']:struct.pack('<I',asked),
            TLV['samples']:struct.pack('<I',8)}
    return track.settle(values,endpoint.Take(b''),channels,[])['rate']


@pytest.mark.parametrize('source,minimum',[(12,1),(20,3),(31,7),(64,33)])
def test_all_in_range_requests_use_a_nearest_feasible_divisor(source,minimum):
    track=VirtualCapture({1},{8},minimum,source)
    feasible={Fraction(source,divisor) for divisor in range(1,source//minimum+1)}
    for asked in range(minimum,source+1):
        before=copy.deepcopy(track)
        actual=settle(track,asked)
        assert actual in feasible and minimum<=actual<=source
        assert abs(actual-asked)==min(abs(rate-asked) for rate in feasible),(asked,actual)
        assert track==before,'query calculation changed capture state'


@pytest.mark.parametrize('asked,expected',[(740,500),(750,None),(760,1000),(100,100),(101,Fraction(1000,10)),(1000,1000)])
def test_upper_lower_and_equal_distance_rates(asked,expected):
    track=VirtualCapture({1},{8},100,1000)
    actual=settle(track,asked)
    if expected is None:assert actual in (Fraction(500),Fraction(1000))
    else:assert actual==expected


def test_lowest_request_does_not_round_below_declared_minimum():
    assert settle(VirtualCapture({1},{8},6,20),6)==Fraction(20,3)


@pytest.mark.parametrize('source',[59,60])
@pytest.mark.parametrize('channels',[1,2,3,4])
def test_analog_rate_uses_nearest_value_within_shared_adc_budget(channels,source):
    track=VirtualCapture({1},{16},1,source,frontends={0:(0,3300,0)})
    feasible={Fraction(source,divisor) for divisor in range(1,source+1) if Fraction(source,divisor)*channels<=source}
    for asked in range(1,source+1):
        actual=settle(track,asked,channels)
        assert actual in feasible
        assert abs(actual-asked)==min(abs(rate-asked) for rate in feasible)


@pytest.mark.parametrize('query',[True,False])
def test_logic_wire_answer_can_round_above_request(query):
    ep,hst,lc,_=bench()
    core.plan_apply(hst,[(lc.fn,0,20)])
    before=copy.deepcopy(ep.captures[lc.fn])
    result=lc.configure(rate=17_000_000,samples=8,query=query)
    assert result.rate==20_000_000
    if query:
        assert ep.captures[lc.fn]==before and lc.config is None
    else:assert ep.captures[lc.fn].rate==result.rate


def test_analog_wire_answer_reports_nearest_rate_and_matching_skew():
    ep,hst,_,_=bench();an=capture.AnalogCapture(hst)
    core.plan_apply(hst,[(an.fn,0,16),(an.fn,1,17)])
    cfg=an.configure(rate=10_000,samples=8)
    assert cfg.rate==Fraction(83_333,8)
    assert cfg.skew_ns[1]==int(Fraction(1_000_000_000,1)/(cfg.rate*2))


@pytest.mark.parametrize('asked,expected',[(3_221_225_471,Fraction(4_294_967_295,2)),(3_221_225_472,Fraction(4_294_967_295))])
def test_u32_source_selects_the_closest_side_of_fractional_midpoint(asked,expected):
    assert settle(VirtualCapture({1},{8},1,4_294_967_295),asked)==expected


def test_exact_rate_does_not_quantize_to_divisor_clocks():
    track=VirtualCapture({1},{8},3,31,exact=True)
    for asked in range(3,32):assert settle(track,asked)==asked
