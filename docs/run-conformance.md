# Checks of optional run and the loader state

[日本語](run-conformance.ja.md)

`RunChecks` checks §4/4.4 of the selected SPEC's `oep-if-debug.ja.md` and console §3
in the third stage `oep-interface`. Pass `Checks(send, registry, unit)` and
`SampleRunAdapter(source)` explicitly and call `RunChecks(checks, adapter).run()`.
The common declarations of all 5 fns are separated into the second stage `InterfaceChecks`.
The public code does not search for equipment or private benches.

It uses an explicit logical fixture that provides optional run.
run is a loader call and is not reused as a debugger continue.

- A normal stop is stopped 1 / success. Even if the PC stays at the start position it is a success, and resume is not issued again.
- If it can stop on timeout: stopped 0 / status timeout / completed failed.
  dpc and the requested values are returned as valid. If it cannot stop: stopped 2, dpc=0, nvals=0.
- A line/fault failure during preparation is stopped 3, dpc=0, nvals=0, elapsed_us=0.
  The hart is not run and is returned still halted.
- elapsed_us is the time from resumereq until the stop is observed or the stop is given up.
  Preparation time is not added, and the wait for the stop after timeout is included.
  The fixture uses 400 us of preparation, 100 us to a normal stop, and a stop wait of 200/300 us added to timeout_ms.
  This does not require real-clock accuracy or these times of other probes.
- The host-given PC/GPRs and dcsr.ebreakm/prv M are returned as changed.
  DATA1/DATA0/abstractauto are restored to the saved values, and hartsel/haltreq are cleared.
  The fixture restores DATA/abstractauto before resume. Physical abstract read/write is not handled.
- It checks 0 inputs / 0 outputs, the order of returning several outputs of the same regno, u32 PC and timeout=0.
  timeout_ms > max_op_ms is refused with unsupported before any side effect.
- Input/output arrays that do not match the counts, truncated arguments, broken TLVs and critical, and session/resources are checked up front.
  It checks an example where the n_out results fit the selected transport's max_frame exactly, and one that exceeds it.
  If the required fixed fields and values do not fit, unsupported. The limit is not exceeded for optional response TLVs.
- Memory/GPRs changed by the logical program are kept and reflected in the return values and later read_block.
  A console poll on the same connection does not touch scratch during run and keeps mailbox/pending/buffer/marks.
  Other connections do not change. The console resumes after the response.
- A resend for a normal stop / a timeout that was stopped / continued running returns the saved result and does not run the loader twice.

`RunModel` is an independent logical model that adds run to the reset fixture.
`run_behavior(mode, after_pc)` chooses `stopped` / `timeout-halted` / `not-halted` /
`prepare-line` / `prepare-fault`.
`run_program(connection, memory, gprs)` states the logical program effects after execution.
memory is aligned-u32 address → u32 value, gprs is index 0 to 31 → u32 value.
This logical run handles only abstract GPR numbers 0x1000 to 0x101F. This range is not made a limit of the standard spec.
It does not run CPU instructions, CSR/abstract commands, a real clock or transfers to a real target.
Each case is a new boot and records raw exchanges, resume/finish times, PC/dcsr/GPRs, restoration and console arbitration.

The unit tests include fault injection, missing preconditions, invalid stimuli and common declarations.
`full_conformance` is false. The frame limits of all transports, the policy for unsupported abstract regs / partial preparation,
accepting a hart that is running initially, failure of result reads / DATA restoration itself, a program writing to the mailbox while running,
physical CSR/CPU/clock, the real response writer, other targets/capacities and production Host integration are not checked.

```sh
uv run pytest -q tests/test_conformance_run.py
```
