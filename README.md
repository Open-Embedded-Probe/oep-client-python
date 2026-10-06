# OEP Python client

[日本語](README.ja.md)

The host side of Open Embedded Probe (OEP). It speaks the v1 protocol of
[oep-spec](https://github.com/Open-Embedded-Probe/oep-spec) (the core `docs/oep-core.ja.md`, `docs/oep-transports.ja.md`
and the interfaces `interfaces/*.ja.md`), v1 before the freeze: until the freeze the spec may still break. The wire numbers come from `oep_client.registry`, a verbatim copy of
oep-spec's generated `generated/oep-v1/oep_v1_registry.py`. This is an experimental stage: breaking changes are expected and
no compatible API is promised.

**The spec this implements: oep-spec commit `498ae95`** (no `v0.x` tag yet; oep-spec versioning §6 - before the freeze,
revision 1 alone does not fix the forms, so an implementation names the spec it implements). That is the 2026-10-06
simplification (one 10-byte request header, TLV len u16, closed fixed forms, the `ops` describe tag, no resume), the
console's send queue and the reset settle wait (f0c68bf), a longer probe.config item (d34dafa), the link source's len
(4bd3a87), an attach joining a connection (59dd028), and the 2026-10-06 structure (289bde0 .. 498ae95): the core has no
name (fn 0, never listed; its ops are the eight mandatory ones, fn 0's clock among them), the optional parts are
interfaces found by name - `oep.probe.plan` (plan_apply / plan_release, plan_roles), `oep.probe.restart` (restart,
restart_max_ms), `oep.probe.link` (was `oep.probe.link`) -, subscribe / unsubscribe are ops 0x30 / 0x32 of the interface that
sends the notifications (no heartbeat), and the ops encoding of core §7.4 is checked.

Until the freeze the Japanese text (`.ja.md`) is the specification's working text; the English documents are regenerated
from it at the freeze and become authoritative then. Start with oep-spec's [README](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/README.md) and
[review guide](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/review-guide.md) (what is where); [getting started](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/getting-started.md)
builds the smallest probe and host, [docs/oep-core.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/oep-core.md) is the protocol core, and
[docs/conformance.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/conformance.md) says what a host must do to conform.

It follows OEP's division of work: the knowledge of the target lives in the host. The probe knows only its wires and DMI /
DP-AP transfers; the CH32 flash controller, the RAM loader, the RP2350 boot ROM, the Cortex-M debug registers and so on are
here.

```sh
pip install oep-client-python     # PyPI (import oep_client); a checkout: pip install -e <checkout>
uv run pytest                     # in a checkout (the fake probe; no hardware)
OEP_HW_BOARDS=<board id> OEP_PROBE_DIR=<oep-probe-arduino checkout> uv run pytest tests/hw -m hw   # a real probe: tests/hw/README.md
```

`import oep_client` is all it takes. The registry and oep-spec's test vectors (`tests/vectors/*.json`, checked by
`tests/test_vectors.py` against this client and the fake) are copied from oep-spec with `tools/sync_registry.sh`. PyPI's
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
| `host` | requests and results (one 10-byte header: the session's id on a request sent in the session, 0 on a lock-free one outside it), the session id and the lock, `call()` (raises unless it worked), pipelining, the errors (`OepError` / `Rejected` / `Failed`; `NoSession` when the session ended - end, lease expiry, another session's force - and the probe released everything it created: no resume, the session is never re-opened behind the caller's back, `Host.session` goes to None and `Host.epoch` moves; `NotUsable` for a probe whose confirm is outside core §7.1's bounds or whose `max_op_ms` is outside 1..600000 - nothing more is sent to it). `open(lease_ms, force=, owner=)` always opens a new session under a new random id and answers `Opened(lease_ms, boot_id)`; `end()` releases everything and leaves the host out of a session. A changed boot_id (confirm, clock, open) drops the name -> fn cache, so interfaces are listed again. `clock()` reads fn 0's clock (core §7.7, lock-free and session-free) as `ClockReading(before_ns, after_ns, uptime_ns, boot_id, round_trip_ns)` - this host's time (`time.monotonic_ns`, or `now=`) just before the request went out and just after the answer came in, the probe's uptime read between them; `.host_ns` is the midpoint and `.uncertainty_ns` half the round trip (host guide §12) - and `clock_best(n=8)` keeps the shortest round trip of n readings. `subscribe(fn, min_bytes, max_delay_ms)` / `unsubscribe(fn)` send the interface's own ops 0x30 / 0x32 (core §11.3; min_bytes / max_delay_ms batch the data only, events come at once; an fn that sends nothing answers unknown_operation). `restart_probe(wait_s=None)` restarts the probe through `oep.probe.restart`, found by name (oep-if-restart, host guide §5.2; LookupError when the probe lists none; the session must hold the lock): after the answer the link closes, waits `restart_after_answer_ms` (100 ms), opens again as a new open (confirm first; a serial port at its boot speed, a USB device found again once it re-enumerated, a TCP connection made again), retried until the probe's restart_max_ms (oep.probe.restart's describe, read before the restart; `core.restart_max_ms`) or 10 s when it declares none - then the probe is gone (ConnectionError / TimeoutError) - and the new boot_id is returned - `NotRestarted` if it stayed the same; a lost answer whose resend the restarted probe refuses no_session counts as done. `request_restart()` sends the request alone. |
| `link` | transports: serial ports (always COBS + CRC as `0x00 <COBS> 0x00`, bytes outside frames skipped as noise, opened exclusively, 8N1 with DTR / RTS asserted), USB vendor bulk / HID and TCP (length frames, the transports §5 resync - waiting 250 ms after the host's last write; on TCP a pause inside a frame is read on); matching by corr and resending; a resend that gets no answer either raises `TransportFailed`, and the next request first recovers with a confirm (quiet input, then a confirm with its own corr; serial ports too) or raises ConnectionError; every answer waited at least core §4.4's floor (argument time + 1000 ms + a serial port's transfer time with min_max_frame until a confirm answer, `wait_floor_s`); short answers are broken frames; `open_host(target)` |
| `core` | interfaces by name (cached), confirm (with the probe's `boot_id` and `transport`, the index this host came in on; later confirms ask for the revision in use), the probe's describe (declarations only, cached per boot: labels, the transport list, `max_op_ms`), every fn's `ops` (the describe's ops tag, core §7.4, its one encoding checked: an fn whose ops break it is not used - `UnusableFunction` -, fn 0's makes the probe `NotUsable`; `ops(hst, fn)`, `offers(hst, fn, op)`, `Interface.offers(op)`; `require` / `not_offered` raise without sending the same `Rejected` (detail unknown_operation) the probe would answer), taking the lock (`take`), the pin plan through `oep.probe.plan` (`plan_fn`, `plan_apply`, `plan_release`, `plan_roles`), `oep.probe.restart` (`restart_fn`, `restart_max_ms`), the `Interface` base; a list entry with fn 0 is left out (the core is never listed); `oep.probe.link` (oep-if-link): `link_fn`, `link_speed`, source / sink request and answer helpers |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio` (scan, attach - `max_speed` always sent, `reset=(channel, hold_ms)` for an attach under reset -, detach, connections), `oep.target.riscv-dm` (answers count their values; `RunResult.not_halted`; a step that could not halt the hart again raises `StepError` with `step_left`), `RiscvDm.declared()` (the optional ops the probe offers by its ops tag; the others answer unknown_operation), `Wire.search_retries` (the extra attempts of the attach's bring-up, sent only when one ran), attach and scan wait their budgets (an attach with the reset TLV attach_budget_ms + hold_ms + reset_settle_ms, a riscv-dm reset reset_settle_ms: `attach_ms`, `reset_ms`), finding the reset line (`find_reset_line(candidates, pins=...)`), attach through GPIO |
| `targets` | what the host knows per target family, in one table (`FAMILIES`: wire, target_id match, reset vector, option-byte NRST reader, max_speed / idle_clock); `identify(target_id)` |
| `pins` | `oep pins`: classify the channels, hold-low search, scan, identify, confirm the reset line, suggest a slot (`PinFinder`) |
| `console` | `oep.target.console` (position streams: read answers carry their length, marks are `time_ns`, the lock-free `streams()` list; a write goes into the stream's send queue, `Console.send_queue` = describe tag 0x41, accepted = min(count, its free space), 0 only when full; SDI takes nothing) and `ConsoleIO`, read as bytes and written a send_queue at a time, on from each answer's accepted (`ConsoleIO.send_queue`; `stall_s`: TimeoutError when nothing is taken for that long) |
| `fixture` | `oep.fixture.gpio` (an output's strength per element: `set([(ch, mode, Drive.max_ma(10))])`, `Drive.default()` = drive_kind 2; `drive_levels()`, `read_state()` = levels and the level in force) / `uart` (its stream is the plan's; `status()`) / `i2c-target` (`pullup_ohms`: the pull-ups it enables, None when it declares none; the reserved addresses 0x00-0x07 / 0x78-0x7F refused before sending) / `spi-target` (`cs_setup_ns`: how long after CS the first bit is sure; `oep dump` shows it) |
| `config` | `oep.probe.config` (slots - `boot_reset` after `attach` -, binds of 3-byte streams, plan / label / idle - 6 bytes, its `drive` or the default (kind 2) - / uart items, get / set / unset / save / erase; `describe()` = the declarations, `state()` = the live storage / slot / bind state, `reset_at_ns` included, the storage as the last page says; `hash_of(items)` = the probe's hash; `find_line(cfg, slot, "nrst")` = probe.config §1.3's line lookup, the firmware's fixed labels as its step (c)) |
| `capture` | `oep.fixture.logic` / `analog` / `capture-group` (revision 1, oep-spec oep-if-capture). mode, rate, trigger, pretrigger and frontend go critical, the answer's samples hold; a start's blocking_ms is waited out with nothing sent (then a resync on length frames). Every start is a generation (`LogicCapture.generation`) that read and release name - `read_segment(segment)` does it by itself; `status()` returns a `Status`. Every segment read goes to the `Host.on_capture` callbacks as a `CaptureRecord` (the hook for run recorders; no wireskein dependency) |
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
# a serial port (always COBS), "tcp://127.0.0.1:PORT" (a broker), "usb:<unit id>" (the device whose USB serial it is;
# describe's unit_id must match), "usb" (every device on the project's VID:PID 1209:4F45, each counted once: exactly
# one -> vendor, then HID, else its CDC port; several -> link.SeveralProbes lists them, name one) /
# "usb:VID:PID[:SERIAL]" (vendor, then HID). Each is probed first with a
# confirm only (transports §3): no valid answer -> closed, link.NotOepProbe
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

## USB access on Linux (udev)

A probe enumerates with the project's USB VID:PID `1209:4F45` (transports §3). Opening its vendor bulk (libusb) or its HID
(hidraw) as an ordinary user needs a udev rule: [`udev/70-oep-probe.rules`](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/udev/70-oep-probe.rules) (in a checkout and the sdist) gives the
logged-in user (`uaccess`) and the `plugdev` group access to the USB device and its hidraw node. Installing it needs
administrator rights:

```sh
sudo install -m 0644 udev/70-oep-probe.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # or unplug and replug the probe
```

A probe's CDC ports (`/dev/ttyACM*`) need no rule beyond the usual `dialout` group. Windows and macOS need no rule.

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
oep pins <probe> --power 5 --wire swio       # where the target is wired: debug pins, reset line, a slot (below)
oep clock <probe> [-n 8] [--json]            # fn 0's clock: uptime_ns and boot_id against this host's time (shortest of n)
oep restart <probe>                          # oep.probe.restart: restart the probe, wait until it is back (new boot_id)
```

`<probe>` is a serial port, `tcp://HOST:PORT` or `usb[:VID:PID[:SERIAL]]`. A change takes the lock (owner "oep config") and
ends the session after it; it takes effect at once and, after `save`, stays over a restart.

A run on hardware: ArduinoCore-CH32's `tests/manual/oep_smoke/` (`oep_smoke.py`, `oep_probe_checks.py`).

## Finding the pins (`oep pins`)

`oep pins <probe> [--power CH] [--exclude CH,...] [--wire swio|rvswd|swd] [--steps ...] [--save] [--json]` finds where a
target is wired to a probe whose pins the host chooses. Each step says what it did; the whole run stays under a minute and
every plan is released at the end. The procedure and its reasons are in oep-spec's host development guide (§19
"Finding pins"); what each target family needs is in one table, `targets.FAMILIES` (wire, reset vector, the option-byte reader
for the reset line, max_speed / idle_clock), which oep-spec `docs/target-scan-notes.ja.md` records the sources of.

1. **classify**: every channel `oep.fixture.gpio` allows is read 16 times under pull-up, both pulls, then pull-down:
   floating, pulled-up (both pulls still read 1: a weak pull-up such as a reset line's), driven-high / driven-low (the same
   level under both pulls: push-pull or a strong pull - an idle-high UART line looks like this), active (changes between
   reads). With `--power CH` the target is first read switched off: the channels that read otherwise follow its power.
   These are candidates only.
2. **hold**: each floating / pulled-up channel is held low (open drain) while an active channel is watched; a channel
   that stops it is a reset-line candidate.
3. **scan**: the wire over the floating / pulled-up channels (rvswd / swd: pairs, at most 600) - never over a driven or
   active channel, the power channel or `--exclude`.
4. **identify**: attach (halt) on what answered; the target_id names the family; a CH32V00x's option bytes say whether
   NRST exists (read only, never written); then resume.
5. **reset**: attach under reset through each candidate - the real line stops the hart at the reset vector.
6. **slot**: a suggested `oep config slot` line and the labels `<slot>.nrst` / `<slot>.power_hi`, checked with
   `config.find_line` against the probe's settings (a note when another channel already carries the name: the line
   would not be found); `--save` writes them (set + save). Nothing is written without `--save`.

Safety: a driven or active channel is never driven, scanned or held; the power channel is touched only with `--power`;
holding low is open drain only. A new gpio plan lets every pin of the old one go first - the power channel too - so with
`--power` each new plan is followed by a clean power cycle (`power_cycles` in the report).

ESP32-P4 with a CH32V003 (power on GPIO5), 2026-10-02:

```
$ oep pins /run/board-identify/by-id/esp32-series-30eda0ea068b --power 5 --wire swio
power: channel 5 low 300 ms (target off: read), then high 400 ms before the reads
classify: 52 channels, 16 reads each under pull-up, both pulls, pull-down
  floating     0-3,6,9-20,26-34,36-50,52-54
  pulled-up    4
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
slot: oep config slot ... --name ch32v00x --wire swio --pins 19   (not written; --save writes it)
label: oep config label ... 4 ch32v00x.nrst   (not written)
label: oep config label ... 5 ch32v00x.power_hi   (not written)
idle:  oep config idle ... 5 output-high   (keeps the target powered while no plan holds channel 5; not written by this tool)
released every plan (channel 5 is back to its idle state: the target is powered only while something drives it)
done in 7.3 s
```

22 / 23 are the target's UART (idle high: driven, so neither scanned nor held); 21 is an output its app toggles.

## A faster UART bridge (port_speed, opt-in)

A probe whose optional `oep.probe.link` offers `port_speed` in its ops (oep-if-link §3; the classic ESP32 reference firmware
does) lets the host
run its UART bridge faster than the boot speed (115200) for one session. Nothing changes unless the host asks:

```python
hst = link.open_host("/dev/ttyUSB0", port_speed=[921600, 500000])    # takes the lock, keeps the session open
print(hst.link.speed.to_text())             # every candidate tried, the baseline, the flows, the one in force
# the full form (a baseline and the flows measured), in a session already taken:
report = link.raise_speed(hst, [921600, 500000], verify=True, flows=[("out", 2)], record=True)
```

The host's procedure is the oep-spec host guide §17 (oep-if-link §3 is the handshake). The **minimal form** (the default, about
50 ms, no measurement): each candidate in order - `try` (answered at the speed now, then the probe switches) -> the host
switches to the requested baud -> 20 ms -> a `confirm` (100 ms, up to 3) -> `commit`. The **full form** (`verify=True`,
or `flows=` given): a baseline at the boot speed per flow (this session's frames, or 60 measured), then for each
candidate every flow the session will use - `flows` of `("in"|"out"|"duplex", n)` (in = oep.probe.link source probe -> host, out =
oep.probe.link sink host -> probe, duplex = both interleaved; `n` in flight, 0 = the most the link keeps) - 16 frames at
max_frame - 26 (what one oep.probe.link source answer carries), counting broken and lost and measuring KB/s; a flow fails on broken + lost >= 3 over max(2 x baseline,
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
more there - it never wedges; while raised each wait for an answer is a quarter of the lease, never under core §4.4's
floor, so this ends inside it. In use, a committed rate's first 32 KiB and 1 s (`probation_bytes`, `probation_s`) are its probation: 3 or
more broken or lost frames over max(2 x baseline, 5 %), or a missed answer, there count as a verify failure and step
down at once (the 16-frame verify stays the quick gate). After it the link judges the frames of the last 3 s (none
under 50): over max(2 x baseline, 10 %) broken or lost steps the link down. A step down is port_speed revert at the
raised rate, the boot speed, a confirm, then the next lower candidate of that call that has not failed in the session
(a fresh try -> confirm -> verify -> commit; none left: the boot speed); a rate that broke is not used again in that
session, nor any rate above it (`speed.stepped_down`, `speed.down_why`, `speed.step_downs` with `to` and
`probation`). `max_tries=` bounds the candidates one call tries (a capture host wants 2). `record=True` (a bool, a
path, or a `speed_record.SpeedRecord`; the `oep speed` CLI's default, off in the library) keeps passed / failed rates
per (port, unit_id) under `~/.cache/oep-client/link-speed.json` - a pass for 30 days, a failure for 1 day, a failure
measured within 2 s (`settle_s`) of a breakdown at another rate as unknown - putting a passed rate first and leaving
failed ones out; when every candidate is marked failed the slowest is tried once (`speed.retried`). An `x-` unit_id (a probe with neither a unique number nor storage, core §7.5) keys nothing: no record is kept or read
for it. The port raised is the one this host came in on - the transport TLV of confirm's answer (core §7.1) - when that
is a UART bridge. A probe without `oep.probe.link`, or whose `oep.probe.link` does not offer port_speed, answers `not supported`
and stays at its speed; the link test (`linktest`, `core.link_speed`) needs `oep.probe.link` too. Behind a broker (TCP) the broker does
this, not the client. Serial ports are also opened in the driver's low-latency mode where it has one (an FTDI's latency
timer 16 -> 1 ms tripled a UART bridge's throughput). `open_host(..., baud=)` names the boot speed when the board's
profile is not 115200.

## The fake probe (a working spec)

`endpoint.Endpoint` is a fake probe that answers as oep-spec says (every answer carries the length of its data or list,
TLVs may follow anything, one resource number space, describe = declarations and `state` for the rest, capture
generations); ch32rv, this client and the probe firmware are checked against it (when the
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

The rule changes of 2026-10-02 (oep-spec `docs/v1-rule-change-proposal-2026-10-02.md`) are in: a value a later revision
may define is unsupported, a request is checked form first, then values, then state; a non-repeating request TLV twice,
a short one malformed, a longer one unsupported (critical) or ignored; ignored at most 16 entries with 0x00 as the 16th;
confirm names its transport and refuses an unknown revision with the range it handles; rejected answers are remembered;
lease 1000-60000; session_id 0, booleans and text checked; count = 0 leaves out channels with an idle item, a named
output idle is unavailable cause 5 holder_kind 7; undeclared combinations unsupported with their index; found =
DMSTATUS.version >= 2 and not 15 (`FakeTarget.version`); halt / step failures (`halt_stuck`, `step_stuck`);
`search_retries`; the line search's step (c) over the firmware's labels; an idle pull a channel lacks (`no_pull`); an
item's channel below `channels` and not reserved; label text; a saved bind on a port that is no serial port. What the
probe does to a pin is `pin_state(ch)` (MISO driven only while CS is active - `spi_select` -, an i2c-target open-drain
with the pull-ups it declares - `fake.with_i2c_pullups` -, a plan changing no pin until use, a closed connection's pins
back to idle). `fake.with_unit_id(probe, "x-...")` makes a probe with an `x-` unit_id.

So are the rules since (oep-spec 2e70f40 .. 40291a4, `docs/v1-rule-change-proposal-2026-10-06.md`): an op the interface
does not define, or an optional op the probe does not declare (riscv-dm's block / run / reset / step by features, capture's
query / force, probe.config's save / erase without storage), is unknown_operation before session_required (`offers`);
plan_apply and probe.config set / unset check the whole request's form, then the fns it names, then what the probe lacks,
then the state; non-request roles and short requests are discarded; the clock is ns since boot (`now_ns=` for its
resolution; fake_serve uses `time.monotonic_ns` and draws its boot_id), never back, from 0 at `reboot()`; confirm's bounds
and max_op_ms are checked when an `Endpoint` is built; every channel not reserved is parked at boot. A riscv-dm op whose
target does not answer fails with status line and leaves the lines undriven until one succeeds (`pin_state` says
`wire-free`); the attach reset TLV on an output-idle channel is unavailable cause 5 holder_kind 7; `search_retries` only
when a bring-up ran; i2c-target refuses the reserved addresses, a uart write without TX is unavailable cause 6, and
`esp32-v003`'s spi-target declares a `cs_setup_ns`. No profile has a pinless wire, so a pinless scan is not exercised.

And the 2026-10-06 simplification (oep-spec b69ec26 .. 9d4baf9, `docs/v1-simplification-proposal-2026-10-06.ja.md`): one
10-byte request header with session_id (0 = none; a session's request with 0 is session_required, a lock-free one is
handled without the session check); TLVs as tag(u8) len(u16) value; every sequence count x element with no element
length; fixed forms closed - an item or a request TLV longer than its form is unsupported when critical, else ignored
(probe.config's idle is 6 bytes with drive_kind 2 = default, a slot carries boot_reset after attach, slot_state
reset_at_ns before the tid); every fn's describe carries the `ops` tag (`fake.FakeProbe` fills in every op of the
interface's table when a profile gives none; `fake.ops_of(name, *without)` leaves optional ones out), and an op not set is
unknown_operation; no resume - end, a lapse and force release everything the session created, a request of an ended
session is no_session, the open answer is lease_ms and boot_id, the resend table answers a resent end; console streams
stay the probe's per place and mechanism (closed and readable until the next open there, which returns the number with
its position and marks); `oep.probe.link` (every profile has it; `esp32-v003`'s offers port_speed): source
answers len(u16) data, sink takes count(u16) data. Since f0c68bf a console stream has a send queue of
the describe's send_queue (256 here): a write takes min(count, the free space), 0 only when it is full, SDI nothing;
the probe hands its head to the target 2 (dmseq) or 3 (DMDATA) bytes a poll, one poll per ms of the timers' clock
while the hart runs (`console_take(sid)` hands all of it); it stays over resets and restarts and goes when the stream
closes. A reset (riscv-dm reset, attach's reset TLV) waits out a target that restarts by itself
(`FakeTarget.restart_ms`) up to reset_settle_ms, then answers status line (`settle_log`). An item longer than its form
is ignored or, critical, unsupported (d34dafa); oep.probe.link source answers at most max_frame - 26 (4bd3a87). An attach
that joins a live connection keeps the settings it does not carry (idle_clock absent keeps the current rest; max_speed
only lowers the speed), and a scan leaves a live connection's settings (59dd028). `tests/test_vectors.py` runs
sessions.json step by step and every ops.json case on the fake in the state it names.

And the 2026-10-06 structure (oep-spec 289bde0 .. 498ae95): fn 0 is the core - no name, never in list (p4-x035 lists
15 interfaces, so a whole dump makes 16 describe requests) - and its ops are exactly the eight mandatory ones (confirm,
list, describe, clock, open, end, keepalive, lock_state); clock answers boot_id and uptime_ns, the clock read while
handling the request, with session_id 0 touching no session, lock or lease. Every profile lists `oep.probe.plan`
(plan_apply / plan_release, plan_roles 32 in its describe; a probe made with an interface that has plan roles and no
plan gets one at the next fn) and `oep.probe.restart` (restart, restart_max_ms 2000) after the fns it had before them,
so no earlier fn number moved: p4-x035 plan 14 / restart 15, esp32-v003 11 / 12, p4-bench 8 / 9, rp2350-pins 7 / 8.
`fake.without(probe, name)` leaves an interface out (`fake.without(p, fake.RESTART)` is a probe without restart).
logic, analog and capture-group set subscribe / unsubscribe (0x30 / 0x32) in their ops; every other fn, fn 0 included,
answers them unknown_operation. A subscription's min_bytes / max_delay_ms hold a streaming capture's data (until min_bytes
bytes wait, or max_delay_ms since the oldest of them; both 0: at once), never an event; no heartbeat, the captures
declare no features. A profile's ops tag outside core §7.4's encoding (a test's, `fill_ops=False`) is served as given.

Other programs' tests run `fake_serve` as a child process:

```sh
python -m oep_client.fake_serve --pty --profile p4-bench --slot x035 --bind last-reset \
    --console 'uptime %d\r\n' --every 100
# first line: PTY /dev/pts/N (PORT n with --tcp 0); it ends when stdin closes (not with --keep-on-eof)
```

A line `reboot` on its stdin reboots the probe mid-session (`Endpoint.reboot`) with a new random boot_id: the session
table, the resend table, connections, streams, subscriptions, the plan and the unsaved settings go, the saved
settings (`--slot`, `--bind`, `--label`, `--uart-plan`) apply again, and a request with the old session gets
no_session; confirm and open show the new boot_id. The pty or TCP connection stays open. stderr says
`fake_serve: rebooted, boot_id 0x........`; any other line is ignored with a message on stderr. oep.probe.restart's
restart (oep-if-restart; every profile lists the interface with restart_max_ms 2000 in its describe; `--no-restart`
makes a probe without it - not listed, its fn unknown_function) does the same from a request: the answer goes out
first, then the probe reboots, and what it had read behind the request is dropped:

```python
# the test holds the child (stdin=PIPE) and writes the line
proc.stdin.write(b"reboot\n"); proc.stdin.flush()
```

The pty is a serial port (the host opens it with TIOCEXCL); `--tcp PORT` is `--framing cobs` (a serial port) or
`--framing length` (the TCP form: the listener is a TCP transport of the probe, listed in describe and named by every
confirm; a length over max_frame closes the connection). Faults: `--drop N` (the N-th answer is not sent, once; the request did run,
so a resend gets the remembered result), `--noise TEXT` (noise before every answer), `--corrupt N` (the N-th answer's CRC
broken once). `--uart-plan` / `--uart-rx` give the first fixture UART a plan and RX bytes, `--run-hook` a host's own model of
riscv-dm run, `--capture-slipped` flags bit2 on every capture segment. `--no-drive-levels` takes the gpio's
drive_levels away (a probe that cannot switch the output strength). `--silent-until-reset N` makes the N-th pair's target
answer nothing until a reset through its line; with `--boot-reset` (every `--slot` asks for the at-boot retry with reset)
the retry with reset happens at start through the slot's `nrst` line - a `--label CH=TEXT` (e.g. `23=v003.nrst`), or
the firmware's fixed `NRST` label (probe.config §1.3 step (c)). port_speed: `esp32-v003`'s `oep.probe.link` offers it
(`--no-port-speed` takes it out of the ops), and `--broken-rate RATE[:MIN_SIZE][:in|out]` makes a rate break frames (in process,
`fake_serial.FakeSerialStream` also garbles everything while the host's own rate differs from the probe's). Events and data pushes go out on the pty and on TCP
(both framings). The rest: `--help`.
