import pytest

from oep_client import (
    Allocation, Caps, Channel, ConfigurePlan, Connection, ConnectionManifest,
    LeaseRegistry, PeripheralGroup, RoleRequest, resolve, resolve_group,
    resolve_plan, validate_caps, VoltageDomain,
)


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


def test_rejects_reserved_channel():
    caps = Caps((Channel(2, frozenset(("gpio.in",)), reserved=True),))
    with pytest.raises(ValueError, match="reserved"):
        resolve(caps, ConnectionManifest((Connection("sense", 2),)),
                {"sense": "gpio.in"})


def test_resolves_all_uart_group_roles_or_nothing():
    caps = Caps((Channel(1, frozenset(("uart.rx",))), Channel(2, frozenset(("uart.tx",)))),
                (PeripheralGroup("uart0", "uart", frozenset(("rx", "tx"))),))
    manifest = ConnectionManifest((Connection("dut.tx", 1), Connection("dut.rx", 2)))
    plan = resolve_group(caps, manifest, "uart0", (
        RoleRequest("rx", "dut.tx", "uart.rx"),
        RoleRequest("tx", "dut.rx", "uart.tx"),
    ))
    assert [(item.role, item.channel) for item in plan.roles] == [("rx", 1), ("tx", 2)]
    with pytest.raises(ValueError, match="requires roles"):
        resolve_group(caps, manifest, "uart0", (
            RoleRequest("rx", "dut.tx", "uart.rx"),))


def test_rejects_exclusive_groups_before_allocation():
    caps = Caps((), (PeripheralGroup("uart0", "uart", frozenset(), frozenset(("capture0",))),
                     PeripheralGroup("capture0", "capture", frozenset())))
    with pytest.raises(ValueError, match="conflict"):
        resolve_plan(caps, ConnectionManifest(()), {"uart0": (), "capture0": ()})


def test_resolve_plan_is_immutable_and_keeps_probe_roles_separate():
    caps = Caps((Channel(1, frozenset(("uart.rx",))),),
                (PeripheralGroup("uart0", "uart", frozenset(("rx",))),))
    result = resolve_plan(
        caps, ConnectionManifest((Connection("console.tx", 1),)),
        {"uart0": (RoleRequest("rx", "console.tx", "uart.rx"),)})
    assert isinstance(result, ConfigurePlan)
    assert result.groups[0].roles[0].signal == "console.tx"
    assert result.groups[0].wire_id is None


def test_lease_registry_rejects_duplicates_and_use_after_release():
    caps = Caps((Channel(1, frozenset(("uart.rx",))),),
                (PeripheralGroup("uart0", "uart", frozenset(("rx",))),))
    plan = resolve_plan(
        caps, ConnectionManifest((Connection("console.tx", 1),)),
        {"uart0": (RoleRequest("rx", "console.tx", "uart.rx"),)})
    allocation = Allocation("lease-1", plan)
    registry = LeaseRegistry()
    registry.activate(allocation)
    assert registry.require("lease-1") == allocation
    with pytest.raises(ValueError, match="already active"):
        registry.activate(allocation)
    assert registry.release("lease-1") == allocation
    with pytest.raises(ValueError, match="not active"):
        registry.require("lease-1")


@pytest.mark.parametrize("caps, message", [
    (Caps((Channel(1, frozenset()), Channel(1, frozenset()))), "channel id"),
    (Caps((), (PeripheralGroup("uart0", "uart", frozenset()),
               PeripheralGroup("uart0", "uart", frozenset()))), "group id"),
    (Caps((), (PeripheralGroup("uart0", "uart", frozenset(),
                               frozenset(("missing",))),)), "unknown group"),
    (Caps((), (PeripheralGroup("uart0", "uart", frozenset(),
                               frozenset(("uart0",))),)), "excludes itself"),
])
def test_rejects_invalid_probe_caps(caps, message):
    with pytest.raises(ValueError, match=message):
        validate_caps(caps)


def test_rejects_unsupported_voltage_domain():
    caps = Caps(
        (Channel(1, frozenset(("gpio.in",)),
                 voltage_domains=frozenset(("3v3",))),),
        voltage_domains=(VoltageDomain("3v3", 3300, 3600),))
    manifest = ConnectionManifest((Connection("sense", 1, "5v"),))
    with pytest.raises(ValueError, match="voltage domain 5v"):
        resolve(caps, manifest, {"sense": "gpio.in"})


def test_rejects_unknown_or_input_only_voltage_domains():
    with pytest.raises(ValueError, match="unknown voltage domain"):
        validate_caps(Caps((Channel(
            1, frozenset(), voltage_domains=frozenset(("3v3",))),)))

    caps = Caps(
        (Channel(1, frozenset(("gpio.out",)),
                 voltage_domains=frozenset(("sense5v",))),),
        voltage_domains=(VoltageDomain("sense5v", 5000, 5500,
                                       can_drive=False),))
    with pytest.raises(ValueError, match="input-only"):
        resolve(caps,
                ConnectionManifest((Connection("drive", 1, "sense5v"),)),
                {"drive": "gpio.out"})
