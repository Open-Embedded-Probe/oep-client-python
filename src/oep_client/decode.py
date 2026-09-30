"""Host-side decoders for capture channels (oep.fixture.logic: `LogicCapture.channel(data, k)` gives one value
per sample)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class I2cEvent:
    kind: str                 # "start", "byte", "stop"
    sample: int               # sample index where the event was recognised
    value: int = 0            # byte value for "byte"
    ack: bool | None = None   # True = ACK (SDA low on the 9th clock)


@dataclass
class I2cTrace:
    events: list[I2cEvent] = field(default_factory=list)
    scl_periods: list[int] = field(default_factory=list)   # samples between SCL rising edges

    def bytes(self) -> list[tuple[int, bool]]:
        return [(e.value, bool(e.ack)) for e in self.events if e.kind == "byte"]

    def summary(self) -> str:
        parts = []
        for e in self.events:
            if e.kind == "byte":
                parts.append(f"{e.value:02x}{'A' if e.ack else 'N'}")
            else:
                parts.append("S" if e.kind == "start" else "P")
        return " ".join(parts)


def decode_i2c(scl: list[int], sda: list[int]) -> I2cTrace:
    """Edge-based I2C decode of two channels: START = SDA falling while SCL high, STOP = SDA rising while SCL high,
    data sampled on SCL rising edges, every 9th bit is the ACK."""
    trace = I2cTrace()
    n = min(len(scl), len(sda))
    if not n:
        return trace
    bits: list[int] = []
    in_frame = False
    last_rise = None
    for i in range(1, n):
        if scl[i] and scl[i - 1]:
            if sda[i - 1] and not sda[i]:
                trace.events.append(I2cEvent("start", i))
                in_frame, bits = True, []
            elif not sda[i - 1] and sda[i]:
                trace.events.append(I2cEvent("stop", i))
                in_frame, bits = False, []
        if scl[i] and not scl[i - 1]:   # SCL rising edge: sample SDA
            if last_rise is not None:
                trace.scl_periods.append(i - last_rise)
            last_rise = i
            if in_frame:
                bits.append(sda[i])
                if len(bits) == 9:
                    value = 0
                    for b in bits[:8]:
                        value = (value << 1) | b
                    trace.events.append(I2cEvent("byte", i, value, ack=bits[8] == 0))
                    bits = []
    return trace
