"""force_hw.run on the virtual bench's P4 + X035 profile: force closes S's debug connection, its number is no_resource
for the taker, S's requests are not completed, and the taker's attach gets a new, larger number (core §6.4, §9)."""
import copy

from oep_client import endpoint, virtual_bench
from oep_client.conformance import Checks
from oep_client.conformance.force_hw import run
from spec_names import SPEC


def bench():
    clock = [0]
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), lambda: clock[0], boot_id=23)
    check = Checks(lambda message: ep.handle(message), copy.deepcopy(SPEC), 'p4-x035-0001')
    return ep, check


def test_force_closes_the_connection_of_the_session_it_takes_from():
    ep, check = bench()
    check.confirm()
    out = run(check, wire_name='oep.wire.rvswd', swdio=2, swclk=54, speed=1_000_000)
    assert out['after_force'] == [] and out['t_connection'] > out['s_connection']
    assert check.session is None
    assert check.success(check.request(check.core['lock_state']))[0] == 0   # T ended: nothing holds the lock
