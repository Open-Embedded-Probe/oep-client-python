import pytest

from oep_client import Caps, Channel, Connection, ConnectionManifest, PeripheralGroup, resolve, resolve_group, resolve_plan


def test_resolves_declared_uart_wiring():
    caps = Caps((Channel(1, frozenset(("uart.rx",))),
                 Channel(2, frozenset(("uart.tx",)))))
    manifest = ConnectionManifest((Connection("dut.tx", 1), Connection("dut.rx", 2)))
    assert resolve(caps, manifest, {"dut.tx": "uart.rx", "dut.rx": "uart.tx"}) == {"dut.tx": 1, "dut.rx": 2}


@pytest.mark.parametrize("manifest,required", [
    (ConnectionManifest(()), {"dut.tx": "uart.rx"}),
    (ConnectionManifest((Connection("dut.tx", 1),)), {"dut.tx": "uart.tx"}),
    (ConnectionManifest((Connection("a", 1), Connection("b", 1))), {"a": "uart.rx", "b": "uart.rx"}),
])
def test_rejects_invalid_allocation(manifest, required):
    caps = Caps((Channel(1, frozenset(("uart.rx",))),))
    with pytest.raises(ValueError):
        resolve(caps, manifest, required)


def test_rejects_output_on_input_only_channel():
    caps = Caps((Channel(46, frozenset(("gpio.out",)), input_only=True),))
    with pytest.raises(ValueError, match="input-only"):
        resolve(caps, ConnectionManifest((Connection("drive", 46),)),
                {"drive": "gpio.out"})


def test_resolves_all_uart_group_roles_or_nothing():
    caps = Caps((Channel(1, frozenset(("uart.rx",))), Channel(2, frozenset(("uart.tx",)))),
                (PeripheralGroup("uart0", "uart", frozenset(("dut.tx", "dut.rx"))),))
    manifest = ConnectionManifest((Connection("dut.tx", 1), Connection("dut.rx", 2)))
    assert resolve_group(caps, manifest, "uart0", {"dut.tx": "uart.rx", "dut.rx": "uart.tx"}) == {"dut.tx": 1, "dut.rx": 2}
    with pytest.raises(ValueError, match="requires roles"):
        resolve_group(caps, manifest, "uart0", {"dut.tx": "uart.rx"})


def test_rejects_exclusive_groups_before_allocation():
    caps = Caps((), (PeripheralGroup("uart0", "uart", frozenset(), frozenset(("capture0",))),
                     PeripheralGroup("capture0", "capture", frozenset())))
    with pytest.raises(ValueError, match="conflict"):
        resolve_plan(caps, ConnectionManifest(()), {"uart0": {}, "capture0": {}})
