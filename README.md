# OEP Python client

[日本語](README.ja.md)

The host side of Open Embedded Probe (OEP). It speaks the v1 protocol of
[oep-spec](https://github.com/Open-Embedded-Probe/oep-spec) (`docs/oep-core.ja.md` and the standard interfaces
`docs/oep-if-*.ja.md`, a candidate being settled). The wire numbers come from `oep_client.registry`, a verbatim copy of
oep-spec's generated `generated/oep-v1/oep_v1_registry.py`. This is an experimental stage: breaking changes are expected and
no compatible API is promised. For a map of the specification, start with oep-spec's `docs/review-guide.ja.md`.

It follows OEP's division of work: the knowledge of the target lives in the host. The probe knows only its wires and DMI /
DP-AP transfers; the CH32 flash controller, the RAM loader, the RP2350 boot ROM, the Cortex-M debug registers and so on are
here.

```sh
pip install oep-client-python     # PyPI (import oep_client); a checkout: pip install -e <checkout>
uv run pytest                     # in a checkout (the fake probe; no hardware)
OEP_HW_BOARDS=<board id> OEP_PROBE_DIR=<oep-probe-arduino checkout> uv run pytest tests/hw -m hw   # a real probe: tests/hw/README.md
```

`import oep_client` is all it takes. The registry is copied from oep-spec with `tools/sync_registry.sh`. PyPI's
`oep-client` is another project, so the distribution is named `oep-client-python`.

Releases: run the GitHub Actions workflow Release (workflow_dispatch, version X.Y.Z or X.Y.ZbN). `tools/prepare_release.py`
sets the version in pyproject.toml, uv.lock and `oep_client.__version__` and turns CHANGELOG.md's Unreleased into that version; after
the tests and the build it commits, tags, makes the GitHub Release and publishes to PyPI (Trusted Publishing). Record changes
under Unreleased in CHANGELOG.md, (EN) and (JA).

## Modules (`oep_client`)

The modules below are the public API; import them directly (`from oep_client import riscv`). Anything not listed, and
names starting with `_`, may change. A probe firmware and this client go together by version: OpenEmbeddedProbe X.Y.Z with
oep-client-python X.Y.Z (until the v1 freeze every release may break the wire; the versions move together).

