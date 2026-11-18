# Checks of optional block operations and state restoration

[日本語](block-conformance.ja.md)

`BlockChecks` checks §4/4.5 of the selected SPEC's `oep-if-debug.ja.md` and core §7.4
in the third stage `oep-interface`. Pass `Checks(send, registry, unit)` and
`SampleBlockAdapter(source)` explicitly and call `BlockChecks(checks, adapter).run()`.
The public code does not search for equipment or private benches. The common declarations are separated into the second stage with `InterfaceChecks`.

The explicit fixture provides read_block/write_block as a pair and
declares max_length=8 bytes. This value is not required of other probes.

- It checks the unit/width of max_length, the op pair, fitting into the selected transport's frame and the count limit.
- 4-byte alignment of address, count over the limit and truncated write data are refused before any side effect.
- count=0 is success / done 0 whether the hart is halted or running.
  With count>0 and the hart running, status state / completed failed, done 0.
- done and outcome of the executed prefix, the order of read values, and no execution of the suffix after a write failure.
  A new read after target memory is changed by an external stimulus is checked, and the probe-side stale copy is not used.
- On both success and partial failure, GPR s0/s1/a0/a1 and DATA1/DATA0/abstractauto are restored to their values before being touched.
  Unrelated GPRs are not changed. DATA1 is restored before DATA0, and a high-level op returns with hartsel 0.
- Polling the console on the same connection during block does not touch scratch.
  The live dmseq frame is saved and restored, and it checks that the original output arrives after resume.
- A resend of the same read/write request does not repeat bus accesses and does not recompute the result.
- A nonexistent connection is no_resource; giving a console stream as the connection is unavailable cause 6.
- If restoring DATA (cleanup) fails, even when all words were transferred, it is completed partial (done>0) with a failure status value.
  The connection and its stream are closed after the response, and the next request gets no_resource (debug §4).

The independent `BlockModel` adds optional ops to the mandatory debug fixture.
`block_memory(connection, aligned-u32-to-u32-dict)` is a logical stimulus on the target bus,
`block_scratch(connection, gpr-to-u32-dict)` the GPRs before saving,
`block_failure(index-or-None, status)` a failure just before a word, and `block_restore_failure(bool)` a cleanup failure.
The state stores bus accesses, restore order and memory/GPR/mailbox/pending, and each case is a new boot.
It is not an implementation of physical abstract commands, program buffer or sysbus.

The unit tests include fault injection, missing preconditions, invalid stimuli and the common declarations of all 5 fns.
`full_conformance` is false. Frame constraints of all transports, physical GPR restore failure,
the algorithms of abstract command/program buffer/sysbus, RV32 address overflow and bus access rights,
reset/step/run, the real response writer, other capacities and production Host integration are not checked.
The external memory stimulus checks alone are not taken as proving real target memory changes through dmi/run.

```sh
uv run pytest -q tests/test_conformance_blocks.py
```
