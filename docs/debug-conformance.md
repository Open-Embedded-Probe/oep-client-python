# Logical checks of riscv-dm dmi, halt/resume and console arbitration

[日本語](debug-conformance.ja.md)

`DebugChecks` checks §4/4.1/4.2 of the selected SPEC's `oep-if-debug.ja.md` and
§3 of `oep-if-console.ja.md` in the third stage `oep-interface`.
Pass `Checks(send, registry, unit)` and `SampleDebugAdapter(source)` explicitly and
call `DebugChecks(checks, adapter).run()`. The public code does not search for equipment or private benches.

The explicit fixture's fn 5 provides the required dmi of standard riscv-dm and the optional halt/resume.
The cases that send halt/resume (HALT-RESUME, halt/resume of HARTSEL-*, HALT-FAILURE, RESUME-FAILURE) run only when the fixture declares them in ops (debug §4).
The common declarations of all 5 fns are checked separately with `InterfaceChecks`, apart from the second-stage results.

- dmi n=0, mixed write/read/wait, order of different values, done/status/nvals, and no execution of the suffix after a midway failure.
- poll_reads/poll_us match, timeout, and a single read with a limit of 0. A poll timeout adds the last value;
  if the read failed because of a bad line, no value is added. status is matched with the registry's timeout=4 and so on.
- An unknown kind (validation stops at that position, unsupported, mark 0 in the fixed part), a truncated step (malformed),
  and an excess in the sum of time arguments are refused before the whole request runs. Earlier steps do not run either (debug §4.1).
  It checks critical TLVs, the structure of invalid TLVs, the resource kind/family and the session/header priority order.
- The probe does not automatically restore values the host wrote to DATA0/DATA1 with raw dmi. A resend does not repeat target accesses.
- Even when a console poll on the same connection is injected between the dmi steps, it does not touch DATA0/DATA1.
  After the response returns, it reads DMSTATUS and then goes back to the mailbox.
- Mailbox stop during halt, return after resume, and issuing resumereq only once.
  A halt timeout clears haltreq, and a resume failure returns status state / completed failed.

The independent `DebugModel` is a logical fixture that adds riscv-dm to the 4 wire/console fns.
max_op_ms is 1, and the logical cost of a DMI access is 10 us. The case where poll_reads reaches the limit of the whole request is also checked.
These are not values that prove real-clock DMI performance or wait time conformance.
It is stimulated with `debug_failure(index-or-None, status)`, `debug_hart_failure(bool)` and
`debug_registers(connection, u8-to-u32-dict)`, and
`debug_state()` observes resources, accesses, console poll arbitration and buffer/pending.
Each case does a logical restart into a new boot. No target or hardware is operated.

The unit tests include fault injection, missing preconditions, invalid stimuli and common declaration checks of all 5 fns.
```sh
uv run pytest -q tests/test_conformance_debug.py
```

`full_conformance` is false. Optional reset/step/run/block operations and state restoration, hartsel,
physical DMI/timeout/line retries, scheduling of other connections, the order of the real response writer and production Host integration are not checked.
Passing logical arbitration within one connection is not treated as all debug operations or hardware conformance.

The response length boundary is also checked. A request whose value array alone exceeds max_frame is refused with
unsupported / fixed part 0 before touching the target, even if it starts with writes.
Within the boundary, all values are returned. If there is no room for optional auxiliary TLVs, they are left out.
The unit tests check the Debug/Block/Step/Reset/Run/Retry/Cut models on a 64-byte transport, and
also detect oversized responses, writes before refusal and boundary overruns from auxiliary TLVs by fault injection.

hartsel of high-level halt/resume is also checked. On success/failure, and even for a halt
when already halted, hartsel is returned as 0. resume and halt timeout clear haltreq. Raw DMI does not
clear what the host set automatically. The state of other connections is kept. It also checks that on failure no register
other than DMCONTROL changes. This is a check of the logical state of DMCONTROL, not a physical observation of
selecting among several real harts. hartsel of optional ops remains a separate check scope.

ARM-ADI raw transfer and the basic block are handled by the [independent ARM-ADI checks](arm-adi-conformance.md).
The RISC-V DMI/hart state checks are not reused for ARM. Posted reads and raw ACK are check conditions on the ARM side.
