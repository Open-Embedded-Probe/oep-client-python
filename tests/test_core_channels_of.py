"""core.channels_of: lines named as kept in scripts -> the probe's channel numbers for this boot (a channel name, any
ASCII case, or a port's line label S.N / S@P, host guide §5.3); a name the probe does not know is refused by name."""

import pytest

from oep_client import core, endpoint, virtual_bench, host as h


def open_probe():
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), lambda: 0)
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return hst


def test_channel_names_and_labels_give_channel_numbers():
    hst = open_probe()
    assert core.channels_of(hst, ["GPIO10", "gpio11"]) == [10, 11]
    assert core.channels_of(hst, ["x035.swdio", "x035.swclk"]) == [2, 54]   # labels in the profile's settings
    assert core.channels_of(hst, []) == []


def test_a_name_the_probe_does_not_know_is_named():
    hst = open_probe()
    with pytest.raises(ValueError, match="GPIO999: no channel of that name"):
        core.channels_of(hst, ["GPIO999"])
    with pytest.raises(ValueError, match="nope.swdio: no channel carries that label"):
        core.channels_of(hst, ["nope.swdio"])
