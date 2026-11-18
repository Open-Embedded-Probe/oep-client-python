# Pipeline execution order checked from resource side effects

[日本語](execution-order-conformance.ja.md)

`ExecutionOrderChecks` checks the common rules of core §4.4/9 with an explicit logical resource/pipeline fixture.
It observes the order of responses separately from the order in which resources are actually created, used and released.

It states sample fn1/2, kind1/2, max_frame64/window80/max_inflight3 and a sequential allocator.
`SampleExecutionOrderAdapter` adds a logical restart and resource/next-number snapshots to the pipeline adapter.
These are not public ops added to hardware, and they do not depend on a private bench.

- CLOSE-USE-CREATE: releases an existing ID, a use of the same ID becomes no_resource, and a new ID is created for another fn/kind.
- CREATE-CLOSE-USE: creates the next ID, releases that ID, and the use becomes no_resource. The other original resource is kept.
- Each batch runs in three states: writer budget 0/1/normal. On a stop or partial send, the real resources and the allocator are observed before the result completes.
- The corr/shape/outcome/ID of each result are matched independently, and a resend returns the original result without running the side effect again.

After all cases, the owned lock and resources are released.
The parts that predict future IDs are check stimuli based on fixture observation; they are not an example of normal Host resource use.
Because they assume a known next number, an arbitrary allocator or all-failure holes are not fitted to this fixture's rules.

It detects ordered replies after reverse execution, correct replies with a false final resource, broken reply order, and delayed creation side effects.
It does not check all ops, simultaneous arrival, long execution or physical backend timing. `full_conformance=false`.

```sh
uv run pytest -q tests/test_conformance_execution_order.py
```
