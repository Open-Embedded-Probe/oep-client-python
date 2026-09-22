"""Host-side decoders for fixture.capture sample streams (one byte per sample, bit k = line k)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class I2cEvent:
    kind: str          # "start", "byte", "stop"
    sample: int        # sample index where the event was recognised
    value: int = 0     # byte value for "byte"
    ack: bool | None = None  # True = ACK (SDA low on the 9th clock)


@dataclass
class I2cTrace:
    events: list[I2cEvent] = field(default_factory=list)
    scl_periods: list[int] = field(default_factory=list)  # samples between SCL rising edges

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


def decode_i2c(samples: bytes, scl_bit: int, sda_bit: int) -> I2cTrace:
    """Edge-based I2C decode: START = SDA falling while SCL high, STOP = SDA rising while
    SCL high, data sampled on SCL rising edges, every 9th bit is the ACK."""
    trace = I2cTrace()
    if not samples:
        return trace
    scl = lambda b: (b >> scl_bit) & 1  # noqa: E731
    sda = lambda b: (b >> sda_bit) & 1  # noqa: E731
    prev = samples[0]
    bits: list[int] = []
    in_frame = False
    last_rise = None
    for i in range(1, len(samples)):
        cur = samples[i]
        if scl(cur) and scl(prev):
            if sda(prev) and not sda(cur):
                trace.events.append(I2cEvent("start", i)); in_frame = True; bits = []
            elif not sda(prev) and sda(cur):
                trace.events.append(I2cEvent("stop", i)); in_frame = False; bits = []
        if scl(cur) and not scl(prev):  # SCL rising edge: sample SDA
            if last_rise is not None:
                trace.scl_periods.append(i - last_rise)
            last_rise = i
            if in_frame:
                bits.append(sda(cur))
                if len(bits) == 9:
                    value = 0
                    for b in bits[:8]:
                        value = (value << 1) | b
                    trace.events.append(I2cEvent("byte", i, value, ack=bits[8] == 0))
                    bits = []
        prev = cur
    return trace
