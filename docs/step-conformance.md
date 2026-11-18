# Checks of optional step and re-halt

[日本語](step-conformance.ja.md)

`StepChecks` checks §4/4.2 of the selected SPEC's `oep-if-debug.ja.md` and console §3
in the third stage `oep-interface`. Pass `Checks(send, registry, unit)` and
`SampleStepAdapter(source)` explicitly and call `StepChecks(checks, adapter).run()`.
The common declarations of all 5 fns are checked in the second stage of `InterfaceChecks`.
The public code does not search for equipment or private benches.

It uses an explicit logical fixture that provides optional step.
It does not require a step implementation of probes that do not provide it.

- On success, it returns status ok, PC before/after and moved. Success even if the PC stays the same on a self-branch.
  The transition from the top of the u32 PC to 0 is also checked. Even if step was set from the start, it is cleared at the end.
- dcsr.prv is not changed. prv 0/1/3 are checked.
- resume happens only once. If the hart does not return to debug, it tries to re-halt with haltreq.
  If the re-halt succeeds, step is cleared and the status is state. If not, the hart keeps running;
  haltreq is cleared, status state and a zero-length step_left are returned, and since cleanup cannot finish,
  the connection and its stream are closed after the response (debug §4.2).
  step_left is a notice that the step bit may remain; remaining is not a required condition.
- If status is not ok, moved and both PCs are 0. The combination with completed failed is checked.
  On a line/fault failure during the explicit fixture's preparation, it does not resume and does not change the PC.
- Saved values are restored in DATA1→DATA0 order, and hartsel/haltreq are cleared.
  abstractauto and unrelated GPRs/memory are not changed.
- A console poll on the same connection is inserted in the middle of step, and it checks that scratch is not touched.
  The mailbox/frame/pending are kept on success, re-halt and continued running alike.
  After running resumes, the saved dmseq output is delivered.
- critical / broken TLVs / truncated arguments, session priority order, and invalid resources / streams / ARM resources are checked.
  A resend of the same request does not repeat step/resume and returns the saved result as is.

`StepModel` is an independent logical model that adds step to the block fixture.
`step_seed(connection, pc, dcsr)` sets the state before saving, and
`step_behavior(mode, after_pc)` sets the result. mode is
`ok` / `late-halt` / `running` / `prepare-line` / `prepare-fault`.
It handles logical PC transitions and state restoration; it does not run CPU instructions or abstract CSR commands.
Each case is a new boot and records raw exchanges, PC/dcsr, the DATA restore order, haltreq and console arbitration.

The unit tests include fault injection (including the error of keeping the connection after step_left), a normal case that passes even when the step bit is cleared with step_left,
missing preconditions, invalid stimuli and common declarations. `full_conformance` is false.
Physical instruction/CSR operations, the real time of re-halt, failure of DATA restoration itself, acceptance of a hart running from the start,
the real response writer, reset/run, and integration with other targets and the production Host are not checked.

```sh
uv run pytest -q tests/test_conformance_step.py
```
