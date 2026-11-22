# The `oep` commands

[日本語](cli.ja.md)

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
(`kept_session.KeptSession` in `Host.kept`; host guide §5). Before the host's first open, it ends the session the previous run left:
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
   A channel that some pin follows alone belongs to that pin, and a pin that follows only channels other pins have alone is shown as not wired.
   A channel something else holds high or low (a board's pull-down, for example) is never driven by the probe: each pin not found yet is made an
   output on the target side, one at a time, and that channel is read.
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
floating inputs (a channel something else holds is only read). The power channel is touched only with `--power`. Holding low is
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
and also duplex if the full form checks duplex, are each streamed for 1 second or more (`link.FAST_VERIFY_S`) at that concurrency. They
are judged by the same criteria as the flows (no re-run at n = 1). That trial asks for verify_ms 4000 (6000 if duplex is included), so the lease needs
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
speeds per (port, unit_id) in `~/.cache/oep-client/link-speed.json`, puts passed speeds first and drops failed ones. A passed speed is kept for 30
days and a failed one for 1 day; a failure measured within 2 seconds (`settle_s`) of a breakdown at another speed is "unknown". When all candidates are recorded as failed,
the slowest candidate is tried once (`speed.retried`). For a unit_id starting with `x-` (a probe with no unique number or storage, core §7.5),
nothing is kept or read. What is raised is the port this host came by (the transport TLV of the confirm response, core §7.1) when it is a UART bridge.
A port_speed request that comes on a port that is not a UART bridge, and a trial while a port is raised, are refused with unavailable (cause 6). On a probe without `oep.probe.link`, or whose `oep.probe.link` does not have port_speed,
the speed does not change (the result says why). Wire tests (`linktest`, `core.link_speed`) also need `oep.probe.link`. Behind a broker (TCP), the broker does it, not the client. A serial port is opened in low-latency
mode if the driver has it (on FTDI, the latency timer goes from 16 to 1 ms). If the board's boot speed is not 115200,
pass it with `open_host(..., baud=)`.
