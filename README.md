# OEP Python client

[日本語](README.ja.md)

The host side of Open Embedded Probe. It speaks v1 (oep-spec's core `docs/oep-core.ja.md`, `docs/oep-transports.ja.md` and the interfaces `interfaces/*.ja.md`). Numbers are taken from `oep_client.registry`, a straight copy of oep-spec's
`generated/oep-v1/oep_v1_registry.py`.

**Spec implemented: the oep-spec commit in `tests/vectors/SPEC_COMMIT`** (oep-spec versioning §2 / §6: before the freeze, revision 1 alone
does not fix the shapes, so an implementation names the commit of the spec it implements). The number tables and the shared vectors in `tests/vectors/` are copied from that commit.
The history of changes is in [CHANGELOG.md](CHANGELOG.md).

Until the freeze, the Japanese text (`.ja.md`) is the working text of the spec; the English documents are made from it at the freeze, and from then on the English is authoritative (until then oep-spec carries no English translation). Readers new to OEP start with oep-spec's
[README](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/README.ja.md) and the [review guide](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/review-guide.ja.md) (what is written where).
[Getting started](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/getting-started.ja.md) builds a minimal probe and host,
[docs/oep-core.ja.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/oep-core.ja.md) is the body of the protocol, and [docs/conformance.ja.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/conformance.ja.md)
states what host conformance needs.

It follows OEP's division of work that target knowledge lives in the host. The probe knows only wires and DMI / DP·AP transfers; the CH32 flash
controller, RAM loaders, the RP2350 boot ROM, the Cortex-M debug registers and so on live here.

```sh
pip install oep-client-python     # PyPI (import oep_client); a checkout: pip install -e <checkout>
uv run pytest                                                            # in a checkout (virtual bench; no hardware)
OEP_HW_BOARDS=<board id> OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw   # hardware: tests/hw/README.md
```

`import oep_client` is all it takes. The number tables and oep-spec's test vectors (`tests/vectors/*.json`; `tests/test_spec_vectors.py` matches this client and
the virtual bench against them) are copied from a named oep-spec checkout with `OEP_SPEC_DIR=<checkout> tools/sync_registry.sh` (`OEP_SPEC_REF` chooses
a commit other than HEAD; the commit and the file hashes stay in `tests/vectors/SPEC_COMMIT` / `SPEC_SHA256`). `oep-client` on PyPI is a different project, so the distribution name is `oep-client-python`.

Releases are the GitHub Actions Release (workflow_dispatch, version = X.Y.Z or X.Y.ZbN): `tools/prepare_release.py` rewrites
pyproject.toml, uv.lock and `oep_client.__version__`, turns CHANGELOG.md's Unreleased into that version, and after the tests and build it commits and tags,
and publishes the GitHub Release and to PyPI (Trusted Publishing). Add changes to CHANGELOG.md's Unreleased in (EN) / (JA) beforehand.

The minimal setup check is the [setup check steps](docs/hardware-quickstart.md). See also the common [testing policy](docs/testing-policy.md) and the [guide for projects that use OEP](docs/testing-consumers.md). Equipment settings are in the [configuration design](docs/hardware-configuration.md) and the [settings format and templates](docs/hardware-schema.md).

## Modules (`oep_client`)

The modules in the table below are the public API. Import them directly (`from oep_client import riscv`). Anything not in the table, and names starting with `_`, may
change. Compatibility between probe and client is checked by the SPEC version implemented, protocol/interface revision, the needed declared features and integration tests. The firmware and client versions are each recorded, and compatibility is not judged by the same version number alone. Until v1 is frozen, the wire can change, so the corresponding SPEC and the combinations confirmed are stated explicitly.

