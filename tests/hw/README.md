# Hardware integration test (`tests/hw`)

[日本語](README.ja.md)

The pre-release test of oep-spec's [docs/release-testing.ja.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/release-testing.ja.md):
a probe firmware (oep-probe-arduino's `examples/Firmware/OepProbe`) is put on a real board and this client is run through
it, end to end. The ordinary test suite (`uv run pytest`) runs against the fake probe and never touches hardware; this
directory is skipped entirely unless `OEP_HW_BOARDS` names boards. Both the firmware release (built from main, every
board at hand) and the client release (the latest firmware release) go through it; neither ships when it fails.

```sh
# build the firmware from a checkout (main against main: before a firmware release)
OEP_HW_BOARDS=esp32-pico-d4-50029191fe34 OEP_PROBE_DIR=~/dev_oep/oep-probe-arduino uv run pytest tests/hw -m hw

# the GitHub release's images (before a client release, a reproduction, CI with a board)
OEP_HW_BOARDS=esp32-pico-d4-50029191fe34 OEP_PROBE_VERSION=0.0.25 uv run pytest tests/hw -m hw

# test what is on the board already (nothing is flashed)
OEP_HW_BOARDS=9489dd2ae0953650 OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw

# a dry run of the test logic on the fake probe (a pty; no hardware, never a release test)
OEP_HW_BOARDS=fake-esp32-v003 uv run pytest tests/hw -m hw
```

`-s` shows the flasher's progress and the port_speed table as they happen. A board's whole run takes 1.5-2.5 minutes
(about 40 s of it the ESP32 flash at 115200); a local build adds about a minute the first time per profile (cached under
`~/.cache/oep-hw/`, `OEP_HW_CACHE`). Several boards: `OEP_HW_BOARDS=a,b`; the tests run board by board.

## Files

| File | What |
|---|---|
| `boards.py` | the board table, keyed by board-identify id (or, for a USB probe without one, its unit id): kind, sketch.yaml profile, how to reach its OEP port after flashing, expected model, free channels for the tests |
| `firmware.py` | where the firmware comes from: `OEP_PROBE_DIR` (`arduino-cli compile --profile <profile> --output-dir ...`) or `OEP_PROBE_VERSION` (`firmware-<ver>.json` and the images from the GitHub release, sha256 checked, urllib only) |
| `flash.py` | the flashers per board kind, and the DTR / RTS reset of a bridge board |
| `record.py` | one board's run: the connection, the measurements, the results file |
| `test_probe.py` | the tests, in order |
| `conftest.py` | the `hw` marker and the skip without `OEP_HW_BOARDS`; grouping per board; the one-screen summary |
| `results/` | `<board>-<firmware>-<client>.json`, one per run (small summaries only; `.gitignore` keeps anything else out) |

## What each test checks

Every test after `flash` skips when the firmware could not be put on. Each records its measurements in the results
file; the `verdict` is pytest's outcome.

| Test | Checks | Records |
|---|---|---|
| `flash` | the image goes on (esptool / DFU / picotool), the board boots and confirms within 45 s | the firmware before and after (describe), the flasher's command, seconds, last lines |
| `identity` | confirm revision ≥ 1; list starts with `oep.core`; describe's model is the table's; boot_id changed over the flash; firmware string equals the version flashed (a local build's `library.properties`, or the release's) | boot_id, firmware, model, unit_id, chip, limits, the interface list |
| `config` | `oep.probe.config`: set a label and a `disable` item → get returns them and `hash_of` agrees; a plan on the disabled channel is refused (unavailable / PinsTaken); save → state `applied` with that hash; **reboot** (a classic ESP32 behind a bridge: EN through RTS as esptool's hard reset) → the saved items are applied at boot (state, get); unset both, save → the hash from before the test. Other settings on the probe (a bench's slots / binds) are kept | hashes at each step, the reboot's boot_ids and time |
| `wire` | only with `OEP_HW_TARGET=<name>[@swdio[,swclk]]`: scan (the pair named, or every pair), attach with the reset TLV when `OEP_HW_RESET=<channel>`, then 50× (`OEP_HW_LOOPS`) halt → s0 / s1 / a0 / a1 via dmi → read_block (8 words at `OEP_HW_TARGET_ADDR`, default 0x20000000) → the four registers again, unchanged → resume | scan result, connection, DMSTATUS, speed, target_id, dpc, loops and seconds, any register change |
| `gpio` | `oep.fixture.gpio` on the table's two free channels (`OEP_HW_GPIO=a,b`): output_high reads 1, output_low 0, input_pullup 1, input_pulldown 0 | every level read |
| `uart` | `oep.fixture.uart` on the table's RX / TX (`OEP_HW_UART=rx,tx`): configure 115200 8N1 within 5 %, status says `session` with that rate and format, configure 9600; with `OEP_HW_UART_LOOP=rx,tx` (the two wired together) a write is read back | the actual rates, the status, the loopback bytes |
| `capture` | `oep.fixture.logic` on the table's two free channels, planned together with `oep.fixture.gpio` on the same pins (a logic capture listens only, oep-if-capture §1.2; a probe that refuses the sharing gets the settings' idle item on the released channels instead): a one-shot at the lowest declared rate (≥ 1 kHz) over a 10 ms window (≥ 16 samples; a segment over 16 KB is cut), pulled up then pulled down → every sample of every channel reads 1, then 0. The segment's samples equal configure's, the capture finishes no earlier than its window (less the declared rate uncertainty) and within a second after it, status says done with no dropped / slipped flag, the generation advances by 1 per start | the declarations (rate_range, modes, layouts, max_read, segment_ring), the actual rate, layout (w, pos), samples, bytes, timing (jitter, ppm, blocking); per capture start → done seconds, read seconds, start_ns and its uncertainty, generation; the ones per channel |
| `capture_analog` | `oep.fixture.analog` (when listed) on a free channel its role 0 allows (`OEP_HW_ANALOG=<channel>`): a one-shot at the lowest rate over 10 ms with the widest frontend; layout (s / o / b, order), frontend_used, scale / zero / reference come back, the values fit b bits. The pull-up / pull-down bands (mean ≥ 80 % / ≤ 20 % of full scale) are judged only when the probe lets gpio share the pin; the reference ESP32 firmware does not (an analog channel is shared with nothing), so the floating pin's values are recorded | the declarations (frontends too), the configure answer, the values' min / max / mean (raw and mV), calibration (schemes, vrefint) |
| `capture_group` | `oep.fixture.capture-group` (when it lists both tracks): logic + analog configured as above, bound, started together → the start answer names both generations, one segment each with its configured samples, each start_ns at or after the group's (within 1 s), both read back, unbound | the group's declarations, start_ns, each track's offset from it, generations, done seconds |
| `i2c_target` | `oep.fixture.i2c-target` (when listed), SDA / SCL on free channels its roles allow (`OEP_HW_I2C=sda,scl`): state 0 before configure; configure address 0x42 mode 3 (when declared) → state 1, preload three tx slots → counted 1, 2, 3 and tx_slots 3; configure mode 1, arm_rx 4 → armed; read_rx; reset → armed off, queue and counters 0. There is no bus controller and the lines float: a glitch's error or frame is recorded, not judged | the declarations (queue_depth, max_clock_hz, max_length, features), every status, read_rx |
| `spi_target` | `oep.fixture.spi-target` (when listed), SCK / MOSI / MISO / CS on four free channels (`OEP_HW_SPI=sck,mosi,miso,cs`; the first channel_group when the interface comes as fixed pin sets): state 0 before configure; configure mode 0 MSB first → state 1; arm 4 bytes with 2 MISO bytes → armed, or consumed already (SCK / CS float: the ATOM's target counts 1-2 phantom transactions right after the arm, so the one-at-a-time refusal of a second arm is not relied on); read_rx; reset → armed off, queue and counters 0 | the declarations, every status (transactions = the phantoms), read_rx |
| `console` | only with `OEP_HW_TARGET`: scan, attach running (no halt, no reset), `oep.target.console` open on the connection with mechanism dmseq (or the first the probe declares), read what arrives for 1 s from the position at open, the streams list shows the stream, close, detach | mechanisms, stream, existing, backlog bytes, bytes seen (may be 0), lost, marks, the streams list, the first 64 bytes |
| `port_speed` | UART bridge probes only: `oep_client.linktest.matrix` at the speed in force and the table's candidates (`OEP_HW_RATES`), patterns in / out / duplex, in flight 1 and the probe's max, one frame (`max_frame - 16`), `OEP_HW_FRAMES` (100) frames a cell. **Verdict** (host guide §7.3.2): a one-at-a-time cell at a raised rate fails when broken + lost ≥ 3 and its ratio is over max(2 × the same cell's ratio at the boot speed, 5 % `OEP_HW_ERROR_MAX`); the run fails only when no candidate passes (a failing candidate is recorded, as is one the probe refuses or that gives no confirm) | every cell (ok / broken / lost, KB/s, seconds), the rates' actual values, the link's counters |
| `session` | a 1000 ms lease lapses → `Expired`; the same id re-opened says resumed 2 (swept); a force takeover with a new id → the old id is `Locked` out | the lease, the resumed codes, the refusals' texts |

The results file also carries the client's version and commit, the firmware source (checkout + commit + dirty flag, or
release version + manifest URL + sha256s), the host platform and every `OEP_*` variable of the run. The one-screen summary
is printed at the end of the pytest run.

On the fake (`fake-esp32-v003`) `capture` records its levels without judging them (the fake captures a counter, not its
pins, and its one-shot is done as start answers), and `i2c_target` / `spi_target` fail: the fake declares the two
interfaces but plans and runs neither.

## Boards and flashers

| Board (`OEP_HW_BOARDS` id) | Kind | Profile | Flasher | Exercised |
|---|---|---|---|---|
| `esp32-pico-d4-50029191fe34` M5Stack ATOM (ESP32-PICO-D4, FTDI) | esp32 | esp32 | `esptool --chip esp32 -p <port> -b 115200 write-flash 0x0 <merged.bin>` (the ATOM's bridge takes 115200) | **yes** (0.0.26 main build and release 0.0.25, 2026-10-01) |
| `esp32-d0wd-v3-0070070d9394` V003 jig (ESP32-D0WD-V3, CH340) | esp32 | esp32 | the same | **yes** (0.0.26 main build, 2026-10-02, with the CH32V003 over SWIO: wire 50 loops pass; port_speed flaky on the CH340's bursty loss) |
| `esp32-series-30eda0e31108` X035 jig (ESP32-P4), `esp32-series-30eda0e343c6` second P4 | esp32p4 | esp32p4 | USB DFU 1.1 of the app `.bin` to the running probe (pyusb, what `dfu-util -D` does: the DFU interface found by class FE/01, wTransferSize from its functional descriptor, DNLOAD blocks, a zero-length DNLOAD to manifest), wait for the device to drop and return; on WSL `usbipd.exe attach --wsl --busid <busid>` (the table's `usbip_busid`) | **yes** (X035 jig, 0.0.26 main build, 2026-10-02: DFU on interface 4 / 4096 B in 8 s; wire over RVSWD 50 loops pass; the uart test runs on the jig's settings plan rx 12 / tx 6). The second P4 was flashed by hand over DFU (interface 7 / 1024 B on its old firmware); its new USB serial needs a Windows usbipd re-bind before a run |
| `9489dd2ae0953650` SparkFun Pro Micro RP2350 (1b4f:0026, keyed by unit id) | rp2 | promicrorp2350 | the 1200-baud touch on the CDC port (BOOTSEL), wait for the boot ROM's USB device (`RP2350 Boot` / `RP2 Boot`, matched by product and serial so another RP2 on the host is never touched), `picotool load -x <uf2> --bus --address` (`OEP_HW_PICOTOOL`, default `picotool` on PATH; [picotool 2.3.1 Linux x86_64](https://github.com/raspberrypi/pico-sdk-tools/releases/download/v2.3.1-0/picotool-2.3.1-x86_64-lin.tar.gz)), wait up to 45 s for the CDC port to return. With `OEP_HW_UF2_DRIVE=<mount>` the `.uf2` is copied to that BOOTSEL drive instead (a host that mounts it); neither possible → the flash is skipped and the tests run on the firmware already there | **yes** (picotool, 2026-10-02; the first run froze the probe at `uart` on pins its UART cannot use - fixed in oep-probe-arduino 654b06a; the run with that build passes) |

The ATOM's bridge (a CH552 posing as FTDI) loses probe→host frames in bursts (0 % one minute, 10-55 % the next, 2026-10-02): a port_speed failure there is re-run once before it counts; the CH340 jig is the steadier gate for port_speed.

`OEP_HW_NOFLASH=1` skips flashing on every board and tests the firmware found there (recorded as "on-board"; the firmware
string is recorded, not compared).

The `rp2040` / `rp2350` (Pico) profiles use the same rp2 flasher; a board entry is all they need.

## Shared jigs: the permission rule

The V003 jig, the two ESP32-P4 jigs and the WCH-Links on this host belong to the ArduinoCore-CH32RV bench (another
session's hardware). The board table has them so that a release test can cover them, but **running on a jig waits for
the bench's permission**: ask first, every time; never flash or reset a device you were not given. The ATOM
(`/dev/ttyUSB1`) is the board that is free for OEP tests. A `jig` in a board's `notes` marks the shared ones
(`Board.shared`).

## Environment

| Variable | Meaning |
|---|---|
| `OEP_HW_BOARDS` | comma list of board ids (required; nothing runs without it) |
| `OEP_PROBE_DIR` / `OEP_PROBE_VERSION` / `OEP_HW_NOFLASH` | the firmware source (one of them) |
| `OEP_HW_TARGET`, `OEP_HW_RESET`, `OEP_HW_TARGET_ADDR`, `OEP_HW_LOOPS` | the wire test: a target is wired (and where), its reset line, the block address, the loop count |
| `OEP_HW_GPIO`, `OEP_HW_DISABLE`, `OEP_HW_UART`, `OEP_HW_UART_LOOP` | channel overrides for the fixture tests |
| `OEP_HW_ANALOG`, `OEP_HW_I2C`, `OEP_HW_SPI` | the analog capture's channel, the i2c-target's `sda,scl`, the spi-target's `sck,mosi,miso,cs` (default: the table's free channels - gpio pair, disable, UART pair - that the describe allows) |
| `OEP_HW_RATES`, `OEP_HW_FRAMES`, `OEP_HW_LT_TIMEOUT`, `OEP_HW_ERROR_MAX` | the port_speed test's candidates, frames a cell, answer wait, the verdict's floor (default 5 %) |
| `OEP_HW_PICOTOOL`, `OEP_HW_UF2_DRIVE`, `OEP_HW_USBIP_BUSID`, `OEP_HW_CACHE` | tool and platform details |

Tools: `arduino-cli` (a local build), `esptool` (classic ESP32), `picotool` (RP2); pyusb for DFU and the boot ROM lookup
(a dependency of the client already). No `gh`, `dfu-util` or mounted drive is needed.
