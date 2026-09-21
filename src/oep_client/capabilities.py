"""MCU-independent capability planning for development probes."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Channel:
    id: int
    functions: frozenset[str]
    input_only: bool = False

@dataclass(frozen=True)
class PeripheralGroup:
    """One hardware peer instance and the roles it requires together."""
    id: str
    kind: str
    roles: frozenset[str]
    exclusive_with: frozenset[str] = frozenset()

@dataclass(frozen=True)
class Caps:
    channels: tuple[Channel, ...]
    groups: tuple[PeripheralGroup, ...] = ()

@dataclass(frozen=True)
class Connection:
    signal: str
    channel: int

@dataclass(frozen=True)
class ConnectionManifest:
    connections: tuple[Connection, ...]

def resolve(caps: Caps, manifest: ConnectionManifest,
            required: dict[str, str]) -> dict[str, int]:
    """Return an exact allocation; reject absent/ambiguous channel bindings."""
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
        if channel.input_only and function in {"gpio.out", "open_drain", "uart.tx", "i2c.sda", "i2c.scl", "spi.tx", "spi.sck", "spi.cs"}:
            raise ValueError(f"input-only channel {channel_id} cannot drive {function}")
        allocation[signal] = channel_id
    if len(set(allocation.values())) != len(allocation):
        raise ValueError("allocation aliases one probe channel to multiple signals")
    return allocation

def resolve_group(caps: Caps, manifest: ConnectionManifest, group_id: str,
                  signals: dict[str, str]) -> dict[str, int]:
    """Resolve every required role of one declared peripheral instance."""
    group = next((item for item in caps.groups if item.id == group_id), None)
    if group is None:
        raise ValueError(f"unknown peripheral group {group_id}")
    if set(signals) != set(group.roles):
        raise ValueError(f"group {group_id} requires roles {sorted(group.roles)}")
    return resolve(caps, manifest, signals)
