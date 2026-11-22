# Hardware integration tests (`tests/hw`)

[日本語](README.ja.md)

These are the steps of the Python hardware harness. For the common responsibilities and what is guaranteed, see the [overall testing policy](../../docs/testing-policy.md); for how any project that uses OEP proceeds, see the [consumer guide](../../docs/testing-consumers.md). Verifying probe updates is judged by the firmware owner, and client behavior by the client side. The two releases are not tied together uniformly.

Normal `uv run pytest` runs board-free checks such as the virtual bench. The hardware checks in this directory skip unless `OEP_HW_BOARDS` is given. Equipment TOML is used with `oep-hardware preflight` and `pytest tests/equipment` for standalone probe checks ([steps](../../docs/hardware-quickstart.md)). `.env.example`, `hardware.example.toml` and `hardware.resolved.example.toml` are templates of the [settings format](../../docs/hardware-schema.md); the hardware tests in this directory do not read them.

Run the entry points from the repository root. Replace `PROBE_ID` with an explicit ID that `boards.py` accepts. Commands other than the virtual example operate hardware.

```sh
# Use the installed firmware. This skips firmware writing; it is not a read-only test.
OEP_HW_BOARDS="$PROBE_ID" OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw

# When the probe owner verifies with an explicit candidate checkout and update target.
OEP_HW_BOARDS="$PROBE_ID" OEP_PROBE_DIR=/path/to/oep-probe-arduino uv run pytest tests/hw -m hw

# When the probe owner verifies an update with an explicit release image.
OEP_HW_BOARDS="$PROBE_ID" OEP_PROBE_VERSION="$PROBE_VERSION" uv run pytest tests/hw -m hw

# Check the harness steps in the virtual environment. Separate from hardware quality assurance.
OEP_HW_BOARDS=virtual-esp32-v003 uv run pytest tests/hw -m hw
```

`OEP_HW_NOFLASH=1` skips firmware writing and records the firmware string used. The config test changes, saves and restores settings, and the Wi-Fi test saves the given network settings. Check the side effects and end states below, and coordinate the use of shared resources with the equipment administrator. This harness does not take the `OEP_HW_LOCK` lock.

Several boards, `OEP_HW_BOARDS=a,b`, are processed one board at a time in order. Tests of a candidate source and of installed firmware are kept as separate results, and compatibility is not judged by the version string used alone. A skip of a required contract means the release check is incomplete.

## Files

| File | Content |
|---|---|
| `boards.py` | The board table. Keyed by the board-identify id (for a USB probe without one, the unit id): kind, sketch.yaml profile, how to reach the OEP port after flashing, expected model, free channels used for tests |
| `firmware.py` | Where the firmware comes from: `OEP_PROBE_DIR` (`arduino-cli compile --profile <profile> --output-dir ...`) or `OEP_PROBE_VERSION` (`firmware-<ver>.json` and the images of the GitHub release, sha256 checked, urllib only) |
| `flash.py` | How to flash each kind of board, and the DTR / RTS reset of a bridge board |
| `record.py` | One board's run: the connection, what was measured, the results file |
| `test_probe.py` | The tests. They run in this order |
| `test_harness.py` | Checks the harness's own cleanup on the in-process virtual bench (it has no `hw` mark, so it runs in the normal `uv run pytest`): settings are put back after a failed test and after the probe went away |
| `conftest.py` | The `hw` marker and the skip without `OEP_HW_BOARDS`, grouping per board, the one-screen summary |
| `results/` | `<board>-<firmware>-<client>-<started>.json`. One per run. The name has the run's start time (`20261006T203121`), so a later run does not overwrite an earlier run's file (small summaries only; everything else is kept out by `.gitignore`) |

## What each test checks

The tests after `flash` skip when the firmware could not be flashed, and when the probe went away and does not answer again. Each writes what it measured to the results file, and `verdict` is pytest's
result.

