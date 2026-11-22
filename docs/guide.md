# User guide

[日本語](guide.ja.md)

How to use OEP probes from Python: connect to a probe, assign its pins, drive a target with the fixtures, capture waveforms,
and write and debug a target. Every example also runs on the virtual bench, with no hardware ([Virtual bench](virtual-bench.md)).
Each module in detail is in [Modules](reference.md), and the `oep` commands in [The `oep` commands](cli.md).

## Installing

```sh
pip install oep-client-python        # imported as oep_client
```

`oep-client` on PyPI is a different project. Where USB vendor bulk cannot be used, the HID or CDC port gives the same functions.

### USB permissions on Linux (udev)

A probe enumerates with the project's USB VID:PID `1209:4F45` (transports §3). Opening its vendor bulk (libusb) or HID (hidraw) as an
ordinary user needs a udev rule. [`udev/70-oep-probe.rules`](../udev/70-oep-probe.rules) (in a checkout and the sdist) lets the
logged-in user (`uaccess`) and the `plugdev` group open the USB device and its hidraw. Installing it needs administrator rights:

```sh
sudo install -m 0644 udev/70-oep-probe.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # or unplug and replug the probe
```

The probe's CDC port (`/dev/ttyACM*`) needs no rule beyond the usual `dialout` group. Windows and macOS need no rule.

## Connecting to a probe

```python
from oep_client import link, core

hst = link.open_host("oep://30eda0ea068b")   # the probe's unit id: looked for on USB, then on the network (DNS-SD)
try:
    core.take(hst, 30_000, owner="my-test")  # a session and the lock (a 30 s lease, extended while in use)
    print(core.unit_id(hst), core.max_op_ms(hst))
finally:
    hst.end()                                # gives back the lock and the assigned pins
    hst.link.close()
```

What `open_host` takes:

| Form | Meaning |
|---|---|
| `oep://<unit id>` | That probe: looked for on USB, then on the network. After opening, describe's unit id must be the one named |
| `oep://<unit id>?via=usb` / `bulk` / `hid` / `cdc` / `net` | Chooses the route |
| `oep://<unit id>?via=tty:/dev/ttyUSB0` | A probe behind a USB-UART bridge chip (a classic ESP32 and so on) |
| `/dev/ttyACM0`, `COM3` | A serial port (the unit id is not checked) |
| `tcp://HOST:PORT` | A TCP probe, or a broker |
| `usb` | The probe with the project's VID:PID, when there is exactly one |

Only one session holds the lock. When another host holds it, `core.take` waits for its lease to run out, and is refused with the
holder's name (`owner`) when it cannot wait that long.

## Finding probes

```sh
oep find                         # probes on the network (DNS-SD _oep._tcp: unit id, address, port)
oep dump --port oep://<unit id>  # that probe's functions, pins and settings
```

A probe on USB opens as `oep://<unit id>` as it is (the unit id is its USB serial number). From Python,
`discovery.browse(timeout=2.0)` lists the probes on the network and `discovery.find_unit(unit_id)` looks for one.

## Naming and assigning pins

A probe's pins are handled as channel numbers. The numbers are fixed per boot, so write names in scripts and turn them into numbers
with `core.channels_of`. A name is the probe's channel name (`GPIO10`) or a line label given in the settings (`x035.swdio`,
`x035@pb9`; [The `oep` commands](cli.md)).

Before a fixture is used, its pins are assigned with `core.plan_apply`. An assignment is a list of `(fn, role, channel)`, made by each
class's `assignments(...)`. Pins another interface holds are refused (`core.PinsTaken` names the holder). The assignment belongs to
the session, and is given back by `core.plan_release` or when the session ends.

```python
from oep_client import fixture

a, b = core.channels_of(hst, ["GPIO10", "GPIO11"])
gpio = fixture.Gpio(hst)
core.plan_apply(hst, [(gpio.fn, 1, a), (gpio.fn, 1, b)])
gpio.set([(a, gpio.OUTPUT_HIGH), (b, gpio.INPUT_PULLDOWN)])   # applied in order in one request
print(gpio.read([a, b]))                                      # [1, 0]
core.plan_release(hst)
```

A target's reset line is moved with the open-drain modes (`gpio.OPEN_DRAIN_LOW` / `OPEN_DRAIN_RELEASE`, `gpio.pulse_low(ch)`).
The open-drain modes never drive a line high.

## Driving a target with the fixtures

The fixtures are the counterparts for testing a target's peripherals. Each is used after its pins are assigned.

| Class | What it does |
|---|---|
| `fixture.Gpio` | Makes lines outputs or inputs and reads levels |
| `fixture.FixtureUart` / `FixtureUartIO` | The counterpart of a target's UART (`configure(baud)`, streams to read and write) |
| `fixture.I2cController` / `SpiController` | The probe as the controller, testing a target's I2C / SPI target |
| `fixture.I2cTarget` / `SpiTarget` | The probe as the target, testing a target's I2C / SPI controller |
| `fixture.Pwm` / `Counter` | Sends a square wave; counts edges to measure a frequency |
| `fixture.Dac` | Sends a voltage |

