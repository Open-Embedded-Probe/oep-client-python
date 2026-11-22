# OEP Python client

[日本語](README.ja.md)

The host side of Open Embedded Probe: a Python library and the `oep` command. It connects to OEP probes, writes and debugs targets,
tests peripherals with the fixtures, and captures logic and analog waveforms. Following OEP's division of work, target knowledge lives
in the host: the CH32 flash controller, RAM loaders, the RP2350 boot ROM, the Cortex-M debug registers and so on live here. The probe
knows only the wires and the DMI / DP·AP transfers.

**Spec implemented: the oep-spec commit in `tests/vectors/SPEC_COMMIT`** (oep-spec versioning §2 / §6: before the freeze, revision 1
alone does not fix the shapes, so an implementation names the commit of the spec it implements). OEP itself is in
[oep-spec](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/README.md). The history of changes is in [CHANGELOG.md](CHANGELOG.md).

## Installing

```sh
pip install oep-client-python     # imported as oep_client
```

`oep-client` on PyPI is a different project, so the distribution name is `oep-client-python`. On Linux, opening the USB ports as an
ordinary user needs a udev rule ([User guide](docs/guide.md#usb-permissions-on-linux-udev)).

## A minimal example

```python
from oep_client import link, core, fixture

hst = link.open_host("oep://30eda0ea068b")   # the probe's unit id
try:
    core.take(hst, 30_000, owner="my-test")
    (led,) = core.channels_of(hst, ["GPIO10"])
    gpio = fixture.Gpio(hst)
    core.plan_apply(hst, [(gpio.fn, 1, led)])
    gpio.set([(led, gpio.OUTPUT_HIGH)])
finally:
    hst.end()
    hst.link.close()
```

```sh
oep dump --port oep://30eda0ea068b           # that probe's functions, pins and settings
oep dump --virtual p4-x035                   # a virtual bench probe, with no hardware
```

## Documents

| Document | What is in it |
|---|---|
| [User guide](docs/guide.md) | Installing, connecting, assigning pins, fixtures, capture, debugging and writing, settings |
| [The `oep` commands](docs/cli.md) | `oep dump` / `settings` / `pins` / `speed` / `linktest` and more, kept sessions, TCP |
| [Modules](docs/reference.md) | The public API (each `oep_client` module) |
| [Writing and debugging an SWD board](docs/swd.md) | RP2040 / RP2350 boards from the Arduino IDE and gdb through an OEP probe |
| [The pytest plugin](docs/pytest.md) | OEP probes from pytest-embedded-arduino-cli |
| [Virtual bench](docs/virtual-bench.md) | A probe and target that run with no hardware (tests, CI, developing other hosts) |
| [All documents](docs/README.md) | Every document, with hardware tests, conformance checks and the testing policy |
| [Contributing](CONTRIBUTING.md) | Tests, copying the number tables, releases |

## License

[LICENSE](LICENSE)
