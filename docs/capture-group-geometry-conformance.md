# Checks of immediate group start and timing

[日本語](capture-group-geometry-conformance.ja.md)

`CaptureGroupGeometryChecks(checks).run(...)` checks capture §2/3.3/4.1 of the explicit SPEC in the third stage.
The success payloads of configure/start/status and the first 37-byte segment record of each track are passed as they are.
The probe's encoder, the Host decoder, private benches and sibling checkouts are not taken in.

capture-group v1 defines only immediate bulk start, and does not define linked trigger/pretrigger or a mapping of trigger_index from time.
This check also handles only one-shot immediate start.
The caller states explicitly the members in bind order, the time of each track's first sample (an independently known origin) and the tolerance for time matching.

It returns 3 items.

- CONTEXT: num/den of actual_rate and actual_samples, the fixed shape of group start (start_ns, the group's generation, n, fn and generation in bind order).
  start_ns is not all ones, and the generation is not 0.
- STATUS: the 13-byte fixed shape, state 4 for one-shot completion, and the start time / group generation matching the start response.
- SEGMENTS: first serial/position 0 of each track, the track generation, the sample count equal to actual_samples,
  no gap/short/reserved flags (bit2, time base bent, is allowed), immediate trigger_index.
  The segment's start_ns is matched against the known origin and tolerance, and it checks that nothing was captured before the group's start_ns.

For trigger_index of an immediate segment, 0xFFFFFFFF (a segment with no trigger) or 0 is accepted, and the observed value is recorded.
The SPEC does not state which one an immediate-start segment has, so it is not narrowed to one.

Estimated sample times generally do not match real time exactly.
This checker's time matching tolerance is decided by the caller from the resolution/conditions of the known fixture.
`start_uncertainty_ns` is not a SPEC guarantee, so the tolerance is not widened automatically from the reported value.
The 1 ns tolerance of this logical fixture is not applied as is to a physical probe.

`capture_group_geometry_model.fixtures()` builds raw payload/record examples.
They include integer/fractional rates, a 20MHz and 48kHz pair, per-track origin offsets, a trigger_index 0 example,
large u32 num/den and start_ns near the top of u64, 3 tracks and skipped fn numbers.
This is a generator of metadata examples, not a model that implements a capture engine or large sample buffers.
The configure examples also carry the normal required information besides rate/capacity, but verifying all layout/scale and so on is another checker's job.

The unit tests include the normal case, invalid observations/preconditions (including wrongly shaped start/status), not using the reported uncertainty,
and the configuration of the virtual bench's `Endpoint`. The Endpoint configuration is logic/analog bind order and 3 pairs of in-range rates.
The real response bodies are taken from the wire, and the origin is computed from the group's start_ns and the ADC's known conversion delay (5000ns) and matched.
It is not a check that proves conformance of the `Endpoint`/Host core or op numbers.

`full_conformance` is false. Wire refusal/resend/advance of all generations, feasibility of bind, all configure/data structures,
repeat/streaming/notifications, short segments on stop and physical clock/skew/accuracy are not checked.
It is combined with the [independent group operation checks](capture-group-conformance.md), but
that same-rate fixture is not treated as a completed independent capture engine with different rates.

```sh
uv run pytest -q tests/test_conformance_capture_group_geometry.py tests/test_conformance_capture_group.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_group_geometry.py
```

The [mixed-rate capture timeline](capture-timeline-conformance.md) does not use this geometry checker;
it checks the group start, each track's segments and raw data itself.