```python
# 1 kHz from the PWM, counted on the same pin by the counter
pwm, cnt = fixture.Pwm(hst), fixture.Counter(hst)
(out,) = core.channels_of(hst, ["GPIO12"])
core.plan_apply(hst, pwm.assignments(out) + cnt.assignments(out))
pwm.start(0, 1000)                    # 1000 Hz on output 0, 50 % duty; the probe answers with what it makes
print(cnt.measure(100_000).hz(0))     # counted for 0.1 s -> 1000.0
core.plan_release(hst)

# the I2C controller writes 1 byte to address 0x50, then reads 4 bytes
i2c = fixture.I2cController(hst)
sda, scl = core.channels_of(hst, ["GPIO20", "GPIO21"])
core.plan_apply(hst, i2c.assignments(sda, scl))
i2c.configure(100_000, 10_000)        # SCL 100 kHz, waiting up to 10 ms on a stretch -> the SCL used
t = i2c.transfer([fixture.I2cSegment.write(0x50, b"\x00"), fixture.I2cSegment.reading(0x50, 4)])
print(t.ok, t.data)
core.plan_release(hst)
```

The limits of speeds and lengths are declared by the probe in describe (`i2c.max_clock_hz`, `i2c.max_length` and so on). A value
outside them is refused with `host.Unsupported`.

## Capture

`acquire` puts a logic and analog capture together in one call. Write the lines and the rate in an `acquire.Request`, and receive the
values the probe really used and the data in a `Result`.

```python
from oep_client import acquire

req = acquire.Request([("A", "GPIO10"), ("B", "GPIO11")], rate_hz=20_000_000, samples=2000,
                      trigger=("B", "rise"), pretrigger=100)
res = acquire.capture_at("oep://30eda0ea068b", req)   # opens the probe, captures, closes it
print(res.tick_hz, res.meta["trigger_tick"], res.complete)
for ch in res.channels:
    print(ch.name, ch.n)                               # a logic line is Bits (packed bits)
```

- `capture_at(address, req)` opens and closes the probe. `capture(hst, req)` captures on a session the caller opened.
- `mode="stream"` streams (for `duration_s` seconds, or until the `during=` function returns). `mode="repeat"` takes one part per trigger.
- Analog lines are added with `analog=[("V", "GPIO16", None)]` and `analog_rate_hz`. Logic and analog starting together use the
  probe's capture-group.
- Lost data shows in `res.lost`. A stream that loses data stops there by default (`on_loss="continue"` takes it to the end).
- A long stream is written to disk as it comes with `spool_dir=`.

The limits of capture rates and channel counts differ by probe; `oep dump` shows describe's values.

## Debugging and writing

Target knowledge lives in the host. The probe handles only the wires and the DMI / DP·AP transfers; how to write a flash and so on
lives in this client.

```python
from oep_client import riscv, ch32_flash

wire = riscv.Wire(hst, "oep.wire.rvswd")       # "oep.wire.swio" for SWIO chips
conn, _ = wire.attach(halt=True)               # attach halted
dm = riscv.RiscvDm(hst, conn)
print(hex(dm.read_register(0x7b1)))            # dpc
print(dm.read_block(0x20000000, 4).hex())      # read 4 words
dm.reset_halt()
ch32_flash.program(hst, dm, open("sketch.bin", "rb").read(), ch32_flash.PROFILES["x035"])
dm.reset()                                     # run
wire.detach(conn)
```

- Each target family's wire, reset and speeds are in `targets.FAMILIES`. Which chip uses which wire is in
  [oep-probe-arduino's debug targets](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/docs/guide/debug-targets.md).
- Arm (SWD) chips use `arm`. Writing and debugging RP2040 / RP2350 boards from the Arduino IDE and gdb is in
  [Writing and debugging an SWD board](swd.md).
- The target console (input and output over the debug wire) is `console`.

## Settings

A probe's settings (pin names and labels, pins not to use, the network) are read and written with `oep settings` or
`settings.Settings`.

```sh
oep settings show oep://<unit id>
oep settings set oep://<unit id> core channel.GPIO23.label=v003.nrst --save
```

```python
from oep_client import settings
s = settings.Settings(hst)
for category, name, value in s.show():
    print(category, name, value)
```

Writing the settings to a file and reading them back is `export()` / `plan_import(doc)` / `apply_import(plan)` (oep-spec's settings
file).

## Memo

A host can keep named data on a probe (wiring records and so on). The probe itself never reads it.

```python
from oep_client import memo
mm = memo.Memo(hst)
mm.write("my-bench/port/x035", b"...")
print(mm.read("my-bench/port/x035"), mm.list("my-bench/"))
```

## Calling an interface a sketch added

An interface added in your own sketch (`oep::SimpleInterface` and so on) is called by name and fields with `call`.

```python
from oep_client.call import call
r = call(hst, "io.github.yourname.mine", "add", a=2, b=3)
print(r["sum"])
```

The op names and fields are the ones the sketch declares in describe.

## The probe's log

Lines a sketch wrote with `OepProbeLog.h` are read by `probe_log`, with the probe's clock. `sample_of` says which sample of a capture
taken on the same clock a line falls at.

## Using it from tests

- From pytest-embedded-arduino-cli: [the pytest plugin](pytest.md)
- Hardware integration tests: [tests/hw](../tests/hw/README.md)
- How projects that use OEP build their tests: [guide for projects that use OEP](testing-consumers.md)
