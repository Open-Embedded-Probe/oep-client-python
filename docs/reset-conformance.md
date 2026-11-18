# Checks of optional reset and DM return

[日本語](reset-conformance.ja.md)

`ResetChecks` checks §4/4.3/4.6 of the selected SPEC's `oep-if-debug.ja.md`, console §2/3 and
common mark §1 in the third stage `oep-interface`.
Pass `Checks(send, registry, unit)` and `SampleResetAdapter(source)` explicitly and
call `ResetChecks(checks, adapter).run()`. The common declarations of all 5 fns are separated into the second stage
`InterfaceChecks`. The public code does not search for equipment or private benches.

It uses an explicit logical fixture that provides optional reset.
It does not require an implementation of probes that do not provide reset.

- mode is only 0 (run) and 1 (stop before the first instruction) (debug §4.3). 2 and 255 are unsupported (mark 0 in the fixed part).
  flags has only bit0 reached; the other bits are 0. success only when reached is set and cleanup is also done.
  The PC for mode 0 is 0; mode 1 returns dpc.
- It tells apart success in the 2 modes, state-reach timeout, DM line failure, cmderr fault and cleanup failure.
  A failure keeps the same fixed response shape as completed. When cleanup (clearing haltreq) fails,
  it answers with flags reached and failed with status line, and closes the connection and its stream after the response.
- ndmreset is set and cleared. mode 1 keeps haltreq on release and clears it before the response (the hart stays halted).
  mode 0 also clears it before the response. hartsel and ndmreset are set to 0 before the response, and the reset pin is not moved.
- If DMSTATUS after ndmreset release is line, it waits and reads again. The logical fixture has max_op_ms=1,
  reads up to 5 times at 200 us intervals, and has an example that recovers after 2 lines and one that stays line to the end.
  The reads are done before the overall limit. When the DM does not come back, the connection/stream are kept, or the connection and its stream are closed after the response (debug §4.3).
  This interval/count is not required of other probes. Resending a wire wake is optional, so it is not a required condition.
- When the DM answers, havereset is acknowledged. Each stream's dmseq returns to unsynchronized, and pending is dropped.
  The live buffer/position/send queue are kept. When the reset op succeeds, exactly one reset mark (0x40/detail 1 ndmreset) is added, and
  no restart mark (0x41) is added for the same reset. This is also checked with a wire marks read.
  When the DM does not come back and havereset is not observed, there is neither a mark nor a havereset acknowledgement. Dropping dmseq synchronization when not observed is not a pass/fail condition.
- A console poll on the same connection does not touch scratch during reset, and
  does not affect the target/mailbox/protocol/stream of another connection.
- Unknown mode, critical / broken TLVs / truncated arguments, and session and resource refusals happen before any side effect.
  A resend of the same request for success / DM line failure does not repeat reset, mark, ack or checks.

`ResetModel` is an independent logical model that adds reset to the step fixture.
`reset_behavior(result)` chooses `ok` / `state-timeout` / `dm-recover` / `dm-line` /
`fault` / `cleanup-line`.
It states the logical state, order and PC explicitly; it does not run a physical DM or CPU instructions.
Each case is a new boot and records raw exchanges, ndmreset/DM read/ack/restoration, and buffer/mark/pending.

The unit tests include fault injection, a normal case that drops synchronization preventively even when the DM does not come back,
missing preconditions, invalid stimuli and common declarations. `full_conformance` is false.
Physical ndmreset/wake and DM return time, failure of the havereset ack itself,
the real response writer, run, other targets/capacities and production Host integration are not checked.

```sh
uv run pytest -q tests/test_conformance_reset.py
```
