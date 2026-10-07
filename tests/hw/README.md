# Hardware integration test (`tests/hw`)

[日本語](README.ja.md)

The pre-release test of oep-spec's [docs/release-testing.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/release-testing.md):
a probe firmware (oep-probe-arduino's `examples/Firmware/OepProbe`) is put on a real board and this client is run through
it, end to end. The ordinary test suite (`uv run pytest`) runs against the virtual bench and never touches hardware; this
directory is skipped entirely unless `OEP_HW_BOARDS` names boards. Both the firmware release (built from main, every
board at hand) and the client release (the latest firmware release) go through it; neither ships when it fails.

```sh
# build the firmware from a checkout (main against main: before a firmware release)
OEP_HW_BOARDS=esp32-pico-d4-50029191fe34 OEP_PROBE_DIR=~/dev_oep/oep-probe-arduino uv run pytest tests/hw -m hw

# the GitHub release's images (before a client release, a reproduction, CI with a board)
OEP_HW_BOARDS=esp32-pico-d4-50029191fe34 OEP_PROBE_VERSION=0.0.25 uv run pytest tests/hw -m hw

# test what is on the board already (nothing is flashed)
OEP_HW_BOARDS=9489dd2ae0953650 OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw

# a dry run of the test logic on the virtual bench (a pty; no hardware, never a release test)
OEP_HW_BOARDS=virtual-esp32-v003 uv run pytest tests/hw -m hw

# the ATOM: flash it over its bridge and give it the bench's Wi-Fi (read from the environment, never printed or recorded)
OEP_WIFI_SSID_0=lab OEP_WIFI_PASS_0="$LAB_PASS" OEP_HW_BOARDS=esp32-pico-d4-50029191fe34 OEP_PROBE_DIR=~/dev_oep/oep-probe-arduino uv run pytest tests/hw -m hw
# then the same board over Wi-Fi / TCP (found by DNS-SD; OEP_HW_ATOM_TCP=tcp://HOST:PORT where mDNS does not reach)
OEP_HW_BOARDS=esp32-pico-d4-50029191fe34-tcp uv run pytest tests/hw -m hw
# any probe on the network, no table entry
OEP_HW_BOARDS=tcp://192.168.1.23:7450 uv run pytest tests/hw -m hw
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
| `test_harness.py` | the harness's own bookkeeping on the in-process virtual bench (no `hw` marker: it runs in the ordinary `uv run pytest`): settings put back after a failed test and after a probe that went away |
| `conftest.py` | the `hw` marker and the skip without `OEP_HW_BOARDS`; grouping per board; the one-screen summary |
| `results/` | `<board>-<firmware>-<client>-<started>.json`, one per run - the run's start time (`20261006T203121`) in the name, so no run overwrites another (small summaries only; `.gitignore` keeps anything else out) |

## What each test checks

Every test after `flash` skips when the firmware could not be put on, or when the probe went away and does not answer again. Each records its measurements in the results
file; the `verdict` is pytest's outcome.

| Test | Checks | Records |
|---|---|---|
| `flash` | the image goes on (esptool / DFU / picotool), the board boots and confirms within 45 s | the firmware before and after (describe), the flasher's command, seconds, last lines |
| `identity` | confirm revision ≥ 1; no list entry is fn 0 (the core has no name, core §7.2); clock (core §7.7) gives confirm's boot_id; describe's model is the table's; boot_id changed over the flash; firmware string equals the version flashed (a local build's `library.properties`, or the release's) | boot_id, firmware, model, unit_id, chip, limits, the interface list, clock's uptime_ns and shortest round trip of 4 |
| `required` | what every probe must give and a lock-free look can check (core §1.2, §7.1, §7.5; oep-spec `docs/conformance.md` section 1), the same list `oep dump` prints as MISSING: confirm's transport TLV, naming a transport of fn 0's describe (or 0xFF); fn 0's describe with unit_id, transport and max_op_ms. Fails naming each one missing | the list (empty when nothing is missing) |
| `config` | `oep.probe.config`: set a label (the table's `label` channel, else the first gpio one) and a `disable` item (`OEP_HW_DISABLE`) → get returns them, get's hash equals set's and differs from the one before (the hash is the probe's own, never computed here); a plan on the disabled channel is refused (unavailable / PinsTaken); `needs_save` before the save, save → state `applied` with that hash and no `needs_save` after it; **reboot** (`oep.probe.restart` when the probe lists it - `Host.restart_probe`, waiting up to its restart_max_ms, then `OEP_HW_REOPEN_S` more when set (below); on a USB probe (P4, RP2) only with `OEP_HW_RESTART=1`, else skipped with that reason in the record - else a classic ESP32 behind a bridge: EN through RTS as esptool's hard reset; neither: skipped) → the saved items are applied at boot: storage_hash equals get's hash and the items are the ones saved (`same_items`); unset both → the items equal, item by item, those before the test; save. Other settings on the probe (a bench's slots / binds) are kept; whatever fails, the items and the storage are put back (below) | hashes at each step, the reboot's way, boot_ids and time (restart_max_ms, reopen_s and whether the reopen was needed with oep.probe.restart; the error when the probe did not come back) |
| `wifi` | `oep.probe.config`'s wifi item (when describe lists it; probe.config §1.4, §3.3). With `OEP_WIFI_SSID_<n>` / `OEP_WIFI_PASS_<n>` (n = the index): the entries whose SSID or passphrase presence differ from the probe's are set (a passphrase cannot be compared, host guide §15.1) and saved - the bench's settings, they **stay** on the probe -, the state's link must be connected with an address within `OEP_HW_WIFI_WAIT_S` (30 s), and on a board reached otherwise, when DNS-SD finds it by its unit_id, `tcp:<unit_id>` must open it (describe's unit_id checked). Without the variables only the state is recorded | wifi_max, the indexes sent / unchanged, saved, the state (state, entry, reason, rssi, whether an address came), seconds to connect, DNS-SD's port and instance - never an SSID, passphrase or address |
| `wire` | only with `OEP_HW_TARGET=<name>[@swdio[,swclk]]`: scan (the pair named, or every pair), attach with the reset TLV when `OEP_HW_RESET=<channel>`, then 50× (`OEP_HW_LOOPS`) halt → s0 / s1 / a0 / a1 via dmi → read_block (8 words at `OEP_HW_TARGET_ADDR`, default 0x20000000) → the four registers again, unchanged → resume | scan result, connection, DMSTATUS, speed, target_id, dpc, loops and seconds, any register change: before, after, read again, the two words past the block, dpc (the failure message prints every change in full); when an op raises: the loop, the error, DMSTATUS read twice and DMCONTROL (raw dmi), dpc when halted |
| `gpio` | `oep.fixture.gpio` on the table's two free channels (`OEP_HW_GPIO=a,b`): output_high reads 1, output_low 0, input_pullup 1, input_pulldown 0. Skips when the table gives the board none | every level read |
| `uart` | `oep.fixture.uart` on the table's RX / TX (`OEP_HW_UART=rx,tx`), or on the probe's settings plan for it when that is in force or the table gives the board no pair (none either: skips): configure 115200 8N1 within 5 %, status shows the baud and format in force (configure's actual rate, 8N1), configure 9600; with `OEP_HW_UART_LOOP=rx,tx` (the two wired together) a write is read back | the actual rates, the status, the loopback bytes |
| `capture` | `oep.fixture.logic` on the table's two free channels, planned together with `oep.fixture.gpio` on the same pins (a logic capture listens only, oep-if-capture §1.2; a probe that refuses the sharing gets the settings' idle item on the released channels instead): a one-shot at the lowest declared rate (≥ 1 kHz) over a 10 ms window (≥ 16 samples; a segment over 16 KB is cut), pulled up then pulled down → every sample of every channel reads 1, then 0. The segment's samples equal configure's, the capture finishes no earlier than its window (less 1 %) and within a second after it, status says done with no dropped / slipped flag, the generation advances by 1 per start | the declarations (rate_range, modes with max_samples, channels max), the actual rate, layout (w, pos), samples, bytes, blocking_ms; per capture start → done seconds, read seconds, start_ns and its uncertainty, generation; the ones per channel |
| `capture_analog` | `oep.fixture.analog` (when listed) on a free channel its role 0 allows (`OEP_HW_ANALOG=<channel>`): a one-shot at the lowest rate over 10 ms with the widest frontend; layout (s / o / b, order), frontend_used, scale / zero / reference come back, the values fit b bits. The pull-up / pull-down bands (mean ≥ 80 % / ≤ 20 % of full scale) are judged only when the probe lets gpio share the pin; the reference ESP32 firmware does not (an analog channel is shared with nothing), so the floating pin's values are recorded | the declarations (frontends too), the configure answer, the values' min / max / mean (raw and mV), calibration (schemes, vrefint) |
| `capture_group` | `oep.fixture.capture-group` (when it lists both tracks): logic + analog configured as above, bound, started together → the start answer names both generations, one segment each with its configured samples, each start_ns at or after the group's (within 1 s), both read back, unbound | the group's declarations (its tracks only), start_ns, each track's offset from it, generations, done seconds |
| `i2c_target` | `oep.fixture.i2c-target` (when listed), SDA / SCL on free channels its roles allow (`OEP_HW_I2C=sda,scl`): state 0 before configure; configure address 0x42 → state 1, no tx slots; preload up to 3 slots (queue_depth) → tx_slots counts them; read_rx (recorded); stretch(0) when the ops offer it; configure again → state 1, the queue, the slots and the counts 0. There is no bus controller and the lines float: a glitch's error or frame is recorded, not judged | the declarations (queue_depth, max_clock_hz, max_length, features, max_stretch_us, internal_pullups), every status, read_rx, stretch |
| `spi_target` | `oep.fixture.spi-target` (when listed), SCK / MOSI / MISO / CS on four free channels (`OEP_HW_SPI=sck,mosi,miso,cs`; the first channel_group when the interface comes as fixed pin sets): state 0 before configure; configure mode 0 MSB first → state 1; arm 4 bytes with 2 MISO bytes → armed, or consumed already (SCK / CS float: the ATOM's target counts 1-2 phantom transactions right after the arm, so the one-at-a-time refusal of a second arm is not relied on); read_rx; configure again (mode 0 MSB first; there is no reset) → state 1, armed off, queue and counters 0 | the declarations, every status (transactions = the phantoms), read_rx |
| `console` | only with `OEP_HW_TARGET`: scan, attach running (no halt, no reset), `oep.target.console` open on the connection with mechanism dmseq (or the first the probe declares), read what arrives for 1 s from the position at open, the streams list shows the stream, close, detach | mechanisms, stream, existing, backlog bytes, bytes seen (may be 0), lost, marks, the streams list, the first 64 bytes |
| `port_speed` | UART bridge probes whose `oep.probe.link` sets port_speed in its ops only: `oep_client.linktest.matrix` at the speed in force and the table's candidates (`OEP_HW_RATES`), patterns in / out / duplex, in flight 1 and the probe's max, one frame size (`max_frame - 7`, the most one source answer carries; a sink request at most `max_frame - 12`), `OEP_HW_FRAMES` (100) frames a cell. **Verdict** (host guide §17.3.2): a one-at-a-time cell at a raised rate fails when broken + lost ≥ 3 and its ratio is over max(2 × the same cell's ratio at the boot speed, 5 % `OEP_HW_ERROR_MAX`); the run fails only when no candidate passes (a failing candidate is recorded, as is one the probe refuses or that gives no confirm) | every cell (ok / broken / lost, KB/s, seconds), the rates' actual values, the link's counters |
| `session` | a 1000 ms lease lapses → `NoSession` (released, no resume); a force takeover with a new id → the old id is `Locked` out; after its end every id is `NoSession` | the lease, the boot_id, the refusals' texts |

The results file also carries the client's version and commit, the firmware source (checkout + commit + dirty flag, or
release version + manifest URL + sha256s), the host platform and every `OEP_*` variable of the run - `OEP_WIFI_*` as
`(set)` only, never their values. The one-screen summary is printed at the end of the pytest run.

On the virtual bench (`virtual-esp32-v003`) `capture` records its levels without judging them (the virtual bench captures a counter, not its
pins, and its one-shot is done as start answers).

## A probe that does not come back, and the settings tests/hw changes

**The restart's wait.** A host retries a restarted probe only until its restart_max_ms has passed (host guide §5.2):
`restart_probe` never waits longer by itself, and a probe not back by then is gone - its link closed. On WSL a USB probe
that re-enumerates is a new device on Windows: it reaches Linux only once `usbipd attach --wsl` attaches it again (by hand,
or `usbipd attach --wsl --auto-attach --busid <busid>` left running in a Windows shell), which can take longer than the
probe's restart_max_ms (the RP2350's 2.0 s). `OEP_HW_REOPEN_S=<s>` is your reopen of such a probe, asked for beforehand:
after the window it is opened again as a new open (confirm first) for up to that many more seconds
(`restart_probe(reopen_s=...)`; the record says `reopened`). Without it the config test fails naming the loss, and the
later tests skip with "the probe is gone" - each first tries to open it again once, so an attach by hand during the run
brings the rest back.

**Settings.** The tests change the probe's settings in two places, and put them back whatever happens - a failed
assertion, an exception, a probe that went away:

| Where | What it changes | Put back |
|---|---|---|
| `wifi` | `wifi` items from `OEP_WIFI_*` (set and saved) | not put back: they are the bench's settings (remove them with `oep config wifi-unset`) |
| `config` | a `label` item and a `disable` item (set), the storage (saved twice) | the test unsets both and saves; its `finally` then calls `Run.restore_settings`: each item removed, or set back to the value it had before the run, and the storage saved again (or erased when nothing was saved before) |
| `capture` (logic, when the probe refuses gpio on the capture's pins) | `idle` items on the two channels (set, not saved) | `_Pull.release` unsets them, then `restore_settings` in its `finally` |
| `gpio`, `uart`, `capture*`, `i2c_target`, `spi_target` | plans (oep.probe.plan), session state | `plan_release` in each test's `finally`; a plan is the session's, so the session's end at the end of the run releases what a failure left |
| `wire`, `console` | a connection, a console stream | detach (and close) in `finally`; also the session's |
| `capture_group` | a bind of the group's tracks | `bind([])` in `finally`; also the session's |
| `port_speed` | the link's rate | `linktest.matrix` goes back to the boot speed in its `finally`; the probe also falls back by itself when idle |
| `session` | leases, a force takeover | its own sessions, ended |

The harness opens the probe through `link.open_host`, so it keeps its session id per probe (README, "A host run again"):
a run that was stopped half way leaves its session on the probe (a closed transport does not end it, transports §3), and
the next run's first open ends it instead of waiting out its 30 s lease.

No test touches slots, binds of the settings, uart or plan items: those a bench keeps are never changed. When the probe
went away, `restore_settings` opens it again (waiting `OEP_HW_REOPEN_S`, at least 5 s, and up to 35 s for a lost
session's lease), and the run's teardown tries once more. What still could not be put back is in the results file
(`_settings.left_on_probe`) and the summary prints it under **SETTINGS LEFT ON THE PROBE** with the commands to run
once the probe is back, e.g.:

```sh
oep config remove <probe> disable 28
oep config save <probe>        # or `oep config erase <probe>` when nothing was saved before the run
```

## Boards and flashers

| Board (`OEP_HW_BOARDS` id) | Kind | Profile | Flasher | Exercised |
|---|---|---|---|---|
| `esp32-pico-d4-50029191fe34` M5Stack ATOM (ESP32-PICO-D4, FTDI) | esp32 | esp32 | `esptool --chip esp32 -p <port> -b 115200 write-flash 0x0 <merged.bin>` (the ATOM's bridge takes 115200) | **yes** (0.0.26 main build and release 0.0.25, 2026-10-01) |
| `esp32-d0wd-v3-0070070d9394` V003 jig (ESP32-D0WD-V3, CH340) | esp32 | esp32 | the same | **yes** (0.0.26 main build, 2026-10-02, with the CH32V003 over SWIO: wire 50 loops pass; port_speed flaky on the CH340's bursty loss). Every output-capable channel but SWIO / NRST goes to a V003 pad (ArduinoCore-CH32RV `tests/benches/v003-esp32.toml`): the table gives it only the input-only 34 / 35 (config's disable / label, the analog capture), so gpio / capture / capture_group skip and uart runs on the jig's settings plan |
| `esp32-series-30eda0e31108` X035 jig (ESP32-P4), `esp32-series-30eda0e343c6` second P4 | esp32p4 | esp32p4 | USB DFU 1.1 of the app `.bin` to the running probe (pyusb, what `dfu-util -D` does: the DFU interface found by class FE/01, wTransferSize from its functional descriptor, DNLOAD blocks, a zero-length DNLOAD to manifest), wait for the device to drop and return; on WSL `usbipd.exe attach --wsl --busid <busid>` (the table's `usbip_busid`) | **yes** (X035 jig, 0.0.26 main build, 2026-10-02: DFU on interface 4 / 4096 B in 8 s; wire over RVSWD 50 loops pass; the uart test runs on the jig's settings plan rx 12 / tx 6). The second P4 was flashed by hand over DFU (interface 7 / 1024 B on its old firmware); its new USB serial needs a Windows usbipd re-bind before a run |
| `esp32-series-30eda0ea068b` third P4 (ESP32-P4, FS USB-Serial/JTAG port only; ours): a CH32V003 on SWIO 19, NRST 4, powered from GPIO5 (its settings keep idle 5 output-high) | esp32p4-usj | esp32p4 | through that port: `esptool --chip esp32p4 -p <port> erase-region 0xe000 0x2000` (otadata: boot app0), then `write-flash 0x10000 <app.bin>` - the app alone, as DFU writes it, so the saved settings (the target's power) stay | **yes** (main 0f013b7, 2026-10-02: `OEP_HW_TARGET=ch32v003@19 OEP_HW_RESET=4 OEP_HW_ANALOG=20`; 13 passed, port_speed skipped. The V003 answers nothing for about 0.1 s after the probe's boot powers it: the wire / console scans retry for up to 2 s) |
| `9489dd2ae0953650` SparkFun Pro Micro RP2350 (1209:4F45, keyed by unit id) | rp2 | promicrorp2350 | the 1200-baud touch on the CDC port (BOOTSEL), wait for the boot ROM's USB device (`RP2350 Boot` / `RP2 Boot`, matched by product and serial so another RP2 on the host is never touched), `picotool load -x <uf2> --bus --address` (`OEP_HW_PICOTOOL`, default `picotool` on PATH; [picotool 2.3.1 Linux x86_64](https://github.com/raspberrypi/pico-sdk-tools/releases/download/v2.3.1-0/picotool-2.3.1-x86_64-lin.tar.gz)), wait up to 45 s for the CDC port to return. With `OEP_HW_UF2_DRIVE=<mount>` the `.uf2` is copied to that BOOTSEL drive instead (a host that mounts it); neither possible → the flash is skipped and the tests run on the firmware already there | **yes** (picotool, 2026-10-02; the first run froze the probe at `uart` on pins its UART cannot use - fixed in oep-probe-arduino 654b06a; the run with that build passes) |

The ATOM's bridge (a CH552 posing as FTDI) loses probe→host frames in bursts (0 % one minute, 10-55 % the next, 2026-10-02): a port_speed failure there is re-run once before it counts; the CH340 jig is the steadier gate for port_speed.

**TCP boards.** `esp32-pico-d4-50029191fe34-tcp` is the ATOM over Wi-Fi / TCP (kind `tcp`, port `tcp:50029191fe34` -
DNS-SD by unit_id - or `OEP_HW_ATOM_TCP=tcp://HOST:PORT`), and `OEP_HW_BOARDS` may also name any probe on the network as
`tcp:<unit_id>` or `tcp://<host>[:<port>]` (a board of kind `tcp` without a table entry: no model checked, no free
channels unless `OEP_HW_GPIO` / `OEP_HW_UART` / `OEP_HW_DISABLE` give them). A TCP board is never flashed (`flash`
records "a TCP board"): flash it and set its networks (`OEP_WIFI_*`, the `wifi` test) on a run through its serial / USB
entry first. port_speed skips (no UART bridge on this link); `linktest` waits 3 s an answer over TCP. mDNS does not cross
WSL 2's default NAT: from there name the board by address (its ip: `oep config state` over the bridge).

`OEP_HW_NOFLASH=1` skips flashing on every board and tests the firmware found there (recorded as "on-board"; the firmware
string is recorded, not compared).

The `rp2040` / `rp2350` (Pico) profiles use the same rp2 flasher; a board entry is all they need.

## Shared jigs: the permission rule

The V003 jig, the X035 jig (ESP32-P4) and the WCH-Links on this host belong to the ArduinoCore-CH32RV bench (another
session's hardware). The board table has them so that a release test can cover them, but **running on a jig waits for
the bench's permission**: ask first, every time; never flash or reset a device you were not given. The ATOM
(`/dev/ttyUSB1`) and the second P4 (`esp32-series-30eda0e343c6`) are free for OEP tests. A `jig` in a board's `notes` marks the shared ones
(`Board.shared`).

## Environment

| Variable | Meaning |
|---|---|
| `OEP_HW_BOARDS` | comma list of board ids (required; nothing runs without it); `tcp:<unit_id>` / `tcp://<host>[:<port>]` names a TCP probe without a table entry |
| `OEP_WIFI_SSID_<n>`, `OEP_WIFI_PASS_<n>` | the bench's Wi-Fi networks for the `wifi` test (n = the entry's index; no PASS: an open network). Read from the environment, never printed, recorded as `(set)`. The same variables feed `oep config wifi <probe> --from-env` |
| `OEP_HW_WIFI_WAIT_S` | how long the `wifi` test waits for the link (default 30) |
| `OEP_HW_ATOM_TCP` | the ATOM's TCP target for `esp32-pico-d4-50029191fe34-tcp` (default `tcp:50029191fe34`, by DNS-SD) |
| `OEP_PROBE_DIR` / `OEP_PROBE_VERSION` / `OEP_HW_NOFLASH` | the firmware source (one of them) |
| `OEP_HW_TARGET`, `OEP_HW_RESET`, `OEP_HW_TARGET_ADDR`, `OEP_HW_LOOPS` | the wire test: a target is wired (and where), its reset line, the block address, the loop count |
| `OEP_HW_REOPEN_S` | seconds more to open a restarted probe again once its restart_max_ms has passed (the user's reopen, above; WSL / usbipd). Default 0: none - the probe is gone after restart_max_ms |
| `OEP_HW_RESTART=1` | the config test reboots a USB probe (P4, RP2) through `oep.probe.restart` (default: not - a restart left an RP2350 and a P4 failing enumeration until a replug with oep-probe-arduino 0.0.29-dev+3c0cd99) |
| `OEP_HW_WIRE` | the wire / console tests' wire by name (default: `oep.wire.swio` when `OEP_HW_TARGET` names one pin and the probe offers it, else the first wire); the console test uses the `oep.target.console` instance whose open takes that connection |
| `OEP_HW_GPIO`, `OEP_HW_DISABLE`, `OEP_HW_UART`, `OEP_HW_UART_LOOP` | channel overrides for the fixture tests |
| `OEP_HW_ANALOG`, `OEP_HW_I2C`, `OEP_HW_SPI` | the analog capture's channel, the i2c-target's `sda,scl`, the spi-target's `sck,mosi,miso,cs` (default: the table's free channels - gpio pair, disable, UART pair - that the describe allows) |
| `OEP_HW_RATES`, `OEP_HW_FRAMES`, `OEP_HW_LT_TIMEOUT`, `OEP_HW_ERROR_MAX` | the port_speed test's candidates, frames a cell, answer wait, the verdict's floor (default 5 %) |
| `OEP_HW_PICOTOOL`, `OEP_HW_UF2_DRIVE`, `OEP_HW_USBIP_BUSID`, `OEP_HW_CACHE` | tool and platform details |

Tools: `arduino-cli` (a local build), `esptool` (classic ESP32), `picotool` (RP2); pyusb for DFU and the boot ROM lookup
(a dependency of the client already). No `gh`, `dfu-util` or mounted drive is needed.
