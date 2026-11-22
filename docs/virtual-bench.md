# Virtual bench (the spec that runs)

[日本語](virtual-bench.ja.md)

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