| Module | Contents |
|---|---|
| `host` | requests and results, the session id and the lock, `call()` (raises unless it worked), pipelining, the errors (`OepError` / `Rejected` / `Failed`; `Expired` when the lease lapsed - the session is never re-opened behind the caller's back, `Host.epoch` moves) |
| `link` | transports: serial ports (always COBS + CRC as `0x00 <COBS> 0x00`, bytes outside frames skipped as noise, opened exclusively), USB vendor bulk / HID and TCP (length frames, the §5.1 resync); matching by corr and resending; `open_host(target)` |
| `core` | interfaces by name (cached), confirm (with the probe's `boot_id`), the probe's describe (declarations only, cached per boot: labels, the transport list, `max_op_ms`), taking the lock (`take`), the pin plan, the `Interface` base |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio` (scan, attach - `max_speed` always sent, `reset=(channel, hold_ms)` for an attach under reset -, detach, connections), `oep.target.riscv-dm` (answers count their values; `RunResult.not_halted`), finding the reset line, attach through GPIO |
| `console` | `oep.target.console` (position streams: read answers carry their length, marks are `time_ns`, the lock-free `streams()` list) and `ConsoleIO`, read as bytes |
| `fixture` | `oep.fixture.gpio` / `uart` (its stream is the plan's; `status()`) / `i2c-target` / `spi-target` (revision 1) |
| `config` | `oep.probe.config` (slots, binds, plan / label / idle / uart items, get / set / unset / save / erase; `describe()` = the declarations, `state()` = the live storage / slot / bind state; `hash_of(items)` = the probe's hash) |
| `capture` | `oep.fixture.logic` / `analog` / `capture-group` (revision 1, oep-spec oep-if-capture). Every start is a generation (`LogicCapture.generation`) that read and release name - `read_segment(segment)` does it by itself; `status()` returns a `Status`. Every segment read goes to the `Host.on_capture` callbacks as a `CaptureRecord` (the hook for run recorders; no wireskein dependency) |
| `decode` | decoding capture channels (I2C) |
| `registry` | generated from oep-spec's number table (never edited; copied again from oep-spec). The public way to reach an interface by name is `registry.INTERFACES[name]` (`.revision`, `.op`, `.tlv`, `.enum`, e.g. `INTERFACES["oep.fixture.uart"].enum["role"]`); the module-level names (`FIXTURE_UART`, ...) are the same objects |
| `arm` | `oep.wire.swd`, `oep.target.arm-adi`, MEM-AP, halting and calling functions on a Cortex-M |
| `ch32_flash` | writing a CH32 (a RAM loader, page by page) |
| `rp2350` | flash and reboot through the RP2350 boot ROM |
| `uiapduino` | into and out of the UIAPduino bootloader |
| `catalog` / `names` / `interfaces` / `dump` | the capability list and describe shapes, display |
| `fake` / `endpoint` / `fake_serial` / `fake_serve` | the fake probe (below) |

## Example

```python
from oep_client import core, link, riscv, ch32_flash

hst = link.open_host("/run/board-identify/by-id/esp32-series-30eda0e31108")   # pipelined
# a serial port (always COBS), "tcp://127.0.0.1:PORT" (a broker), "usb:<unit id>" (the probe whose USB serial it is),
# "usb" / "usb:303a:0002[:SERIAL]" (vendor, then HID)
core.take(hst, 30000, owner="flash script")   # the only way in: force; else wait out the lease, name the holder
wire = riscv.Wire(hst, "oep.wire.rvswd")
conn, _ = wire.attach(halt=True)
dm = riscv.RiscvDm(hst, conn)
dm.reset_halt()
result = ch32_flash.program(hst, dm, open("sketch.bin", "rb").read(), ch32_flash.PROFILES["x035"])
dm.reset(confirm=True)
wire.detach(conn)
hst.end()
```

## The `oep` command

```sh
oep dump --port <probe>                      # what the probe offers (--fake p4-x035: no hardware)
oep config show <probe>                      # the settings, the declarations and the live state
oep config state <probe>                     # the live slot / bind / storage state alone (lock-free, for polling)
oep config slot <probe> --name x035 --wire rvswd --pins 2,54 --attach at-boot --retry 1 --mechanism dmseq
oep config bind <probe> --port 1 --mode last-reset --stream slot:x035
oep config uart <probe> oep.fixture.uart 115200 --format 8N1   # applied whenever that UART's plan has RX or TX
oep config save <probe>                      # kept over a restart (also: remove = unset, erase)
oep speed <probe> 1500000,921600,500000      # port_speed: try faster rates on a UART bridge, print the report (below)
```

`<probe>` is a serial port, `tcp://HOST:PORT` or `usb[:VID:PID[:SERIAL]]`. A change takes the lock (owner "oep config") and
ends the session after it; it takes effect at once and, after `save`, stays over a restart.

A run on hardware: ArduinoCore-CH32's `tests/manual/oep_smoke/` (`oep_smoke.py`, `oep_probe_checks.py`).

## A faster UART bridge (port_speed, opt-in)

A probe whose describe declares `port_speed` (oep-core §3.5; the classic ESP32 reference firmware does) lets the host
run its UART bridge faster than the boot speed (115200) for one session. Nothing changes unless the host asks:

```python
hst = link.open_host("/dev/ttyUSB0", port_speed=[921600, 500000])    # takes the lock, keeps the session open
print(hst.link.speed.to_text())             # every candidate tried, the baseline, the flows, the one in force
# the full form (a baseline and the flows measured), in a session already taken:
report = link.raise_speed(hst, [921600, 500000], verify=True, flows=[("out", 2)], record=True)
```

The host's procedure is the oep-spec host guide §7 (core §3.5 is the handshake). The **minimal form** (the default, about
50 ms, no measurement): each candidate in order - `try` (answered at the speed now, then the probe switches) -> the host
switches to the requested baud -> 20 ms -> a `confirm` (100 ms, up to 3) -> `commit`. The **full form** (`verify=True`,
or `flows=` given): a baseline at the boot speed per flow (this session's frames, or 60 measured), then for each
candidate every flow the session will use - `flows` of `("in"|"out"|"duplex", n)` (in = link_source probe -> host, out =
link_sink host -> probe, duplex = both interleaved; `n` in flight, 0 = the most the link keeps) - 16 frames at
max_frame - 16, counting broken and lost and measuring KB/s; a flow fails on broken + lost >= 3 over max(2 x baseline,
5 %), runs once more at n = 1 first (then n = 1 is the link's cap), and one failed flow fails the candidate. A failed
candidate reverts (step 2) and goes back to the boot speed, confirmed there. The first candidate that passes is kept; a
rate the probe's UART cannot make is skipped. Which rates pass depends on the bridge chip and its driver (an FTDI took
only 3 MHz / n, a CH340 921600 but not 1500000 towards the host: oep-spec docs/uart-speed-negotiation.ja.md), so the
host chooses them. The report (`link.speed`: `base`, `rate`, `chosen`, `baseline`, `trials` of `SpeedTrial` with
`flows`, `in_kb_s` / `out_kb_s` / `duplex_kb_s`) is there to budget a capture or a write.

The probe goes back to the boot speed by itself when the session ends (`end`, a lapse, a force), when frames break or the
line goes quiet (`idle_ms`, at most `port_speed_idle_max_ms` = 3 s; a host that died leaves the rate no longer than
that): while raised the link sends a keepalive before a request when it has been quiet for less than half of `idle_ms`
(1 s), and `hst.link.keep_alive()` does the same for a caller that sits idle for long; opening a serial port retries its
first confirm for about 4 s to wait out a rate left over. The link follows an `end` or a revert at once, and a request
that goes unanswered at a raised rate (its resend too) takes the link back to the boot speed, confirmed, and goes once
more there - it never wedges; while raised each wait for an answer is at most a quarter of the lease, so this ends well
inside it. In use the link judges the frames of the last 3 s (none under 50): over max(2 x baseline, 10 %) broken or
lost steps the link down - port_speed revert at the raised rate, the boot speed, a confirm - and the rate is not raised
again in that session (`speed.stepped_down`, `speed.down_why`, `speed.step_downs`). `record=True` (a bool, a path, or a
`speed_record.SpeedRecord`; the `oep speed` CLI's default, off in the library) keeps passed / failed rates per (port,
unit_id) under `~/.cache/oep-client/link-speed.json` for 30 days, putting a passed rate first and leaving failed ones
out. A probe without the feature answers `not supported` and stays at its speed. Behind a broker (TCP) the broker does
this, not the client. Serial ports are also opened in the driver's low-latency mode where it has one (an FTDI's latency
timer 16 -> 1 ms tripled a UART bridge's throughput). `open_host(..., baud=)` names the boot speed when the board's
profile is not 115200.

## The fake probe (a working spec)

`endpoint.Endpoint` is a fake probe that answers as oep-spec says (2026-10-01: every answer carries the length of its
data or list, TLVs may follow anything, rejected `expired` after a lapse, one resource number space, describe = declarations
and `state` for the rest, capture generations); ch32rv, this client and the probe firmware are checked against it (when the
spec changes, this is brought in line before the firmware). `fake` holds example declarations (profiles
`p4-x035`, `esp32-v003`, `p4-bench` = a made-up jig with three slots and two seats, `rp2350-pins` = a wire whose pins the host
chooses), `fake_serial` the byte side of a serial port (COBS candidates, raw bytes and binds, held during a session and
resumed after it).

`fake_capture` is `oep.fixture.logic` (logic): one-shot, repeat (segments with the clock at the actual rate, a ring,
release) and streaming (data pushes while subscribed), level / edge triggers with a pretrigger, and events. What it
captures is known: sample i is the counter i, channel k its bit k (a square wave of period 2^(k+1) samples), in the layout
the profile allows (`p4-x035`: w 1-16 as the P4's PARLIO, three channels in w 4; `esp32-v003`: w 8 as the classic ESP32's
sampler, one-shot only). A capture only listens, so it may be planned on pins other interfaces hold. `p4-x035` also has
`oep.fixture.analog` (4 channels of the P4's ADC1 on GPIO16-23: an even channel k a square wave, an odd one a sine, of
period 64 (k // 2 + 1) samples, 12-bit values in 16-bit slots; ESP32-style frontends; a made-up two-point calibration and a
Vrefint) and `oep.fixture.capture-group` binding the logic and the analog: started together, the analog 5 us later
(+-2 us), the trigger of one marked on both. Times are ns on the probe's one clock.

`oep.fixture.i2c-target` and `oep.fixture.spi-target` (`p4-x035`, `esp32-v003`) take their roles from the plan (SDA / SCL;
SCK / MOSI / MISO / CS - on `esp32-v003` one of two fixed channel_groups exactly) and answer every op of fixture §3 / §4.
There is no bus controller: an arm stays armed and nothing is received until a test calls a hook on the endpoint
(`i2c_write` / `i2c_read` / `spi_transfer`: one transaction on the bus).

Other programs' tests run `fake_serve` as a child process:

```sh
python -m oep_client.fake_serve --pty --profile p4-bench --slot x035 --bind last-reset \
    --console 'uptime %d\r\n' --every 100
# first line: PTY /dev/pts/N (PORT n with --tcp 0); it ends when stdin closes
```

The pty is a serial port (the host opens it with TIOCEXCL); `--tcp PORT` is `--framing cobs` (a serial port) or
`--framing length` (the vendor bulk / TCP form). Faults: `--drop N` (the N-th answer is not sent, once; the request did run,
so a resend gets the remembered result), `--noise TEXT` (noise before every answer), `--corrupt N` (the N-th answer's CRC
broken once). `--uart-plan` / `--uart-rx` give the first fixture UART a plan and RX bytes, `--run-hook` a host's own model of
riscv-dm run, `--capture-slipped` flags bit2 on every capture segment. port_speed: `esp32-v003` has it
(`--no-port-speed` turns it off), and `--broken-rate RATE[:MIN_SIZE][:in|out]` makes a rate break frames (in process,
`fake_serial.FakeSerialStream` also garbles everything while the host's own rate differs from the probe's). Events and data pushes go out on the pty and on TCP
(both framings). The rest: `--help`.
