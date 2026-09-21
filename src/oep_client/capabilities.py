"""MCU-independent capability planning for development probes."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Channel:
    id: int
    functions: frozenset[str]
    input_only: bool = False
    voltage_domains: frozenset[str] = frozenset()
    reserved: bool = False

@dataclass(frozen=True)
class PeripheralGroup:
    """One hardware peer instance and the roles it requires together."""
    id: str
    kind: str
    roles: frozenset[str]
    exclusive_with: frozenset[str] = frozenset()
    wire_id: int | None = None
    instance: int = 0


@dataclass(frozen=True)
class VoltageDomain:
    id: str
    nominal_mv: int
    input_max_mv: int
    can_drive: bool = True


@dataclass(frozen=True)
class Caps:
    channels: tuple[Channel, ...]
    groups: tuple[PeripheralGroup, ...] = ()
    voltage_domains: tuple[VoltageDomain, ...] = ()

@dataclass(frozen=True)
class Connection:
    signal: str
    channel: int
    voltage_domain: str | None = None

@dataclass(frozen=True)
class ConnectionManifest:
    connections: tuple[Connection, ...]


@dataclass(frozen=True)
class RoleRequest:
    role: str
    signal: str
    function: str


@dataclass(frozen=True)
class RoleAllocation:
    role: str
    signal: str
    function: str
    channel: int


@dataclass(frozen=True)
class GroupPlan:
    group_id: str
    kind: str
    roles: tuple[RoleAllocation, ...]
    wire_id: int | None = None


@dataclass(frozen=True)
class ConfigurePlan:
    groups: tuple[GroupPlan, ...]


@dataclass(frozen=True)
class Allocation:
    """Effective probe allocation returned after an atomic configure."""
    lease_id: str
    plan: ConfigurePlan


class LeaseRegistry:
    """Track effective allocations returned by a probe session."""

    def __init__(self) -> None:
        self._active: dict[str, Allocation] = {}

    @staticmethod
    def _channels(allocation: Allocation) -> set[int]:
        return {role.channel for group in allocation.plan.groups
                for role in group.roles}

    def activate(self, allocation: Allocation) -> None:
        if not allocation.lease_id:
            raise ValueError("probe returned an empty lease id")
        if allocation.lease_id in self._active:
            raise ValueError(f"lease is already active: {allocation.lease_id}")
        requested = self._channels(allocation)
        for current in self._active.values():
            if requested & self._channels(current):
                raise ValueError("active leases share a probe channel")
        self._active[allocation.lease_id] = allocation

    def require(self, lease_id: str) -> Allocation:
        try:
            return self._active[lease_id]
        except KeyError as error:
            raise ValueError(f"lease is not active: {lease_id}") from error

    def release(self, lease_id: str) -> Allocation:
        allocation = self.require(lease_id)
        del self._active[lease_id]
        return allocation


def validate_caps(caps: Caps) -> None:
    """Reject ambiguous or internally inconsistent probe declarations."""
    channel_ids = [channel.id for channel in caps.channels]
    if len(set(channel_ids)) != len(channel_ids):
        raise ValueError("probe caps repeat a channel id")
    if any(channel_id < 0 for channel_id in channel_ids):
        raise ValueError("probe channel ids must be non-negative")

    domain_ids = [domain.id for domain in caps.voltage_domains]
    if len(set(domain_ids)) != len(domain_ids):
        raise ValueError("probe caps repeat a voltage domain id")
    for domain in caps.voltage_domains:
        if not domain.id:
            raise ValueError("probe voltage domain id must not be empty")
        if domain.nominal_mv <= 0 or domain.input_max_mv < domain.nominal_mv:
            raise ValueError(f"invalid voltage range for domain {domain.id}")
    declared_domains = set(domain_ids)
    for channel in caps.channels:
        unknown = channel.voltage_domains - declared_domains
        if unknown:
            raise ValueError(
                f"channel {channel.id} references unknown voltage domain "
                f"{sorted(unknown)[0]}")

    group_ids = [group.id for group in caps.groups]
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("probe caps repeat a peripheral group id")
    declared = set(group_ids)
    for group in caps.groups:
        if group.id in group.exclusive_with:
            raise ValueError(f"peripheral group {group.id} excludes itself")
        unknown = group.exclusive_with - declared
        if unknown:
            raise ValueError(
                f"peripheral group {group.id} excludes unknown group "
                f"{sorted(unknown)[0]}")

def resolve(caps: Caps, manifest: ConnectionManifest,
            required: dict[str, str]) -> dict[str, int]:
    """Return an exact allocation; reject absent/ambiguous channel bindings."""
    validate_caps(caps)
    by_id = {channel.id: channel for channel in caps.channels}
    bound = {item.signal: item.channel for item in manifest.connections}
    if len(bound) != len(manifest.connections):
        raise ValueError("connection manifest repeats a logical signal")
    allocation = {}
    for signal, function in required.items():
        channel_id = bound.get(signal)
        if channel_id is None:
            raise ValueError(f"no physical connection declared for {signal}")
        channel = by_id.get(channel_id)
        if channel is None or function not in channel.functions:
            raise ValueError(f"channel {channel_id} cannot provide {function}")
        if channel.reserved:
            raise ValueError(f"channel {channel_id} is reserved")
        if channel.input_only and function in {"gpio.out", "open_drain", "uart.tx", "i2c.sda", "i2c.scl", "spi.tx", "spi.sck", "spi.cs"}:
            raise ValueError(f"input-only channel {channel_id} cannot drive {function}")
        declared = next(item for item in manifest.connections if item.signal == signal)
        if declared.voltage_domain and declared.voltage_domain not in channel.voltage_domains:
            raise ValueError(f"channel {channel_id} does not support voltage domain {declared.voltage_domain}")
        if declared.voltage_domain and function in {"gpio.out", "open_drain", "uart.tx", "i2c.sda", "i2c.scl", "spi.tx", "spi.sck", "spi.cs"}:
            domain = next(item for item in caps.voltage_domains
                          if item.id == declared.voltage_domain)
            if not domain.can_drive:
                raise ValueError(f"voltage domain {domain.id} is input-only")
        allocation[signal] = channel_id
    if len(set(allocation.values())) != len(allocation):
        raise ValueError("allocation aliases one probe channel to multiple signals")
    return allocation

def resolve_group(caps: Caps, manifest: ConnectionManifest, group_id: str,
                  requests: tuple[RoleRequest, ...]) -> GroupPlan:
    """Resolve every required role of one declared peripheral instance."""
    group = next((item for item in caps.groups if item.id == group_id), None)
    if group is None:
        raise ValueError(f"unknown peripheral group {group_id}")
    if {item.role for item in requests} != set(group.roles):
        raise ValueError(f"group {group_id} requires roles {sorted(group.roles)}")
    if len({item.role for item in requests}) != len(requests):
        raise ValueError(f"group {group_id} repeats a role")
    resolved = resolve(caps, manifest,
                       {item.signal: item.function for item in requests})
    return GroupPlan(group.id, group.kind, tuple(
        RoleAllocation(item.role, item.signal, item.function,
                       resolved[item.signal]) for item in requests),
        group.wire_id)

def resolve_plan(caps: Caps, manifest: ConnectionManifest,
                 groups: dict[str, tuple[RoleRequest, ...]]) -> ConfigurePlan:
    """Resolve a complete peripheral plan, rejecting declared exclusions."""
    validate_caps(caps)
    declared = {item.id: item for item in caps.groups}
    selected = set(groups)
    if not selected <= set(declared):
        raise ValueError("plan requests an unknown peripheral group")
    for group_id in selected:
        conflict = selected & declared[group_id].exclusive_with
        if conflict:
            raise ValueError(f"peripheral groups conflict: {group_id} and {sorted(conflict)[0]}")
    allocation = tuple(resolve_group(caps, manifest, group_id, requests)
                       for group_id, requests in groups.items())
    used = [role.channel for group in allocation for role in group.roles]
    if len(set(used)) != len(used):
        raise ValueError("peripheral groups share a probe channel")
    return ConfigurePlan(allocation)