| Module | Content |
|---|---|
| `host` | Requests and results (one 10-byte header: a request sent within a session carries that session's id; a lock-free request outside a session, 0. corr wraps without using 0, one number shared by every session_id), session_id and the lock, `call()` (an exception on failure), pipeline, the error hierarchy (`OepError` / `Rejected` / `Failed`. When the session has ended (end, lease expiry, force by another session), `NoSession`: the probe has released everything that session made. There is no resume; it does not silently open again, `Host.session` becomes None, and `Host.epoch` advances. `NoResource` for connection or stream numbers the probe does not know. A probe whose confirm is outside the range of core §7.1, or whose `max_op_ms` is outside 1 to 600000, is `NotUsable`, and nothing more is sent. `Unavailable` reads the payload's cause, channel and `fn` (the fn the refusal concerns)). `open(lease_ms, force=, owner=)` always opens a new session with a new random id and returns `Opened(lease_ms, boot_id)`. `end()` releases everything, and the host leaves the session. When boot_id changes (confirm, clock, open), the name → fn cache is dropped and list is read again. `clock()` returns fn 0's clock (core §7.6; needs neither lock nor session) as `ClockReading(before_ns, after_ns, uptime_ns, boot_id, round_trip_ns)`: this host's time just before sending the request and just after receiving the response (`time.monotonic_ns`, or `now=`), and the uptime the probe read in between. `.host_ns` is the midpoint, `.uncertainty_ns` half the round trip (host guide §12). `clock_best(n=8)` keeps the one with the shortest round trip of n. `subscribe(fn, min_bytes, max_delay_ms)` / `unsubscribe(fn)` send that interface's own op 0x01 / 0x02 (core §10.3. min_bytes / max_delay_ms batch only data; events come at once. An fn that sends no notifications answers unknown_operation. fn 0 sends no notifications, so `subscribe(0)` raises ValueError before sending). `restart_probe(wait_s=None, reopen_s=None)` restarts the probe with `oep.probe.restart` found by name (oep-if-restart, host guide §5.2. LookupError if the probe does not offer it. The session must hold the lock): after the response, the link closes, waits a little (about 100 ms, `host.RESTART_AFTER_ANSWER_S`), and reopens the same way as a new open (confirm first. A serial port at the boot speed, a USB device searched for again after re-enumeration, TCP reconnected), repeating for `wait_s`. The default is the probe's restart_max_ms (from oep.probe.restart's describe, read before the restart; the limit on the probe's own restart only; `core.restart_max_ms`) plus `host.RESTART_EXTERNAL_S` (5 s) for re-enumeration and reconnection, or 10 s without a declaration. When it passes, the probe is treated as gone, the link is closed, and ConnectionError / TimeoutError. When it comes back, the new boot_id is returned. `reopen_s` asks in advance for the user to reopen a probe so treated as gone: it reopens the same way as a new open for up to that many more seconds (for hosts where the device comes back with a delay the probe cannot know - on WSL, usbipd re-attaches a re-enumerated device. Whether it was needed is in `Host.restart_reopened`). If boot_id is the same as before, `NotRestarted`. When the response was lost and the resend was refused with no_session by the restarted probe, it is also treated as done. `request_restart()` only sends the request |
| `link` | transport: serial ports (always COBS + CRC, `0x00 <COBS> 0x00`, anything outside frames dropped as noise, opened exclusively, 8N1 with DTR / RTS raised), USB vendor bulk / HID and TCP (length-prefixed frames, transports §5 reframing: longer than probe_frame_gap_ms since the host's last write, wait 250 ms. On TCP, a pause in the middle of a frame is read through), matching by corr and resending (on a serial port the session holds, when a broken frame arrives while waiting for an answer, it resends at once with the same corr - up to `BROKEN_RESENDS` = 3 times while frames keep arriving, host guide §8. Once after a wait in which nothing came. At a raised port_speed, first resend at that speed, below). If the resend also gets no response, it raises `TransportFailed`, and before the next request it recovers with confirm (waits for the input to go quiet, then a confirm with its own corr; on serial ports too). If it cannot recover, ConnectionError. Every response is waited for at least the lower bound of core §4.4 (the request's processing limit + 1000 ms + the serial port's transfer time. The processing limit is max_op_ms; fn 0 confirm / list / describe / clock / keepalive / lock_state up to 1000 ms; 1000 ms before max_op_ms is read; if an argument's wait is longer, that length (`Host.op_bound_ms`). The transfer time is counted with min_max_frame until the confirm response comes, `wait_floor_s`). `Host.open` reads fn 0's describe first. The confirm and the return of the port_speed procedure use the link's own short waits. A short response is a broken frame. `open_host(target)` |
| `kept_session` | The session id the host keeps per probe for the next run (`KeptSession`, `Host.kept`. Placed by open_host. Below) |
| `discovery` | Network probes: mDNS DNS-SD `_oep._tcp` (transports §3) - `browse(timeout)` -> `Found(instance, unit_id, host, port, addresses)`, `verify(found)` / `check(f)` (confirm and describe's unit_id, host guide §4.1), `find_unit(unit_id)` (checked), `port_of(host)`. python-zeroconf if present (the `mdns` extra), else its own minimal query (below) |
| `core` | Finding interfaces by name (cached), confirm (with the probe's `boot_id` and `transport`, the number of the transport this host came by; from the second time it asks for the revision in use), the probe's describe (declarations only; cached for the boot: labels, the transport list, `max_op_ms`), the `ops` of any fn (the describe ops tag, core §7.4 - base and a bitmap of 1 byte or more not going past op 0xFF - its shape is checked: an fn that breaks it is not used (`UnusableFunction`); if it is fn 0's, the probe is not used (`NotUsable`). `ops(hst, fn)`, `offers(hst, fn, op)`, `Interface.offers(op)`. `require` / `not_offered` raise, without sending, the same `Rejected` (detail unknown_operation) the probe would answer), how to take the lock (`take`), pin assignment with `oep.probe.plan` (`plan_fn`, `plan_apply`, `plan_release`, `plan_roles`), `oep.probe.restart` (`restart_fn`, `restart_max_ms`), the base of `Interface`. `list_entries(hst, name, exact)` reads list forward with first only and keeps matching names on the host (core §7.2). fn 0 list entries are dropped (the core is not on list). `max_op_ms` (`FALLBACK_MAX_OP_MS` 10000 for a probe without the declaration). `oep.probe.link` (oep-if-link): `link_fn`, `link_speed`, parts of the source / sink requests and responses (`link_size` = source response max_frame − 7, `link_sink_size` = sink request max_frame − 12) |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio` (scan: the host makes the candidate pairs (`candidate_pairs()`); attach: sends method, the pin pair and `max_speed` in the fixed part, always a new connection (pins in use are unavailable), `reset=(channel, hold_ms)` attaches while applying reset, method 1 (halt) only when features has attach_halt; detach; connections (connection is u32)), `oep.target.riscv-dm` (the response carries the number of values. `reset()` -> `(flags, pc)` (mode 0; flags bit0 reached), `reset_halt()` -> dpc. `RunResult.not_halted`, `RunResult.not_run` (preparation failed and the hart was not run). A step that could not halt the hart again is a `StepError` with `step_left`. `read_register` / `write_register` keep DATA0 as it was (`data_saved`), and `resume` / `step` / `run` write it back first (`restore_data`, debug §4)), `RiscvDm.declared()` (the optional ops the probe offers in the ops tag; others are unknown_operation), after a high-level op whose cleanup failed (step_left, restoring block's DATA) the connection closes, the target_id scheme is `dmi_7f` (`Wire.SCHEME_DMI_7F`), attach, scan and riscv-dm reset wait max_op_ms as the argument's time (`attach_ms`, `scan_ms`, `reset_ms`; debug §1, §4.3), searching for the reset line (`find_reset_line(candidates, pins=...)`), attach through GPIO |
| `targets` | What the host knows per target family, in one table (`FAMILIES`: wires, target_id matching, the reset vector, the function that reads NRST from option, max_speed / idle_clock). `identify(target_id)` |
| `pins` | `oep pins`: classifying channels, searching while holding low, scan, identification, confirming the reset line, proposing connection profiles and labels (`PinFinder`) |
| `console` | `oep.target.console` (positioned streams: the read response carries a length, and marks carry `time_ns` and the common kinds lost / clear / host, and the console's reset 0x40 / restart 0x41. The lock-free `streams()` lists stream, connection, mechanism. `open(conn, mechanism)` always creates a new stream (u32) on that connection - a second one on a connection with a live stream is unavailable cause 6 -. A stream belongs to the session, and is released with its buffer on close, on release of the connection and at session end; its number is no_resource after that. write goes into the stream's send queue - the probe decides the queue size and does not declare it -. accepted = the smaller of count and the queue's free space, and 0 only when the queue is full. write to an SDI stream is unsupported. marks, clear, mark and write are optional) and `ConsoleIO`, which reads as a byte string and writes at most one frame at a time, continuing from the response's accepted (`stall_s`: TimeoutError if nothing is received for that long) |
| `fixture` | `oep.fixture.gpio` (`set([(ch, mode)])` applies in order in one request, `read(channels)` returns levels. The output strength is the probe's own, with no way to choose) / `uart` (the plan creates the stream. Nothing is received until `configure(baud, fmt)`. `status()` = the `UartStatus(baud, format)` in effect; without configure, baud 0 and format 0. A configure the probe could not bring up raises `Failed` and returns to the unconfigured state) / `i2c-target` (one shape: `configure(address)` - rebuilds the target -, one controller write with data is one frame of `read_rx`, a read is answered from a `preload_tx(data)` slot or 0xFF, `status()` = `I2cStatus(state, queued, rx_frames, tx_slots, errors)`, `stretch` if in ops, `internal_pullups`: whether to enable its own pull-ups. Reserved addresses 0x00 to 0x07 / 0x78 to 0x7F are refused before sending) / `spi-target` (there is no reset; configure again. `cs_setup_ns`: the time after CS until the first bit is valid. `oep dump` prints it) |
| `settings` | The core's settings (oep-spec core §13): fn 0 get / set / unset / save / state / network. `Settings(hst)`: `catalog()` = the names of that boot (each fn's category `core` / `name/instance`, items, choices, the names of channels, transports and netifs), `show()` = all values as (category, flat name, value) (there is no origin. Firmware defaults are like any other value, core §13.4), `changeable()` (whether set / unset exist), `set_named(category, {flat name: value})` / `unset_named` / `unset_all`, `set` / `unset` by number, `save()`, `state()`, `network()`. `export()` / `plan_import(doc)` / `apply_import(plan)` = the settings file (oep-spec docs/oep-settings-file.ja.md; secrets are not read). `find_line(source, "nrst", port=None)` = the way to find a wire in host guide §5.3 (wire N of port S is label `S.N`, ASCII case-insensitive; with the port omitted, only when exactly one port is named), `ports_named(labels)` gives port names as written |
| `capture` | `oep.fixture.logic` / `analog` / `capture-group` (revision 1, oep-spec's oep-if-capture). configure's TLVs follow core §2.3: a value the probe cannot keep is refused with unsupported and the tag as sent (table TLVs are sent without critical). The response is the actual values, and samples is correct from it too (analog: scale is always present per role, and an omitted skew is read as 0). `start()` returns a new generation each time (`LogicCapture.generation`. Not reused within a boot; a start after 0xFFFFFFFF is refused until restart). read and release carry that generation (`read_segment(segment)` carries the segment's own generation), and every event carries a generation (`events()` drops those of earlier starts). Streaming data frames carry the generation and their segment, and do not cross segments (segment_info, `Received.segments`). `status()` returns `Status`. capture-group binds configured tracks of the same mode with an immediate trigger and starts them at once (`CaptureGroup.start` -> the group's start_ns; the generations of the group and each track). Segments read are passed as `CaptureRecord` to the `Host.on_capture` callback (a receiving point for records; no dependency on wireskein) |
| `registry` | A module generated from oep-spec's number tables (do not edit; copy again from oep-spec). The public entry point for looking up interface numbers by name is `registry.INTERFACES[name]` (`.revision`, `.op`, `.tlv`, `.enum`; e.g. `INTERFACES["oep.fixture.uart"].enum["role"]`). Module names such as `FIXTURE_UART` are the same thing |
| `arm` | `oep.wire.swd`, `oep.target.arm-adi`, MEM-AP, Cortex-M halt and function calls |
| `ch32_flash` | CH32 writing (RAM loader, page-wise rewriting) |
| `pytest_plugin` | The OEP plugin for pytest-embedded-arduino-cli. Loaded automatically with the `pytest` extra. The probe lock `probe:<unit_id>`, the port record `port_memo` (`OEP_PORT_MEMO_*` in `.env`, else the probe's memo area), the `oep://` console, SWD writing of RP2040 / RP2350 boards, the session `oep_host` and UART `oep_uart` ([docs/pytest.md](docs/pytest.md)) |
| `brokers` | Finds the broker that handles a probe (`OEP_BROKER`, the user's `oep/brokers/<unit_id>.json`) and gets a share of the lock with `oep.probe.share` (OEP host guide §7) |
| `ide` | Receives the Arduino IDE's network upload and writes through an OEP probe. Also has a gdb server ([docs/swd.md](docs/swd.md)) |
| `gdb_server` | Answers the gdb remote protocol for a Cortex-M on OEP's SWD (registers, memory, run/halt/step, FPB breakpoints, load to flash) |
| `rp2350` | flash and reboot through the RP2350 boot ROM |
| `uiapduino` | Entering and leaving the UIAPduino bootloader |
| `catalog` / `names` / `interfaces` / `dump` | The list of capabilities, the shape of describe, display |
| `virtual_bench` / `endpoint` / `virtual_bench_serial` / `virtual_bench_serve` / `virtual_bench_mdns` | The virtual bench ("Virtual bench" below) |

## Usage example

```python
from oep_client import core, link, riscv, ch32_flash

hst = link.open_host("/dev/ttyACM0")   # with pipelining
# an OEP address "oep://<unit id>[?via=usb|bulk|hid|cdc|net|tcp:HOST:PORT|tty:PATH]" (host guide §5.3; without via
# its USB device, else the probe DNS-SD finds; describe's unit_id must match on any route), or, when the unit id is not
# known, a serial port (always COBS), "tcp://127.0.0.1:PORT" (a broker), "usb" (every device on the project's VID:PID
# 1209:4F45, each counted once: exactly one -> vendor, then HID, else its CDC port; several -> link.SeveralProbes lists
# them, name one). Each is probed first with a
# confirm only (transports §3): no valid answer -> closed, link.NotOepProbe
core.take(hst, 30000, owner="flash script")   # the only way in: force; else wait out the lease, name the holder
wire = riscv.Wire(hst, "oep.wire.rvswd")
conn, _ = wire.attach(halt=True)
dm = riscv.RiscvDm(hst, conn)
dm.reset_halt()
result = ch32_flash.program(hst, dm, open("sketch.bin", "rb").read(), ch32_flash.PROFILES["x035"])
dm.reset()                                    # ndmreset and run -> (flags, pc)
wire.detach(conn)
hst.end()
```

## Writing and debugging an SWD board

An Arm board (RP2040, RP2350) connected to an OEP probe's SWD is written, run and inspected from the Arduino IDE's "Upload" and "Debug", or
directly from the OEP host. The steps are in [docs/swd.md](docs/swd.md).

## USB permissions on Linux (udev)

A probe enumerates with the project's USB VID:PID `1209:4F45` (transports §3). Opening its vendor bulk (libusb) or HID (hidraw) as
an ordinary user needs a udev rule: [`udev/70-oep-probe.rules`](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/udev/70-oep-probe.rules) (in the checkout and the sdist) lets the logged-in
user (`uaccess`) and the `plugdev` group open the USB device and its hidraw. Installing it needs administrator rights:

```sh
sudo install -m 0644 udev/70-oep-probe.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # or unplug and replug the probe
```

The probe's CDC port (`/dev/ttyACM*`) needs no rule beyond the usual `dialout` group. Windows and macOS need no rule.

## `oep` commands

```sh
oep dump --port <probe>                      # the list of capabilities (--virtual p4-x035 without hardware)
oep settings show <probe>                    # all values, and the saved state
oep settings items <probe>                   # the items each owner declares (name, shape, choices)
oep settings set <probe> core channel.GPIO23.label=v003.nrst channel.GPIO20.idle=pull-up [--save]
oep settings unset <probe> core channel.GPIO20.idle [--save]     # --all for everything (with --save afterwards, the storage becomes empty too)
oep settings save <probe>                    # keep across restarts
oep settings state <probe>                   # the saved state (no lock needed)
oep settings network <probe>                 # the current network: the TCP port, each netif's state and address (no lock needed)
oep settings export <probe> jig.oep-settings.json                # all values by name, plus the memo area's objects (secrets only as present)
oep settings import <probe> jig.oep-settings.json [--save]       # shows what changes, then after confirmation set / unset and rewrites the memo area
oep speed <probe> [921600,500000] [--verify] [--flows out:2]  # port_speed: speeds up a UART bridge (default 500000) and prints the result (below)
oep pins <probe> --power 5 --wire swio       # how the target is connected: debug pins, the reset line, labels (below)
oep clock <probe> [-n 8] [--json]            # fn 0's clock: uptime_ns and boot_id aligned to this host's time (the shortest round trip of n)
oep restart <probe> [--reopen-s S]           # oep.probe.restart: restarts the probe and waits until it is back (a new boot_id).
                                             # --reopen-s: when it is not back within restart_max_ms (WSL / usbipd), reopen for up to S more seconds
oep find [--timeout 2] [--json] [--no-verify]  # probes that advertise _oep._tcp (checked): unit_id, host, port, addresses
oep linktest <probe> ...                     # --timeout: default 0.3 seconds per response, 3 seconds on TCP
oep call <probe> io.github.you.thing 0x10 a=1 b=0x2   # an op of any interface: by number and hex request, or by describe's names and fields
                                             # (core §7.4 op_name / op_field). --text, --wait S (waits for a job with status), --json
```

`<probe>` is an OEP address `oep://UNIT_ID[?via=transport]` (oep-spec host guide §5.3. Without via, the USB device; if none, the probe
DNS-SD found. On any transport, if describe's unit_id is not the same, it closes). When the unit_id is not known, a serial port,
`tcp://HOST:PORT`, `tcp://HOST` (the port DNS-SD found on that host) or `usb` (the only probe) also work. Changes take the lock (owner "oep settings"), and when done
close the session. Changes take effect at once, and after `save` they survive a restart.

### A host that runs again: the kept session id

A probe keeps the session even when the transport closes (transports §3), so a command that was killed, crashed or lost its wire leaves the lock and
what it held (connections, plans, subscriptions) until the lease expires. So `link.open_host` keeps the id of the session the host opened in a per-probe file
(`kept_session.KeptSession` in `Host.kept`; host guide §5), and before the host's first open, ends the session the previous run left:
it opens with that id (an open of the session that holds the lock keeps that session; core §6.2: nothing is released) and ends it at once. Then it opens its own session.
These 2 requests are sent with a corr ahead of any corr the previous run sent (core §5.1): the file keeps, together with the id, the corr that run reserved ahead
(reserved again 1024 at a time), and the next run continues from beyond it. With a file without it, nothing is ended.
The `oep` commands and tests/hw open with `open_host`, so this happens. Other long-running hosts pass `keep_session=True` (the default), or put
`hst.kept = kept_session.KeptSession()` on a Host they built themselves.

- The file is `<dir>/<unit_id>.session` (the id and the reserved corr in hex, `ssssssss cccc`; empty after `end`). It is keyed by fn 0's unit_id, so other ports or
  transports of the same probe use the same file. `<dir>` is `$OEP_SESSION_DIR`, else `$XDG_RUNTIME_DIR/oep-client`, else
  `%LOCALAPPDATA%\oep-client\sessions` (Windows) or `${XDG_CACHE_HOME:-~/.cache}/oep-client/sessions`.
- While the host holds the file, it puts an exclusive lock (flock / msvcrt.locking) on it, released when the link closes or the process ends.
  It does not touch a file another running host holds: that host's session belongs to that host, and this host opens normally (it meets that
  lock, and `core.take` waits or shows the holder). A unit_id starting with `x-` does not name a unit, so nothing is kept.
- `Host.end_previous(sid)` is this procedure itself: True if it ended that session, False if another session holds the lock (not something this host
  ends). `keep_session=False` reads and writes nothing.

### TCP

A probe that carries OEP over TCP is found with DNS-SD (network settings belong to the core: transports §7).

- `oep find` browses `_oep._tcp` (transports §3, host guide §4.1) and lists unit_id, host, port and addresses. The service name
  `oep` is not a registered name, so another service may advertise `_oep._tcp`: every instance is checked first (`discovery.verify`,
  all at the same time, 1 second per answer, `--verify-timeout`). It connects over TCP, sends confirm (answered `OEP!`) and fn 0's describe, and
  keeps only those whose describe unit_id is the same as the TXT unit_id. No session is opened. Those that cannot be checked are dropped with a warning on
  stderr naming the destination and what it said. `--no-verify` lists everything advertised as is. `discovery.find_unit` (that is, `oep://UNIT_ID?via=net`) also
  checks the same way and skips instances that cannot be checked. After that, `open_host` checks confirm and describe's unit_id once more on the transport it opened.
  The port is always the SRV
  one (there is no fixed port; the reference probe's 7450 is an example). It searches with python-zeroconf if present (`pip install 'oep-client-python[mdns]'`),
  else with this package's minimal query (QU, receiving answers on a temporary port, and also listening on 5353 if it can be shared). python-zeroconf uses
  all interfaces (InterfaceChoice.All), and the minimal query also sends from each IPv4 interface (IP_MULTICAST_IF per interface
  address. `discovery.interface_addresses`: ifaddr if present; otherwise on Linux each interface's addresses,
  elsewhere the host name's addresses). One send goes out of only one interface (on Windows often a virtual adapter) and does not reach probes on another adapter. mDNS works only within the same
  link: it does not find probes from behind WSL 2's default NAT, a VM or a router. In that case name it with `tcp://HOST:PORT`.
- `oep://UNIT_ID?via=net` opens the probe DNS-SD found with that unit_id and checks describe's unit_id (if different, it closes with UnitIdMismatch).
  After a restart it browses again (the address can change). TCP has no authentication: use it within a trusted network or a tunnel (transports §1).
- `oep linktest` waits 3 seconds for a response on TCP (0.3 seconds otherwise): Wi-Fi retransmissions sometimes stall it for 1 to 4 seconds, and the answer still comes.

## Finding pins (`oep pins`)

`oep pins <probe> [--power CH] [--exclude CH,...] [--wire swio|rvswd|swd] [--pairs D:C,...] [--all-pairs] [--budget S]
[--steps ...] [--name port] [--save] [--json]` finds where a target is connected, on a probe where the host chooses the pins. Each step prints what it
did, the whole finishes within `--budget` (default 55 seconds), and at the end every plan is undone. The procedure is this client's own (oep-spec host guide §5.3 decides only how to find a wire by label). What each target
family needs (wires, the reset vector, how to read the option that tells whether there is a reset line, max_speed / idle_clock, the GPIO layout map reads and the debug
pins, GPIOs connected to one lead inside the package, whether there is an NRST to search for) is collected in one table, `targets.FAMILIES`.

1. **classify**: reads every channel `oep.fixture.gpio` allows 16 times each, under pull-up then pull-down:
   floating (1 under pull-up, 0 under pull-down. A pull weaker than the probe's, such as the weak pull-up of a reset line, also reads this way),
   driven-high / driven-low (the same level under either pull: push-pull or a strong pull. An idle-high UART wire looks like this), active
   (changes between reads). With `--power CH`, it first reads with the target's power off, and channels that read differently follow the power. Each only gives candidates.
2. **hold**: holds the floating channels low one at a time with open drain and watches the active channels. If they stop, it is a reset line candidate.
3. **scan**: scans the wire on floating channels (rvswd / swd in pairs, nearest pairs first. Fixtures often place the 2 debug wires next to each other).
   It goes on until there is an answer or the time is used up. `--all-pairs` goes on to the last pair (a second target becomes visible), and `--pairs` gives the pairs to try.
   Channels driven low are also tried as SWDIO (after the floating pairs at the same distance): a target that has been talked to may hold SWDIO low.
   If nothing answers, it reads the wires again and scans newly allowed pairs. If not all pairs were tried, the report says so (`unique-in-partial`,
   `not-finished`). driven-high / active channels, the power channel and `--exclude` are not scanned.
4. **identify**: attaches (halt) to the wire that answered and tells the family from target_id. For CH32V00x, the option bytes tell whether there is an NRST (read only,
   not written). Then resume.
5. **map**: for families whose GPIO layout is in `targets.FAMILIES`, it shows which target pin connects to each channel: it halts the hart, makes the pins other than the debug
   pins floating inputs, outputs high then low on each channel and reads the target's INDR (twice). Channels that were driven in classify are read
   again first (they may have been the target's outputs), and only those floating then are driven. The power channel, the reset candidates and the 2 debug wires are
   not driven. A pin that follows 2 channels is tried again with the others held low (a wire that nothing drives keeps the charge it picked up from a change on the neighboring wire).
   GPIOs connected to one lead inside the package are shown together (`PC16/PC11`). The target's registers are restored, and it runs again.
   Except for targets whose option bytes show no NRST, the reset of 6 is done first (driving an unknown NRST low resets the target and
   cuts the debug connection). Families with pins that must not be driven (the VIO18 pins of CH32H417 are 1.2 V at boot) are not mapped.
6. **reset**: attaches while applying reset with each candidate. On the real wire, the hart stops at the reset vector (skipped for families with no NRST
   to search for).
7. **labels**: recommends the connection profile the host keeps (wire, pins, wire settings. The probe keeps no wiring), and the labels `<port>.swdio` / `<port>.swclk`
   (or `<port>.swio`), `<port>.nrst` / `<port>.power_hi` (with these, the target can be opened as `oep://<unit_id>/<port>`). Where known, the connected
   target pin is added (`<port>.nrst@pa21`; host guide §5.3): the debug and NRST pins from `targets.FAMILIES`, the others from map.
   A pin with no role gets `<port>@pb6`, and a wire whose port label has a role (such as `rx`) keeps it
   (the port is `--name`; otherwise, when the probe's labels name exactly one port, that port. If neither, a note asks for `--name`). Labels are checked against the probe's settings
   with `settings.find_line` (if another channel already has that name, the wire can no longer be found, so a note says so).
   `--save` writes the labels (set + save). Without `--save`, nothing is written.

Safety: driven-high / active channels are never driven, scanned or held (driven-low only as SWDIO in scan). map drives channels only while the target's pins are
floating inputs. The power channel is touched only with `--power`. Holding low is
open drain only. When the gpio plan is renewed, the probe releases the pins of the previous plan (the power channel too) for a moment, so with `--power`
it cleanly powers up again with each new plan (`power_cycles` in the report).

Example (ESP32-P4 and CH32V003, power on GPIO5):

```
$ oep pins /dev/ttyACM0 --power 5 --wire swio --name v003
power: channel 5 low 300 ms (target off: read), then high 400 ms before the reads
classify: 52 channels, 16 reads each under pull-up, pull-down
  floating     0-3,6,9-20,26-34,36-50,52-54
  driven-high  7-8,22-23,35  (never scanned or held)
  driven-low   51  (never scanned or held)
  active       21  (21: 8 changes under pull-up)
  follow power 4,6,9-11,13,15-16,19-23,32-33  (read otherwise with the target off: wired to it; candidates only)
reset line (hold low): 45 candidates, each held low (open drain) up to 390 ms while watching 21 (105 changes in 1.0 s running, longest lull 130 ms)
  hold 4 low: 21 stopped
  45 held in 3.4 s: stopped by 4
scan swio: 45 channels (0-4,6,9-20,26-34,36-50,52-54; not 7-8,21-23,35,51: driven / active) in 0.14 s -> 19
attach swio 19: target_id 00310510 (WCH DMI 0x7F) -> ch32v00x, halted at dpc 0x108
  option bytes (read only): RST_MODE 10 (USER 0xf7): NRST on PD7, 12 ms ignore window
  attach under reset through 4 (held 20 ms): dpc 0x0 -> the reset line
profile: wire swio, pins 19   (the host's to keep: the probe keeps no wiring)
label: oep settings set ... core channel.GPIO4.label=v003.nrst   (not written)
label: oep settings set ... core channel.GPIO5.label=v003.power_hi   (not written)
idle:  oep settings set ... core channel.GPIO5.idle=high   (keeps the target powered while no plan holds channel 5; not written by this tool)
released every plan (channel 5 is back to its idle state: the target is powered only while something drives it)
done in 7.3 s
```

19 is SWIO and 4 is NRST (hold stopped 21, and the attach under reset gave dpc 0). 22 / 23 are the target's UART (idle high,
so they are driven and neither scanned nor held), and 21 is an output the application drives.

## Speeding up a UART bridge (port_speed, only when used)

On a probe whose optional `oep.probe.link` sets `port_speed` in its ops (oep-if-link §3; the reference classic ESP32 firmware sets it), the host can, for one session,
run the UART bridge faster than the boot speed (115200). Nothing changes unless the host asks:

```python
hst = link.open_host("/dev/ttyUSB0", port_speed=True)    # 500000; takes the lock and keeps the session open
print(hst.link.speed.to_text())             # per candidate: the baseline, the flows, the current speed
# within a session that took the full form (measuring the baseline and flows):
report = link.raise_speed(hst, [500000], verify=True, flows=[("out", 2)], record=True)
# a faster speed only when the user chooses it (e.g. `oep speed <probe> 921600,500000`): a 1-second check first
hst = link.open_host("/dev/ttyUSB0", port_speed=[921600, 500000])
```

**The default ceiling is 500000** (this client's choice; oep-if-link §3 leaves to the host which speeds to try, how to check them and when to go back): the default candidate is only 500000 (`link.DEFAULT_CANDIDATES`), and unless the user
chooses explicitly, faster speeds are not tried. Put speeds faster than `link.DEFAULT_CEILING` among the candidates only when the user chooses them
(`oep speed <probe> 921600,500000`, settings). Such a speed, in either the minimal or the full form, is decided only after passing the **1-second check**:
in the trial state, full frames of oep.probe.link's source (in, max_frame − 7) and sink (out, max_frame − 12),
and also duplex if the full form checks duplex, are each streamed for 1 second or more (`link.FAST_VERIFY_S`) at that concurrency,
and judged by the same criteria as the flows (no re-run at n = 1). That trial asks for verify_ms 4000 (6000 if duplex is included), so the lease needs
5000 (7000) ms or more (`link.lease_for(candidates, flows, verify)`. `open_host` and `oep speed` take at least that, and with a short
lease that candidate is skipped). After deciding, it goes through the same probation period and in-use judgment as other speeds. With some converters, a speed faster than 500000 passes the short check
and still breaks responses on long writes.

The procedure is this client's. oep-if-link §3 is only the handshake: the port_speed request is baud, step (try / decide / revert) and verify_ms, effective on the
UART bridge port the request came on, and only speeds faster than boot. port_state (no lock needed) reads that port's baud and phase (0 boot, 1 trying, 2 decided, 3 return waiting for a frame
boundary). **The minimal form** (default, about 50 ms, no measurement): per candidate, in order,
`try` (the probe switches after answering at the current speed) → the host switches to the requested baud → 20 ms → `confirm` (100 ms, up to 3 times) →
`decide`. **The full form** (pass `verify=True` or `flows=`): takes a baseline at the boot speed per flow (this session's frames,
or 60 frames if none), and per candidate streams only the flows used. A flow = `("in"|"out"|"duplex", n)` (in = oep.probe.link's source, probe → host;
out = oep.probe.link's sink, host → probe; duplex = both alternately. `n` is the concurrency, 0 = the maximum the link offers); it streams 16 full frames (source
max_frame − 7, sink max_frame − 12),
counts broken and lost, and measures KB/s. If broken + lost is 3 or more and the ratio exceeds max(baseline × 2, 5 %), the flow does not pass, and it is re-run at n = 1
(if it passes, n = 1 is the link's limit); if even one does not pass, the candidate does not pass. A candidate that does not pass is reverted (step 2), the speed returns to the boot speed, confirm is redone, and phase 0 is checked with a new port_state (`link.port_state`).
The first candidate that passes is used. Speeds the probe's UART cannot make (within port_speed_tolerance_pct, 2 %) are refused and skipped. Which speeds pass is decided by the converter chip and driver (some can only make integer divisions of
the clock, and some pass the short check but break long bursts at speeds faster than 500000), so the host chooses the speed within the ceiling.
The result (`link.speed`: `base`, `rate`, `chosen`, `baseline`, `trials` = `SpeedTrial` with `flows` and `in_kb_s` / `out_kb_s` / `duplex_kb_s`)
is used to budget captures and writes.

The probe goes back to the boot speed on its own only when a trial passes verify_ms, when after deciding port_speed_idle_ms
(3 seconds, `link.IDLE_MS`; a crashed host's speed also does not stay longer than that) passes with no valid frame, and when the session ends (`end`, lease expiry,
force) - broken frames alone do not send it back. While raised, the link sends a keepalive before a request if it has been silent for 1 second (`link.KEEPALIVE_S`), and
a caller that stays silent for long does the same with `hst.link.keep_alive()`. Opening a serial port repeats the first confirm for port_speed_idle_ms +
host_wait_add_ms (4 seconds, `link.OPEN_RETRY_S`), waiting for a leftover speed to go back. The link follows `end`, revert, restart and another session's force open at once.
A request that gets no response at the raised speed is first resent at that speed: the first wait is at most a third of port_speed_idle_ms
(`link.RAISED_FIRST_WAIT_S`) and a quarter of the lease, and does not go below core §4.4's lower bound, so the resend still reaches a probe
at that speed. Only when that resend also gets no response does it return to the boot speed, confirm, step down, and send once more there (it does not hang). While in use, the first 32 KiB and 1 second at the decided speed (`probation_bytes`,
`probation_s`) are a probation period: if broken/lost are 3 or more and exceed max(baseline × 2, 5 %) there, or no response comes, it steps down at once
as a failed check (the 16-frame check stays as a quick gate). After that it looks at the frames of the last 3 seconds (no judgment under 50),
and steps down if broken/lost exceed max(baseline × 2, 10 %). Stepping down sends port_speed revert at the raised speed, returns to the boot speed and confirms,
and newly tries the next candidate of that call below those that failed in this session → confirm → check → decide (if none
remain, the boot speed). The broken speed and those above it are not used for the rest of the session (`speed.stepped_down`, `speed.down_why`,
`speed.step_downs` with `to` and `probation`). `max_tries=` is the limit on the number of candidates tried in one call (2 for the capture host).
`record=True` (a bool, a path or a `speed_record.SpeedRecord`; on by default in the `oep speed` CLI, off in the library) keeps the passed / failed
speeds per (port, unit_id) in `~/.cache/oep-client/link-speed.json` (passed for 30 days, failed for 1 day; a failure measured within
2 seconds (`settle_s`) of a breakdown at another speed is "unknown"), puts passed speeds first and drops failed ones. When all candidates are recorded as failed,
the slowest candidate is tried once (`speed.retried`). For a unit_id starting with `x-` (a probe with no unique number or storage, core §7.5),
nothing is kept or read. What is raised is the port this host came by (the transport TLV of the confirm response, core §7.1) when it is a UART bridge.
A port_speed request that comes on a port that is not a UART bridge, and a trial while a port is raised, are refused with unavailable (cause 6). On a probe without `oep.probe.link`, or whose `oep.probe.link` does not have port_speed,
the speed does not change (the result says why). Wire tests (`linktest`, `core.link_speed`) also need `oep.probe.link`. Behind a broker (TCP), the broker does it, not the client. A serial port is opened in low-latency
mode if the driver has it (on FTDI, the latency timer goes from 16 to 1 ms). If the board's boot speed is not 115200,
pass it with `open_host(..., baud=)`.

## Virtual bench (the spec that runs)

The virtual bench builds the probe, the target behind it and the fixture's wiring in software, matched to real fixtures; it is an environment for trying host
software without hardware (in this repo, "bench" alone means a real HIL fixture, and the software one is always written "virtual bench").

Its probe, `endpoint.Endpoint`, answers as the norms of the spec it implements say (response data and lists carry lengths, every response can be followed
by TLVs, describe is declarations only and state is in lock-free state ops, capture generations), and is used as the "spec that runs" to match ch32rv, this client and the probe firmware
against each other (when the spec changes, this is brought in line before the probe firmware). `virtual_bench` has example declarations (profiles:
`p4-x035`; `esp32-v003` = the same frame limits as the classic ESP32 firmware - max_frame 512, riscv-dm max_length 488 -;
`esp32-v003-64` = the same probe at the smallest max_frame it can declare, 64 (max_length 40, list / describe read in parts);
`p4-bench` = a fictional fixture with 2 simultaneous connections at 3 RVSWD places; `rp2350-pins` = a wire where the host chooses pins; `core-v1` = core only with
max_frame 64, no interfaces (`virtual_bench_serve` only). What the probe keeps itself and does not declare - its own channels, the console's
256-byte send queue, the capture widths, segment records, max_read, the capture-group budget - is in `Offered.inner` and `VirtualProbe.own_channels`),
and `virtual_bench_serial` is the byte side of the serial port (COBS frames only; anything not a frame is dropped).

`virtual_bench_capture` is `oep.fixture.logic` (logic): one-shot, repeat (segments are made on the clock at the actual rate, ring, release),
streaming (data push while subscribed; all carry generations and segments), level / edge triggers and pretrigger, events. What can be captured is
fixed: sample i is the counter value i, and channel k is its bit k (a square wave with a period of 2^(k+1) samples). The layout is the widths the profile can make
(its own, not declared. `p4-x035`: w 1 to 16, the same as the P4's PARLIO, w 4 for 3 wires. `esp32-v003`: w 8, the same as the classic ESP32's sampler,
one-shot only). capture only listens, so it can be planned on pins other interfaces hold too. `p4-x035` also has `oep.fixture.analog`
(the 4 channels of the P4's ADC1, GPIO16 to 23. An even channel k is a square wave, an odd one a sine wave, with a period of 64 (k // 2 + 1) samples. 12-bit values
in 16-bit slots. 4 frontends of 950 / 1250 / 1750 / 3100 mV, a fabricated 2-point calibration and Vrefint) and
`oep.fixture.capture-group`, which binds logic and analog (they start together, and analog is 5 µs late (±2 µs)). Time is ns on the probe's one clock.

`oep.fixture.i2c-target` and `oep.fixture.spi-target` (`p4-x035`, `esp32-v003`) take roles through plan (SDA / SCL, SCK / MOSI / MISO / CS;
`esp32-v003`'s SPI matches one of 2 channel_groups exactly) and answer all ops of fixture §3 / §4. There is no bus controller:
nothing is received until a test calls an endpoint hook (`i2c_write` / `i2c_read` / `spi_transfer`: one transaction on the bus) (spi's arm keeps waiting).

The main points of the rules it follows. Core: fn 0 has no name and is not on list, and ops are only the core's required ones. list uses first only. Refusals come in the order header (corr 0 is
malformed, unknown_function, unknown_operation, session_required), resend (core §5.1: the last corr of the last session opened, and records with limits on the whole request and
response - a request with a remembered corr and different content is malformed; an unremembered corr 1 to 32767 ahead is a new request, otherwise result_lost),
session (no_session, locked with the remaining ms and owner); after that, everything is checked before anything is changed, and it answers with any one of the reasons that apply.
Unknown non-critical request TLVs are ignored, unknown critical ones are unsupported with the tag as received, implemented TLVs are checked the same
regardless of bit 7, and TLV number 0 is malformed. unavailable carries cause, channel and fn. connections and streams share one u32 number
space starting at 1, not reused while the boot_id is the same. Notifications (core §10) go to the connection the fn's subscribe came on; events at once, data
batched by min_bytes / max_delay_ms; what has not yet been sent for a replaced or removed subscription is dropped. The clock is ns since boot (`now_ns=`;
virtual_bench_serve uses `time.monotonic_ns` and also draws the boot_id), from 0 at `reboot()`. debug: attach names method, pins and max_speed in the fixed part
and always creates a new connection (pins in use are unavailable cause 1; beyond max_connections, cause 2). method 1 only when features has
attach_halt. attach, scan and riscv-dm reset answer within max_op_ms (10000) (they wait for a target that restarts itself after reset -
`VirtualTarget.restart_ms` - and record it in `settle_log`). reset answers with status, flags and pc. run with timeout_ms 0 stops at once. A preparation
failure (`VirtualTarget.fail_regs`) and a running hart give stopped 3 not_run. A target op whose exchange gets no answer fails with status line and does not
drive the wire (`pin_state` is `wire-free`). A high-level op whose cleanup failed (`VirtualTarget.restore_fails`, `step_stuck`) answers and then closes
the connection and its stream. Found = DMSTATUS.version 2 or more and not 15 (`VirtualTarget.version`). `halt_stuck` makes halt
time out. Console: one stream held by the session per connection. Besides the common marks, reset 0x40 (reset op's ndmreset) and
restart 0x41. The send queue of a DMDATA / dmseq stream passes 3 / 2 bytes per poll to the target, and poll runs once per 1 ms while the hart runs,
and not while it is halted or during riscv-dm requests on that connection (`console_take(sid)` passes everything). fixture: gpio modes are 0 to 6
(the outside wires are `gpio_world`). uart receives nothing until configure, and status is baud and format. i2c-target is one shape (one write with data
is one frame, cut at max_length; reads come from the preload_tx slot, else 0xFF). spi-target has no reset. capture:
configure's TLVs follow only core §2.3. start / configure of a bound track is unavailable cause 4, and a bind short of resources is cause 2 with
fn. A read at or after the write position answers write_pos and len 0. Settings (core §13): each fn's items come from describe, and values have no origin (the profile's defaults go in on a boot with nothing saved; labels are
unique, ASCII case-insensitive; `changeable=False` for a get-only probe). A value whose length differs from its shape is malformed. generation is the count of changes, counted from 1 since boot. state is storage_state, saved_generation, reason.
At boot, saved values (by category and item name) are checked one by one, disable is applied first, and what cannot be applied is left out (partial application). port_speed: baud / step / verify_ms to the UART bridge port
the request came on, within port_speed_tolerance_pct, and phases 0 to 3 can be read with port_state. The virtual bench goes back after verify_ms, after port_speed_idle_ms with no valid
frame, and at session end - not on broken frames.

Other test hooks: what the probe does to a pin is `pin_state(ch)` (MISO is driven only while CS is active - `spi_select` -; i2c-target is
open drain with only its own pull-up - `virtual_bench.with_i2c_pullups` -; taking a plan does not change the pin until use begins; the pins of a closed
connection return to idle). An idle pull the channel does not have (`no_pull`). `virtual_bench.with_unit_id(probe, "x-...")` makes a probe with an `x-` unit_id,
and `virtual_bench.without(probe, name)` removes an interface (`virtual_bench.without(p, virtual_bench.RESTART)`). Every fn's
describe has the `ops` tag (if the profile does not write it, `VirtualProbe` sets all the ops in that interface's table, and
`virtual_bench.ops_of(name, *without)` removes optional ops. ops that break core §7.4 - given by a test with `fill_ops=False` - are output as given).
Every profile except `core-v1` lists `oep.probe.plan` (plan_roles 32) and `oep.probe.restart` (restart_max_ms
2000) after the fns it already had: p4-x035 plan 14 / restart 15, esp32-v003 11 / 12, p4-bench 8 / 9, rp2350-pins 7 / 8.
`tests/test_spec_vectors.py` runs oep-spec's vectors (sessions.json step by step, the ops cases byte for byte) against the virtual bench and this client.

For tests of outside programs, use `virtual_bench_serve` as a child process:

```sh
uv run python -m oep_client.virtual_bench_serve --pty --profile p4-bench --console 'uptime %d\r\n' --every 100
# first line: PTY /dev/pts/N (PORT n with --tcp 0). Closing stdin ends it (not with --keep-on-eof)
```

Writing the line `reboot` to stdin restarts the probe in the middle of the session with a new random boot_id (`Endpoint.reboot`).
The session table, resend table, connections, streams, subscriptions, plans and unsaved settings go away, the saved settings (`--label`) are applied again, the clock starts from 0, and the serial port goes back to the boot speed. Requests in the old session get no_session, and confirm and open return the new boot_id.
pty and TCP connections stay open. It prints `virtual_bench_serve: rebooted, boot_id 0x........` on stderr. `lose [CONNECTION]` makes the wire of that connection (or all live ones if omitted) lost for good (`Endpoint.lose`, debug §2): the connection and its console stream close, and requests naming it after that get no_resource. It prints `virtual_bench_serve: lost connection(s) ...` on stderr. `capture-overflow FN [ERROR]` makes capture FN drop data in the middle of the segment it is capturing (`Endpoint.capture_overflow`): that segment is not published, and the track stops with an error (state 6, error ERROR, default 2). Other lines get a short note on stderr and are ignored. restart of oep.probe.restart (oep-if-restart; every profile except `core-v1` lists this interface and shows restart_max_ms 2000 in describe; `--no-restart` makes a probe without it: not on list, and that fn is unknown_function) does the same by request: it sends the response first, then the probe restarts, and what was read after the request is dropped:

```python
# the test holds the child process with stdin=PIPE and writes that line
proc.stdin.write(b"reboot\n"); proc.stdin.flush()
```

The pty is a serial port (the host opens it with TIOCEXCL); `--tcp PORT` by default speaks length frames like the probe's TCP transport (`--framing length`:
the listener appears in describe as the probe's TCP transport, and confirm returns its number. A length over max_frame closes the connection).
`--framing cobs` only when given; it imitates a serial port over a socket (COBS frames only, one connection at a time). TCP with length framing answers up to `--tcp-connections N` connections at once (default 3, the same as the reference probe; connections beyond that are accepted and closed at once). Each connection is a separate transport (transports §1: confirm and the revision in use are per connection, and notifications go to the connection the fn's subscribe came on), and the probe is one: while a session on one connection holds the lock, open from another connection is refused with locked, and lock_state looks the same from every connection. A closed connection does not end the session (transports §3): the session, lock, subscriptions and resend table remain until the lease expires, notifications to the closed connection are dropped, an open with the same id from another connection takes the session back, and notifications move to that connection too (core §6.2). `--once` ends when, after accepting one connection, there are no connections left (with length framing, when the last of the overlapping connections closes). Fault injection: `--drop N` (does not send the Nth answer, once; the request has run, so a resend gets the remembered
answer), `--noise TEXT` (noise before an answer), `--corrupt N` (breaks the CRC of the Nth answer, once). `--uart-rx` passes bytes to the RX of the first fixture UART the session planned and
configured, and `--run-hook` makes riscv-dm's run the host's own model. `--capture-slipped` sets flags bit2 on all capture
segments.
`--silent-until-reset N` makes the target of the Nth pin pair answer nothing until it is reset through that wire (attach with the host's reset TLV)
(`--label CH=TEXT`, e.g. `23=v003.nrst`, names a wire of the host's profile v003, host guide §5.3). port_speed: `esp32-v003`'s `oep.probe.link` has it (`--no-port-speed` removes it from ops).
`--broken-rate RATE[:MIN_SIZE][:in|out]` breaks frames at that speed (`virtual_bench_serial.VirtualSerialStream` in the same process breaks every byte in both directions while the host's speed
differs from the probe's). Event and data push go out on the pty and on TCP (both framings). For the rest, `--help`.

**Advertising itself (DNS-SD, CI on one machine).** `--announce` (with `--tcp`; length framing) answers mDNS / DNS-SD queries for
`_oep._tcp` like a probe listening on TCP (transports §3): PTR `_oep._tcp.local.` -> instance `OEP virtual <unit_id> <port>`,
its SRV (port and host `oep-virtual-<unit_id>-<port>.local.`), its TXT `unit_id=<unit_id>` (the one in fn 0's describe; set with `--unit-id ID`),
and the host's A records: the `--listen` address (default 127.0.0.1; with `--listen 0.0.0.0`, the addresses of all advertised interfaces,
loopback last). With the `mdns` extra, python-zeroconf answers; otherwise this package's minimal responder (`virtual_bench_mdns`; IPv4):
a query from a port other than 5353 (legacy unicast, RFC 6762 §6.7) is answered to the sender's address and port, with its ID and question, TTL 10;
a query from 5353 is answered to the group. `--announce-on ADDR` (repeatable) chooses interfaces by IPv4 address (default
all, including loopback if the OS lets it join the group; Linux does). `--announce-engine auto|zeroconf|minimal`. CI on one machine
runs the querying side and the answering side on the same host:

```sh
python -m oep_client.virtual_bench_serve --tcp 0 --announce --profile esp32-v003 --unit-id 0123456789ab
# stdout: PORT n (the advertisement is out by then). stderr: virtual_bench_serve: announcing ...
# then: ask for the PTR of _oep._tcp.local. once, from a temporary port to 224.0.0.251:5353 (IPv4) - the answer comes back to that port:
# PTR, SRV (port n), TXT unit_id=0123456789ab, A 127.0.0.1 - and open tcp://127.0.0.1:n
```

The query's multicast comes back to this host on the interface it went out of, and the responder has joined the group on all interfaces.
On a host without a multicast route, use `--announce-on 127.0.0.1` and send the query with IP_MULTICAST_IF 127.0.0.1. Jobs running side by side
are kept apart by changing `--unit-id` (the instance name also includes the port). Containers and VMs answer only within their own network.
tests/test_virtual_bench_announce.py tries this with `oep find`, `discovery.find_unit` and `oep://UNIT_ID?via=net` (both engines) (a responder in the same process also advertises a non-OEP TCP
service and a different TXT unit_id, and checks that verification drops them). Where multicast does not
come back, it skips.
