# Partial checks of the Host's save wait time and transfer time

[日本語](host-wait-conformance.ja.md)

Name the SPEC checkout explicitly and run `HostWaitChecks(SampleHostWaitAdapter(), spec=path).run()`.
It matches the reviewed SHAs of the body texts of core §4.4/7.5/13.7 and transports §6, the wait values and the current save number.
It also matches the registry's core_quick_op_max_ms (1000).
It does not search for private equipment and uses only MemoryStream. It does not open OS ports or wait on a real clock.

It goes through Host.expecting, the timing of settings.Settings.save, and SerialLink._write/_wait/_limits.
save sends the production fn 0 save (core §13.7; the number is looked up from the registry) as is, and records that number in each observation.

By core §4.4, the lower bound of the wait is "the processing limit of the whole request + 1 second + transfer time". By core §13.7, save
finishes all of its saving within max_op_ms, so the processing limit stays max_op_ms.
For fn 0 confirm, the processing limit is min(max_op_ms, core_quick_op_max_ms), 1000 ms before max_op_ms is known, and the wait is at least 2 seconds + transfer time.
The production Host chooses the processing limit per request (Host.op_bound_ms). The sample passes the production confirm through as is.
The report has production_opcode_alignment_proven=true and production_quick_bound_proven=true.

HOST-WAIT-SAVE covers all combinations of max_op_ms 1/5000/600000, max_frame 64/256/65535, an outer wait of 0/7000 ms,
length framing and UART current baud 1200/115200/3000000; HOST-WAIT-FAILURE makes save fail on a part of them.
HOST-WAIT-LIMITS passes a confirm history through length and UART 115200/3000000 (each confirm with a processing limit of 1000 ms, max_op_ms 5000).
Even on a speed-change path with a short configured timeout of 0.05 seconds and lease of 1 second, the wait does not go below save's max_op_ms + 1 second + transfer time.
The UART transfer time uses the length L of the frame actually written and the latest max_frame: (L + max_frame×3)×10/baud.
L for save including COBS/CRC is 15 bytes; for length framing it is 12 bytes, with a transfer time of 0.
A longer wait than needed is allowed. The lower-bound match in float computation allows an error of 1e-12 seconds, but does not set a tolerance for physical timers.

The outer long Host.expecting is not discarded and is restored after save succeeds or fails.
Before the confirm response arrives, max_frame64 is used; after that, the latest response value.
With a real Host.confirm history of 256→64→65535, the values used for each request's wait are 64→256→64.
It does not compute with a new value the current confirm has not yet returned, or with a past maximum.

The unit tests detect omission of argument time / margin / transfer / notification, confusion of message length and wire length, shortening by lease or a short timeout,
loss of the outer wait / leaks on failure, and invalid save projection numbers / results / latest confirm.
The real start of the deadline / timeout occurrence / resend, waiting for earlier responses in a pipeline, the link's own negotiation wait,
processing limits of other ops, real transport/flash and the whole integration with the production Host are not checked. full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_host_wait.py
```
