# Finite scan checks of standard wires

[日本語](scan-conformance.ja.md)

`ScanChecks` checks §1/3/5 of the selected SPEC's `oep-if-debug.ja.md` as third stage
`oep-interface`. scan is an optional op, so the standard declaration checks
do not fail on its absence. This check's explicit fixture is required to provide scan.

Pass `Checks(send, registry, unit)` and `SampleScanAdapter(source)` explicitly and
call `ScanChecks(checks, adapter).run()`. The public code does not search for equipment settings or private benches.
The implementation version, the SPEC identity and each check's raw exchanges stay in the report.


- The RVSWD/SWIO/SWD identifiers, kind, candidate order, tried and count, unknown response TLVs.
- A finite number are tried from the start, and the host names the rest in the next request. It moves on even if nothing is found.
- The 0/1/2/3/14/15 boundaries of RISC-V DMSTATUS.version. For SWD, the DPIDR read is handled.
- count 0, duplicates, truncated candidates, undeclared pairs, invalid SWIO clock, pins in use / disabled / output idle.
  Even when a later candidate is invalid, the request is refused before any side effect, including access to the first one.
- The lower bound and width of max_speed, echo of the raw critical tag, unknown critical/optional, adoption of the first TLV.
- Pin release after each candidate, no resource or ID created, existing connection/console kept, session decision, resend.

The independent `ScanModel` is a logical fixture with 8 channels, explicit candidates and a default budget of 2.
It is stimulated with `scan_budget(1..3)`, `scan_id(fn, pair, u32-or-None)` and
`pin_mode(pin, disabled-or-output)`. `scan_state()` returns resources, pin reservation/release
and logical accesses. Each case uses a logical restart, which is not allowed while owned.
These are not electrical measurements.

The unit tests confirm the checks' detection power: besides the normal path, missing preconditions and invalid stimuli, they inject broken tried/kind/pin/ID,
resource creation, leftover pins, skipped candidates / implicit additions, access before refusal, ignored budget,
adoption of an invalid version, and broken resend results.

`full_conformance` is false. Physical wake/dmactive/scratch/pin idle, idle_clock,
targetsel, electrical timing/max_op_ms, other frame limits / concurrent writers and production Host integration are
reported as not checked. The standard console's mailbox and target debug operations are separate checks.

```sh
uv run pytest -q tests/test_conformance_scan.py
```
