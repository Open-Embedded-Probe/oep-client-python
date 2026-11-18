# Checks of the wire attach options

[日本語](wire-options-conformance.ja.md)

`WireOptionsChecks` checks §1/2/3/5 of the selected SPEC's `oep-if-debug.ja.md` and core §2.3/8.1
in the third stage `oep-interface`. Pass `Checks(send, registry, unit)` and
`SampleWireOptionsAdapter(source)` explicitly and call `WireOptionsChecks(checks, adapter).run()`.
The common declarations of all 4 fns are separated into the second stage `InterfaceChecks`.
The public code does not search for equipment or private benches.

The explicit logical fixture has RVSWD/SWIO/SWD/console.
It declares reset role 3 on channel 6, max_op_ms=5, and a slowest read of 1000 Hz.
These numbers, times and speeds are not required of other probes or equipment.

- RVSWD idle_clock 0 means clock high, 1 means low. data is high. When omitted, this fixture's target has clock high.
  SWIO data is high; SWD is data low / clock high.
  An undefined idle value is unsupported with the received raw tag. For SWIO/SWD, idle_clock is an unknown tag:
  if critical, unsupported with the received tag (with the critical bit); if noncritical, it is ignored and attach proceeds.
- The host gives the reset channel every time. role 3 not provided, an undeclared channel, or hold_ms over the limit are unsupported.
  Conflicts with an existing connection, its own wire pins or an external reservation are cause 1; disabled/output idle is cause 5.
  All requests are checked before reset, including max_speed 0 and broken widths (the fixed part is debug §1).
- reset pulls low open-drain, stops driving after the given hold, and returns to the core's free state.
  The channel is not carried over into the connection. Without a reset TLV, the default line is not moved.
- After releasing reset, it waits at the slowest speed until the target answers.
  RVSWD/SWIO logically handle DMSTATUS, SWD the DP response.
  The fixture advances 1 ms per failed read and has an example that recovers after 2 lines and one that gives up at the overall limit.
  It checks the attach flags/dpc after recovery; a failure is completed line.
  No new connection number is assigned, the tried pins are released, and existing connections/streams and the driving state are kept.
- RISC-V method 1 returns the halted flag and dpc. Since the hart may run between reset release and halt,
  this is not a guarantee of stopping before the first instruction. SWD's dormant wake flag is told apart from the RISC-V flags.
- SWD targetsel is given explicitly as critical. The tid in connections is the DPIDR read at attach (scheme 2, 4 bytes), and
  the host-given targetsel is returned separately in response TLV 0x01 selected_target (entry_index, targetsel) (debug §2.1). Without it, the TLV is not attached.
  On the same pins, a new attach is refused whether targetsel is the same, different or absent.
  To choose another target, detach first and attach with a new resource number.
- The value of a known TLV does not change with the critical bit, and for duplicates the first is used. Later copies are also validated, and undefined values are unsupported.
  A resend of the same attach request does not repeat reset/resource allocation.
- detach and session end release the stream/connection and also stop the idle_clock driving.
  A halted hart is not resumed/reset on its own.

`WireOptionsModel` is an independent logical fixture and does no electrical driving, waveforms or target communication.
`wire_behavior(ok|dm-recover|dm-line|dormant)`, `wire_reset_support(bool)` and
`wire_pin(unowned-channel, free|disabled|output|reserved)` are explicit stimuli.
The reserved state is a logical stimulus in place of the plan allocator and is not evidence of plan integration conformance.
Each case is a new boot and stores raw exchanges, pin mode/level, reset/read times and resource/target metadata.

The unit tests include fault injection, missing preconditions, invalid stimuli, refusal of changes to owned pins and common declarations.
`full_conformance` is false. scan idle_clock/targetsel, physical open-drain/level/hold/wake/DM timing,
the attach speed/scratch algorithm, the response/release order on a broken wire, the policy of idle=0 for non-RVSWD,
plan integration / other capacities, the real response writer and production Host integration are not checked.

```sh
uv run pytest -q tests/test_conformance_wire_options.py
```
