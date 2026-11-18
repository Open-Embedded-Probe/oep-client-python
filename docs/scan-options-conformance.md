# Checks of scan idle_clock and targetsel

[日本語](scan-options-conformance.ja.md)

`ScanOptionsChecks` checks §1/3/5 of the selected SPEC's `oep-if-debug.ja.md` and core §2.3/5/8.1
in the third stage `oep-interface`. Pass `Checks(send, registry, unit)` and
`SampleScanOptionsAdapter(source)` explicitly and call `ScanOptionsChecks(checks, adapter).run()`.
The common declarations are separated into the second stage `InterfaceChecks`. The public code does not search for equipment or private benches.

Only explicit candidates are tried, a finite number of them, and no connection is created.
The fixture adds scan to the model of the wire attach options ([wire-options](wire-options-conformance.md)) and uses max_op_ms=5, a default candidate limit of 2,
a slowest speed of 1000 Hz and a logical clock of 1 ms per candidate. These values are not required of other probes.

- RVSWD idle_clock omitted/0 means clock high, 1 means low. data is high.
  This is told apart from SWIO's data high and SWD's data low/clock high.
  The actual logical levels at read time are recorded, and the order reserve→drive→read→release of each candidate is checked.
  When TARGETSEL is sent, one select is inserted before read.
- SWD scan targetsel gives a single u32 (the number is looked up from the scan context of the selected SPEC's registry; a context separate from attach's targetsel).
  Only the given target is tried. Without it, TARGETSEL is not sent and a multidrop target is not chosen automatically.
  There is no fallback to another selector and no automatic search of several selectors. An explicit 0 is also told apart from omission.
- If DPIDR can be read, an arm-adi entry is returned. For RISC-V, DMSTATUS.version 2 or higher and not 15.
  The response's kind, ID, pins, order and tried are checked, and untried candidates are not reported.
- Without max_speed, the slowest speed; with it, at most that value. The first known TLV is used, and
  the meaning does not change with the critical bit. Invalid width or critical and options for unsupported wires are refused.
- count=0 / duplicate candidates are malformed. Even when undeclared/in-use/disabled/output/reserved pins come later,
  all candidates are checked before touching the first one. Owned pins/reservations are cause 1, disabled/output cause 5.
  Session priority order and invalid TLVs are also checked.
- tried is a finite prefix from the start of the request. With the limit-1 fixture, the untried prefix is named explicitly in the next request to continue the search.
  After each candidate, the pins return to the free state, and existing connections/streams/target metadata and idle settings do not change.
- A resend of the same request returns the saved result and does not repeat reselection, re-search or pin operations.

`ScanOptionsModel` is an independent logical fixture. `scan_identity(fn, pair, selector, id-or-None)`
states readable identifiers explicitly, and `scan_limit(1..3)` sets the finite prefix.
selector is used only for SWD, and None means omitted. It does not search wiring or real targets.
Per new boot, it stores raw exchanges, selection, levels/speed/ID at read time, the release order and the resource state.

The unit tests include fault injection, missing preconditions, invalid stimuli, common declarations,
and the attach options on this fixture. `full_conformance` is false.
Physical wake/dmactive/idle cycles/timing, the response/release order on a broken wire, other budget/frame paths and writer concurrency,
plan integration and production Host integration are not checked.

```sh
uv run pytest -q tests/test_conformance_scan_options.py
```
