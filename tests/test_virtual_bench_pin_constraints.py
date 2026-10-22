"""Core §7.4 pin constraint intersection through plan requests (the settings hold no plan).

These test the existing logical Endpoint's selection semantics. Its legacy
operation numbering is not evidence of current interface-schema conformance.
"""
import copy
from dataclasses import replace
import struct

import pytest

from oep_client import catalog, endpoint, message as m, registry as reg, virtual_bench
from test_virtual_bench_rules_2026_10_02 import Clock, Host


CHANNELS = (4, 5, 13, 14)


CASES = [
    ('groups', (0, 1), True), ('groups', (2, 3), True), ('groups', (0, 3), False),
    ('intersection', (0, 1), True), ('intersection', (2, 3), False),
    ('partial', (0, 1), True), ('partial', (0, 3), True),
    ('partial', (2, 1), True), ('partial', (2, 3), True),
    ('partial', (1, 3), False), ('partial', (0, 2), False),
    ('roles', (0, 1), True), ('roles', (2, 3), True), ('roles', (1, 2), False),
]


def fixture(kind, interface='oep.fixture.i2c-target'):
    groups = [catalog.channel_group(1, [(1, 4), (2, 5)]), catalog.channel_group(2, [(1, 13), (2, 14)])]
    roles = [catalog.role_channels(1, [4, 13]), catalog.role_channels(2, [5, 14])]
    if kind == 'intersection':
        roles = [catalog.role_channels(1, [4, 13]), catalog.role_channels(2, [5])]
    if kind == 'partial':
        groups = [catalog.channel_group(1, [(1, 4)]), catalog.channel_group(2, [(1, 13)])]
        # Repeated role declarations union, even when the role is not grouped.
        roles = [catalog.role_channels(2, [5]), catalog.role_channels(2, [14])]
    if kind == 'spi-partial':
        roles = [catalog.role_channels(3, [17, 18]), catalog.role_channels(4, [19, 21])]
    if kind == 'unlisted':
        groups = [catalog.channel_group(1, [(1, 4)]), catalog.channel_group(2, [(1, 5)])]
        roles = []
    declarations = groups if kind == 'groups' else roles if kind == 'roles' else groups + roles
    probe = virtual_bench.esp32_v003()
    fn = next(o.fn for o in probe.offered if o.name == interface)
    probe.offered = [replace(o, tlvs=tuple(t for t in o.tlvs if m.split_tlvs(t)[0][0] not in (1, 6)) + tuple(declarations))
                     if o.fn == fn else o for o in probe.offered]
    ep = endpoint.Endpoint(probe, Clock()); h = Host(ep)
    assert h.open().succeeded
    return ep, h, fn


def apply(h, fn, pair, path):
    assert path == 'plan'
    tag = reg.PROBE_PLAN.tlv['plan_apply']['role_assignment'] | m.TAG_CRITICAL
    body = b''.join(bytes((tag, 5, 0)) + struct.pack('<HBH', fn, role, channel)
                    for role, channel in enumerate(pair, 1))
    return h.raw(h.ep.fns[reg.PROBE_PLAN.name], reg.PROBE_PLAN.op['plan_apply'], body)


def snapshot(ep):
    return copy.deepcopy((ep.plan, ep.config, ep.parked))


@pytest.mark.parametrize('path', ['plan'])
@pytest.mark.parametrize('kind,pair,allowed', CASES)
def test_channel_selection_obeys_combined_declarations(path, kind, pair, allowed):
    ep, h, fn = fixture(kind)
    pair = tuple(CHANNELS[c] for c in pair)
    assert apply(h, fn, CHANNELS[:2], path).succeeded
    before = snapshot(ep)
    result = apply(h, fn, pair, path)
    if allowed:
        assert result.succeeded, (kind, pair, result.describe())
        assert {(role, channel) for f, role, channel in ep.plan if f == fn} == set(enumerate(pair, 1))
    else:
        assert (result.resolution, result.detail) == (m.REJECTED, m.UNSUPPORTED)
        assert result.payload[0] == reg.PROBE_PLAN.tlv['plan_apply']['role_assignment'] | m.TAG_CRITICAL
        assert snapshot(ep) == before, 'rejected selection changed existing plan/settings/idle'


@pytest.mark.parametrize('path', ['plan'])
@pytest.mark.parametrize('pair,allowed', [((4, 5, 17, 19), True), ((13, 14, 18, 21), True),
                                       ((4, 14, 17, 19), False), ((4, 5, 13, 19), False)])
def test_spi_groups_leave_other_roles_to_candidate_constraints(path, pair, allowed):
    ep, h, fn = fixture('spi-partial', 'oep.fixture.spi-target')
    assert apply(h, fn, (4, 5, 17, 19), path).succeeded
    before = snapshot(ep)
    result = apply(h, fn, pair, path)
    if allowed:
        assert result.succeeded, result.describe()
        assert {(role, channel) for f, role, channel in ep.plan if f == fn} == set(enumerate(pair, 1))
    else:
        assert (result.resolution, result.detail) == (m.REJECTED, m.UNSUPPORTED)
        assert snapshot(ep) == before


@pytest.mark.parametrize('path', ['plan'])
@pytest.mark.parametrize('kind,pair', [('groups', (0, 3)), ('intersection', (2, 3)), ('partial', (0, 2))])
def test_selection_assertions_detect_bypassed_constraints(monkeypatch, path, kind, pair):
    monkeypatch.setattr(endpoint.Endpoint, '_check_plan', lambda *args, **kwargs: None)
    with pytest.raises(AssertionError):
        test_channel_selection_obeys_combined_declarations(path, kind, pair, False)


@pytest.mark.parametrize('path', ['plan'])
def test_role_outside_group_requires_its_own_candidates(path):
    ep, h, fn = fixture('unlisted')
    before = snapshot(ep)
    result = apply(h, fn, (4, 5), path)
    assert (result.resolution, result.detail) == (m.REJECTED, m.UNSUPPORTED)
    assert snapshot(ep) == before
