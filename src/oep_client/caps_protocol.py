"""Paged OEP revision-1 capability discovery."""
import struct

from .capabilities import Caps, Channel, PeripheralGroup, VoltageDomain, validate_caps
from .prototype import Endpoint, FunctionResult, ProtocolError

FUNCTION_PROBE_CAPS = 0x0002
CAPS_GET_SUMMARY = 0x01
CAPS_GET_CHANNEL = 0x02
CAPS_GET_GROUP = 0x03
CAPS_GET_VOLTAGE_DOMAIN = 0x04

FUNCTION_NAMES = (
    "gpio.in", "gpio.out", "open_drain", "pull_up", "pull_down",
    "capture", "uart.rx", "uart.tx", "i2c.sda", "i2c.scl",
    "spi.rx", "spi.tx", "spi.sck", "spi.cs", "pwm.out",
    "analog.in", "analog.out", "edge.out",
)
GROUP_KINDS = {
    1: "uart",
    2: "i2c_controller",
    3: "i2c_target",
    4: "spi_controller",
    5: "spi_target",
    6: "capture",
    7: "pwm",
    8: "analog",
}


def _names(mask: int) -> frozenset[str]:
    unknown = mask >> len(FUNCTION_NAMES)
    if unknown:
        raise ProtocolError(f"capability mask has unknown bits: 0x{unknown:x}")
    return frozenset(name for bit, name in enumerate(FUNCTION_NAMES)
                     if mask & (1 << bit))


def caps_to_dict(caps: Caps) -> dict:
    """Stable JSON-friendly representation for CLI/artifact use."""
    return {
        "channels": [
            {"id": channel.id, "functions": sorted(channel.functions),
             "input_only": channel.input_only, "reserved": channel.reserved,
             "voltage_domains": sorted(channel.voltage_domains)}
            for channel in caps.channels],
        "groups": [
            {"id": group.id, "kind": group.kind,
             "roles": sorted(group.roles),
             "exclusive_with": sorted(group.exclusive_with),
             "wire_id": group.wire_id, "instance": group.instance}
            for group in caps.groups],
        "voltage_domains": [
            {"id": domain.id, "nominal_mv": domain.nominal_mv,
             "input_max_mv": domain.input_max_mv,
             "can_drive": domain.can_drive}
            for domain in caps.voltage_domains],
    }


class ProbeCapsClient:
    def __init__(self, endpoint: Endpoint, connection) -> None:
        self._endpoint = endpoint
        self._connection = connection

    def _exchange(self, operation: int, payload: bytes = b"") -> FunctionResult:
        correlation, request = self._endpoint.function_request(
            FUNCTION_PROBE_CAPS, operation, payload)
        return self._endpoint.parse_function_result(
            self._connection.exchange(request), correlation,
            FUNCTION_PROBE_CAPS)

    def get_caps(self) -> Caps | FunctionResult:
        summary = self._exchange(CAPS_GET_SUMMARY)
        if not summary.succeeded:
            return summary
        if len(summary.data) != 4:
            raise ProtocolError("malformed capability summary")
        revision, channel_count, group_count, domain_count = summary.data
        if revision != 1 or group_count > 64 or domain_count > 8:
            raise ProtocolError("unsupported capability summary")

        domains = []
        for ordinal in range(domain_count):
            result = self._exchange(CAPS_GET_VOLTAGE_DOMAIN,
                                    bytes((ordinal,)))
            if not result.succeeded:
                return result
            if len(result.data) != 6:
                raise ProtocolError("malformed voltage-domain capability")
            identifier, flags, nominal_mv, input_max_mv = struct.unpack(
                "<BBHH", result.data)
            domains.append(VoltageDomain(
                f"domain:{identifier}", nominal_mv, input_max_mv,
                can_drive=bool(flags & 1)))

        channels = []
        for ordinal in range(channel_count):
            result = self._exchange(CAPS_GET_CHANNEL, bytes((ordinal,)))
            if not result.succeeded:
                return result
            if len(result.data) != 12:
                raise ProtocolError("malformed channel capability")
            identifier, flags, domain_mask, function_mask = struct.unpack(
                "<HBBQ", result.data)
            if domain_mask >> domain_count:
                raise ProtocolError("channel references unknown voltage domain")
            channels.append(Channel(
                identifier, _names(function_mask),
                input_only=bool(flags & 1),
                voltage_domains=frozenset(
                    domains[index].id for index in range(domain_count)
                    if domain_mask & (1 << index)),
                reserved=bool(flags & 2)))

        raw_groups = []
        for ordinal in range(group_count):
            result = self._exchange(CAPS_GET_GROUP, bytes((ordinal,)))
            if not result.succeeded:
                return result
            if len(result.data) != 20:
                raise ProtocolError("malformed peripheral-group capability")
            identifier, kind_id, instance, role_mask, exclusive = struct.unpack(
                "<HBBQQ", result.data)
            kind = GROUP_KINDS.get(kind_id)
            if kind is None or exclusive >> group_count:
                raise ProtocolError("invalid peripheral-group capability")
            functions = _names(role_mask)
            raw_groups.append((identifier, f"{kind}{instance}", kind,
                               frozenset(name.rsplit(".", 1)[-1]
                                         for name in functions), exclusive))

        groups = tuple(PeripheralGroup(
            group_id, kind, roles,
            frozenset(raw_groups[index][1] for index in range(group_count)
                      if exclusive & (1 << index)), wire_id=identifier,
            instance=int(group_id.removeprefix(kind)))
            for identifier, group_id, kind, roles, exclusive in raw_groups)
        caps = Caps(tuple(channels), groups, tuple(domains))
        validate_caps(caps)
        return caps