| Test | What it checks | Record |
|---|---|---|
| `flash` | The image goes on (esptool / DFU / picotool). It boots within 45 seconds and answers confirm | The firmware before and after flashing (describe), the flashing command, seconds, the last few lines |
| `identity` | confirm's revision ≥ 1. list has no fn 0 entry (the core has no name, core §7.2). clock's (core §7.6) boot_id is the same as confirm's. describe's model is as in the table. Flashing changed the boot_id. The firmware string equals the version flashed (`library.properties` for a local build, that version for a release) | boot_id, firmware, model, unit_id, chip, limits, the interface list, clock's uptime_ns and the shortest round trip of 4 |
| `required` | Of what every probe must provide, what can be checked without the lock (core §1.2, §7.1, §7.5; oep-spec `docs/conformance.md` section 1). The same list `oep dump` prints as MISSING: confirm's transport TLV (pointing to one of the transports of fn 0's describe, or 0xFF), and fn 0 describe's unit_id, transport and max_op_ms. It fails naming what is missing | The list (empty if nothing is missing) |
| `config` | The core's settings (core §13): `channel.<pin>.label` (the table's `label` channel, else the first gpio one) and `channel.<pin>.disable` (`OEP_HW_DISABLE`) are set → get shows them as the user's values, get's generation equals set's generation and differs from the previous generation. The export (the settings file) contains both. A plan to the disabled channel is refused (unavailable / PinsTaken). Before save, `needs_save`; save → state is `applied` with that generation, and after that not `needs_save`. **Restart** (with `oep.probe.restart` if the probe offers it; on a USB probe only when `OEP_HW_RESTART=1`; otherwise a bridge's classic ESP32 from RTS to EN; if neither, skipped) → the saved values read back by name and are applied at boot (the export is the same). Unset both → import the exported file again (both come back) → unset both once more. save. Other settings on the probe are left as they are. Whatever fails, the values and the storage are put back | The generation at each step, how it restarted, boot_id before and after, and seconds |
| `wire` | Only with `OEP_HW_TARGET=<name>[@swdio[,swclk]]`: scan (the named pair or all), attach with the reset TLV if `OEP_HW_RESET=<channel>`, then 50 times (`OEP_HW_LOOPS`) halt → s0 / s1 / a0 / a1 via dmi → read_block (8 words from `OEP_HW_TARGET_ADDR`, default 0x20000000) → the same 4 again, unchanged → resume | The scan result, connection, DMSTATUS, speed, target_id, dpc, count and seconds, registers that changed: before, after, read again, the 2 words right after the block, dpc (the failure message prints every change without omission). When an op raises: that loop, the exception, DMSTATUS read twice and DMCONTROL (raw dmi), dpc if halted |
| `rp2_swd` | Only with `OEP_HW_RP2=<port>` (an RP2040 / RP2350 behind the pair with the probe's labels `<port>.swdio` / `<port>.swclk`): attach (the chip is told from the DPIDR), halt, the core's 16 registers and 64 words of SRAM read twice the same, release. With `OEP_HW_RP2_IMAGE` (an ELF or .bin), the target's flash is rewritten: erase, program and verify through the ROM from a reset core, restart, and the core runs again (the PC moves) | The chip, DPIDR, the PC when halted, the image's name and size, the write log and seconds, the PC seen after restart |
| `gpio` | `oep.fixture.gpio` on the table's 2 free channels (`OEP_HW_GPIO=a,b`): output_high reads 1, output_low 0, input_pullup 1, input_pulldown 0. Skipped when the table gives that board no free channels | All values read |
| `uart` | `oep.fixture.uart` on the table's RX / TX (`OEP_HW_UART=rx,tx`), or, when the probe's settings have that plan or the table gives the board no pair, on that plan's pins (skipped if neither): configure 115200 8N1 within 5 %, status shows the baud and format in effect (configure's actual speed, 8N1), configure 9600. With `OEP_HW_UART_LOOP=rx,tx` (the 2 wires connected), what is written comes back with read | The actual speeds, status, the loopback byte count |
| `capture` | `oep.fixture.logic` is planned on the table's 2 free channels together with `oep.fixture.gpio` on the same pins (a logic capture only listens, oep-if-capture §1.2. On a probe that refuses the sharing, the channels released by the settings' idle item are pulled instead): a one-shot at the lowest declared rate (1 kHz or more) over a 10 ms window (16 samples or more; a segment over 16 KB is shrunk), once with pull-up and once with pull-down → every sample of every channel is 1, then 0. The segment's sample count equals configure's value, it does not finish earlier than the window (minus 1 %) and finishes within 1 second after the window, status is done with no dropped / slipped flag, and the generation advances by 1 per start | The declarations (rate_range, modes with max_samples, channels max), the actual rate, layout (w, pos), sample count, byte count, blocking_ms, start → done seconds per capture, read seconds, start_ns and its uncertainty, generation, the count of 1s per channel |
| `capture_analog_trigger` | The crossing trigger of `oep.fixture.analog`. With a pair the probe's labels allow (`oep_client.stimulus`: a drive `<port>.stimulus` and an ADC input `<port>.stimulus_in`, checked on the wiring of the day; nothing another bench's target owns is moved): up as the line goes high and down as it goes low, each at the pretrigger's index, with the values before it on the other side of the level. Without such a pair it is checked with force: waiting at a level never reached, then the segment cut at the forced sample, with the pretrigger before it | The pair, the up / down trigger_index, the min / max of the values before it, the value at it. With force: the channel, trigger_index, sample count |
| `capture_repeat_rearm` | Only when logic takes repeat with rearm 1 (a refusal for another reason fails): fixture gpio drives a free channel of the table and logic listens to the same channel. A rising edge per segment → each segment starts at its own trigger (index = the pretrigger, low before it and high from it; the rearm flag, and gap from the second on), a record each (oep-if-capture §3.9), state 5 when the slots are full, and release arms the next segment again | The channel, rate, segment count, the status when full, the record count |
| `pwm_counter` | `oep.fixture.pwm` and `oep.fixture.counter` (if both are in list) on one free channel of the table (the counter only listens): a PWM of 1 kHz and of 20 kHz (up to the counter's maximum), counted in a window, matches the frequency made within an edge and the window's own clock. After the stop nothing is counted | The declarations (the PWM and counter maximums, duty bits, max_gate_us), each run's frequency, duty, window, count and expected count, the count after the stop |
| `capture_external_clock` | Only when logic declares external_clock: a PWM (10 kHz) is output on the first of the table's 2 free channels as the clock (the logic only listens), and the second is driven high / low by fixture gpio as data → a 200-sample one-shot is all 1 / all 0, actual_rate 0 / 1, no clock role in layout, and it does not finish earlier than 200 periods. If a qualifier is declared, the data wire is made the qualifier: nothing is taken while it is low, and samples are taken when high. If format 3 is declared, the deltas from the second on are one clock period (in the timebase) | The clock frequency, the sample count and count of 1s each time, seconds to done, the start's uncertainty, the min / max of format 3's delta |
| `capture_glitch_filter` | Only when logic declares glitch_filter: a PWM of 100 kHz with pulses about 200 ns wide is output on a free channel, and the same channel is captured at 20 MHz → without the filter the pulses are seen, and with a filter longer than the pulse not one sample is high | The filter value, the pulse width, the count of 1s |
| `capture_analog_format_2` | Only when analog declares format 2: a one-shot with minmax and sum (D 16, block_max 64) on a free ADC channel → every interval has min ≤ max, the sum is between the point count × min and × max, and the values fit in b bits | block, byte count, interval count, intervals that did not hold |
| `capture_analog` | `oep.fixture.analog` (if in list) on a free channel that role 0 allows (`OEP_HW_ANALOG=<channel>`): a one-shot at the lowest rate, 10 ms, with the widest frontend. layout (s / o / b, order), frontend_used, scale / zero / reference come back, and the values fit in b bits. The pull-up / pull-down bands (mean at least 80 % / at most 20 % of full scale) are judged only when the probe lets gpio share the pin. The reference ESP32 firmware does not share (an analog channel is shared with nothing), so the values of the floating pin are recorded | The declarations (frontends too), the configure response, the values' min / max / mean (raw and mV), calibration (scheme, vrefint) |
| `capture_group` | `oep.fixture.capture-group` (if it declares both tracks): logic and analog with the same settings as above are bound and started together → the start response has both generations, each track has one segment with the sample count configure gave, each start_ns is at or after the group's start_ns (within 1 second), both are read, unbound | The group's declarations (tracks only), start_ns, each track's offset from it, generations, seconds to done |
| `i2c_target` | `oep.fixture.i2c-target` (if in list) with SDA / SCL on free channels the roles allow (`OEP_HW_I2C=sda,scl`): state 0 before configure. configure with address 0x42 → state 1, no slots. preload up to 3 (up to queue_depth) → tx_slots is that count. read_rx (recorded). stretch(0) if in ops. configure again → state 1, queue, slots and totals are 0. There is no controller on the bus and the lines float, so errors and frames from glitches are recorded and not judged | The declarations (queue_depth, max_clock_hz, max_length, features, max_stretch_us, internal_pullups), each status, read_rx, stretch |
| `spi_target` | `oep.fixture.spi-target` (if in list) with SCK / MOSI / MISO / CS on 4 free channels (`OEP_HW_SPI=sck,mosi,miso,cs`; for an interface that comes with fixed pin sets, the first channel_group): state 0 before configure. configure mode 0 MSB first → state 1. arm 4 bytes with 2 MISO bytes → armed, or already consumed (SCK / CS float, and the ATOM's target counts 1 to 2 phantom transfers right after arm; for that reason, the "one at a time" refusal of a second arm is not relied on). read_rx. configure again (mode 0 MSB first; there is no reset) → state 1, armed is gone, queue and totals are 0 | The declarations, each status (transactions = the phantom transfers), read_rx |
| `i2c_controller` | `oep.fixture.i2c-controller` (if in list) with SDA / SCL on free channels the roles allow (`OEP_HW_I2C_CONTROLLER=sda,scl`), with nothing wired (the pads' pull-ups hold the bus high): state 0 before configure. Asked for 100 kHz, the clock used is at or below it and at or above the declared minimum. On a probe with pull-ups, an address nobody answers is NACKed (result 1, a STOP sent), a write without a STOP that is NACKed holds no bus, a request for a STOP alone sends nothing, and status counts 2 transfers / 2 errors | The declarations (clock maximum and minimum, max_length, max_stretch_timeout_us, internal_pullups), the clock used, write_read, status |
| `spi_controller` | `oep.fixture.spi-controller` (if in list) with SCK / MOSI / MISO / CS on 4 free channels (`OEP_HW_SPI_CONTROLLER=sck,mosi,miso,cs`), with nothing wired: state 0 before configure. Asked for 1 MHz (up to the declared maximum), the clock used is at or below it and at or above the declared minimum. Two transfers back to back → 2 / 1 bytes received, the second's gap and each setup / hold at least the times asked (MISO floats: its bytes are only recorded), status counts 2 transfers | The declarations (clock maximum and minimum, max_length, time_step_ns, modes, max_delay_ns), the clock used, each transfer (bytes received, times), status |
| `console` | Only with `OEP_HW_TARGET`: scan, attach while running (no halt, no reset), open `oep.target.console` on that connection with mechanism dmseq (otherwise the first one the probe declares), read what arrives for 1 second from the position at open, the stream appears in the streams list, close, detach | The mechanism list, stream, existing, the bytes stored, the bytes seen (may be 0), lost, the mark count, the streams list, the first 64 bytes |
| `network` | fn 0's network (when the probe has it; transports §7): the state is recorded. With `OEP_WIFI_SSID_<n>` / `OEP_WIFI_PASS_<n>` (n is the entry): `wifi.<n>.ssid` / `wifi.<n>.passphrase` are set and saved - bench settings, so they are **left** on the probe -. If the probe's entry has the same ssid and no passphrase is given, it is not sent (a secret cannot be compared). Within `OEP_HW_WIFI_WAIT_S` (30 seconds), the wifi netif is up with an address | The netif list, tcp_port, the state before, the entries sent / unchanged, state, reason, entry, rssi, whether an address came, seconds. The ssid, passphrase and address are not recorded |
| `port_speed` | Over a UART bridge, only on a probe with port_speed in the ops of `oep.probe.link`: `oep_client.linktest.matrix` at the current speed and the table's candidates (`OEP_HW_RATES`), in / out / duplex, in flight 1 and the probe's maximum, one frame size (`max_frame - 7`, the most one source response carries; a sink request is at most `max_frame - 12`), `OEP_HW_FRAMES` (100) frames per condition. **Verdict** (the steps in [Speeding up a UART bridge](../../docs/cli.md#speeding-up-a-uart-bridge-port_speed-only-when-used)): a one-at-a-time condition at a raised speed fails when broken + lost is 3 or more and the ratio exceeds max(the ratio of the same condition at the boot speed × 2, 5 % (`OEP_HW_ERROR_MAX`)). It fails only when not one candidate passes (failed candidates, candidates the probe refused and candidates whose confirm does not return are recorded) | ok / broken / lost per condition, KB/s, seconds, the actual value of the speed, the link's counters |
| `session` | A 1000 ms lease expires → `NoSession` (released, no resume). force with a new id → the old id is `Locked`. After its end, every id is `NoSession` | lease, boot_id, the refusal texts |

The results file also has the client's version and commit, the firmware's origin (checkout + commit + dirty, or the release version + the json's
URL + sha256), the host's platform, and all `OEP_*` environment variables of the run (`OEP_WIFI_*` only as `(set)`; the values are not included). The one-screen summary is printed at the end of pytest.

On the virtual bench (`virtual-esp32-v003`), `capture` only records the levels and does not judge them (the virtual bench captures a counter, not pins,
and the one-shot finishes at the same time as the start response).

## A probe that does not come back, and the settings tests/hw changes

**The restart wait.** A host waits for and retries a restarted probe until restart_max_ms has passed (host guide §5.2):
`restart_probe` does not wait longer than that on its own, and a probe not back by then is treated as gone and its link closed. On WSL, a USB probe that
re-enumerated is a new device on Windows, and reaches Linux only after `usbipd attach --wsl` attaches it again (by hand, or by
keeping `usbipd attach --wsl --auto-attach --busid <busid>` running in a Windows shell). That can take longer than the probe's
restart_max_ms. `OEP_HW_REOPEN_S=<seconds>` asks in advance for the user's reopening of such a probe:
after the wait has passed, it reopens the same way as a new open (confirm first) for up to that many more seconds (`restart_probe(reopen_s=...)`;
`reopened` in the record). Without it, the config test writes that and fails, and later tests skip with "the probe is gone" - each first tries to
open it again once, so attaching by hand while the run goes on brings the rest back.

**Settings.** Tests change the probe's settings in 3 places, `network`, `config` and `capture`, and all but `network` put them back whatever happens (an assert failure, an exception, the probe going away). The table also lists the session's resources:

| Where | What it changes | How it is put back |
|---|---|---|
| `network` | the `wifi.<n>.*` values from `OEP_WIFI_*` (set and save) | Not put back: bench settings (to remove them, `oep settings unset <probe> core wifi.<n>.ssid wifi.<n>.passphrase --save`) |
| `config` | `channel.<pin>.label` and `.disable` (set), the storage (save twice) | The test unsets both and saves. On top of that, `finally` calls `Run.restore_settings`: each value is unset or set back to its value before the run, and the storage is saved once more (if nothing was saved before the run, everything is unset and saved) |
| `capture` (logic, when the probe refuses to overlay gpio on the capture's pins) | `channel.<pin>.idle` of the 2 channels (set, not saved) | `_Pull.release` unsets them, and `restore_settings` in its `finally` |
| `gpio`, `uart`, `capture*`, `i2c_target`, `spi_target` | plans (oep.probe.plan), the session's state | `plan_release` in each `finally`. A plan belongs to the session, so what a failure leaves is also released at the end of the session when the run finishes |
| `wire`, `console` | connections, console streams | detach (and close) in `finally`. These also belong to the session |
| `capture_group` | the bind of the group's tracks | `bind([])` in `finally`. This also belongs to the session |
| `port_speed` | the link's speed | `linktest.matrix` goes back to the boot speed in `finally`. The probe also goes back by itself if nothing happens |
| `session` | leases, takeover with force | Its own sessions, ended |

The harness opens the probe with `link.open_host`, so it keeps a session id per probe ([A host that runs again](../../docs/cli.md#a-host-that-runs-again-the-kept-session-id)): a run stopped midway
leaves its session on the probe (closing the transport does not end the session, transports §3), but the next run's first open ends it, without waiting for the 30 s lease
to expire.

No test touches settings other than its own label and disable: what the bench keeps is not changed. When the probe went away,
`restore_settings` reopens it (`OEP_HW_REOPEN_S`, waiting at least 5 s, and up to 35 s for the lost session's lease),
and the cleanup at the end of the run tries once more. What still could not be put back is written to the results file (`_settings.left_on_probe`), and the summary
prints it as **SETTINGS LEFT ON THE PROBE**, together with the commands to run once the probe is back. For example:

```sh
oep settings unset <probe> core channel.GPIO28.disable
oep settings save <probe>      # if nothing was saved before the run: `oep settings unset --all <probe> --save`
```

## Targets and update paths

`boards.py` is a table that bundles units with profiles and wiring defaults. It does not read the general TOML. The placement of real equipment, observation results, serials and USB topology are managed separately by each equipment administrator.

| Kind | Update path |
|---|---|
| classic ESP32 | esptool and a merged image |
| ESP32-P4 | USB DFU app image, or an explicit USB-Serial/JTAG path |
| ESP32-S3 | esptool and the merged image through the board's USB-UART bridge (OEP talks over the chip's USB) |
| RP2040 / RP2350 | BOOTSEL and picotool, or an explicit UF2 drive |
| TCP | Not updated through this entry point. The installed firmware is verified over the given network path |

For TCP, `OEP_HW_BOARDS=oep://<unit_id>?via=net` or `tcp://<host>:<port>` can be given explicitly. A general TCP entry has no expected model or free channels set. Fixture channels are not added without checking the wiring. `OEP_HW_NOFLASH` chooses testing the installed firmware; it does not mean settings or power operations are forbidden.

## Environment variables

| Variable | Meaning |
|---|---|
| `OEP_HW_BOARDS` | Comma-separated board ids (required; nothing runs without it). `oep://<unit_id>?via=net` / `tcp://<host>[:<port>]` is a TCP probe not in the table |
| `OEP_WIFI_SSID_<n>`, `OEP_WIFI_PASS_<n>` | The bench's Wi-Fi the `network` test uses (n is the entry's index; without PASS, an open network). Read from the environment, not shown, recorded only as `(set)`. To set by hand: `oep settings set <probe> core wifi.<n>.ssid=... wifi.<n>.passphrase=...` (the passphrase is not shown) |
| `OEP_HW_WIFI_WAIT_S` | Seconds the `network` test waits for the connection (default 30) |
| `OEP_HW_ATOM_TCP` | The TCP destination of `esp32-pico-d4-50029191fe34-tcp` (default `oep://50029191fe34?via=net`, DNS-SD) |
| `OEP_PROBE_DIR` / `OEP_PROBE_VERSION` / `OEP_HW_NOFLASH` | The firmware's origin (one of them) |
| `OEP_HW_TARGET`, `OEP_HW_RESET`, `OEP_HW_TARGET_ADDR`, `OEP_HW_LOOPS` | The wire test: a target is connected (and where), the reset line, the block address, the count |
| `OEP_HW_REOPEN_S` | How many more seconds to reopen a restarted probe after restart_max_ms has passed (the user reopens; above; WSL / usbipd). Default 0: none - after restart_max_ms the probe is treated as gone |
| `OEP_HW_RESTART=1` | The config test restarts a USB probe (P4, RP2) with `oep.probe.restart` (default: not; some combinations fail enumeration after a restart and need a replug) |
| `OEP_HW_WIRE` | The wire name for the wire / console tests (default: when `OEP_HW_TARGET` names one pin and the probe has `oep.wire.swio`, that; otherwise the first wire). The console test uses the instance of `oep.target.console` that accepts an open on that connection |
| `OEP_HW_GPIO`, `OEP_HW_DISABLE`, `OEP_HW_UART`, `OEP_HW_UART_LOOP` | Overrides of the fixture tests' channels |
| `OEP_HW_ANALOG`, `OEP_HW_I2C`, `OEP_HW_SPI`, `OEP_HW_I2C_CONTROLLER`, `OEP_HW_SPI_CONTROLLER` | The analog capture's channel, the `sda,scl` of i2c-target and i2c-controller, the `sck,mosi,miso,cs` of spi-target and spi-controller (default: of the table's free channels (the 2 gpio, disable, the 2 UART), those describe allows) |
| `OEP_HW_RATES`, `OEP_HW_FRAMES`, `OEP_HW_LT_TIMEOUT`, `OEP_HW_ERROR_MAX` | port_speed candidates, frames per condition, the answer wait, the verdict's floor (default 5 %) |
| `OEP_HW_PICOTOOL`, `OEP_HW_UF2_DRIVE`, `OEP_HW_USBIP_BUSID`, `OEP_HW_CACHE` | Details of tools and environment |

Tools: `arduino-cli` (local builds), `esptool` (classic ESP32, ESP32-S3), `picotool` (RP2). Searching for DFU and the boot ROM uses pyusb
(already a client dependency). `gh`, `dfu-util` and a mounted drive are not needed.
