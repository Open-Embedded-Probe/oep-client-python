# Logical checks of logic capture

[日本語](logic-conformance.ja.md)

`LogicChecks(checks, adapter).run()` checks capture §1 to 3, plan §2 and core §6/8/9 of the selected SPEC in the third stage.
Pass `Checks(send, registry, unit)` and `SampleLogicAdapter(source)` explicitly.
The public side does not search for private benches, neighboring repositories or equipment settings.
The common declarations and logic metadata are applied separately.

- It checks state 0, refusal of configure/query/start without a plan, empty read and stop, and that logic does not change the electrical state of pins.
- query does not apply the settings and returns the same actual values as configure. It checks required TLVs / widths / the omission contract / zero values / raw tag / first-wins.
  pretrigger without a trigger, and configure/query with an undeclared mode 3, are refused with unsupported for that tag.
- The response requires actual_rate/layout, actual_samples for mode 1/2, and actual_segments for mode 2.
  layout has w 1 to 128, C is the number of roles in the plan, and pos are distinct and less than w.
- It checks the exact rate range, samples rounded down to the capacity, the repeat segment count rounded down to the limit and its value when omitted, trigger type/role/value, and the pretrigger boundaries.
- It reads back the edge trigger wait and history and each value of the 2 captured channels stored as bits with width 3.
  Unassigned bits are set to 1 on purpose, and the checker looks only at the bits at pos. Unused trailing bits are not required to be 0 either.
- The start response is only generation(u32). It checks 1 the first time, cause 6 for read/release of an old generation, and data erasure on the next start.
  Starting from FFFFFFFE, a start after FFFFFFFF has been used is refused with unavailable cause 2 before any side effect, and it does not go back to 1 even after configure.
  configure and plan release do not roll back the generation.
- read has u16 max/len. It checks max/more/continuation, reads on SID 0 with no side effects, paging of 37-byte segments and inclusive/next cursors.
  A position at or above write_pos (including max=0) gives position=write_pos, gap 0, len 0.
  When repeat has released all segments and there is no readable data after the requested position: write_pos, gap 1, len 0 (also for max=0).
  After resuming, it advances from a released position to the next readable data and sets gap.
- When repeat is full, state 5. release frees completed segments inclusively, automatically returns to state 3, and puts a gap on the resumed segment.
  It checks the position and start_ns of consecutive segments and the read gap after release.
- A midway stop gives state 1 and a short segment. A capture error shows state 6/error/flags, drops the incomplete segment and does not withdraw completed data.
  A start after an error advances the generation and clears flags.
- A resend of the same start/release request does not repeat the generation, data or release.
  plan replacement/release/end/expiry/force drop the settings, data, segments and capture in progress and return to state 0.

The independent `LogicModel` is a logical fixture with one-shot/repeat, with query, without force/subscribe.
Width 3, 2 channels (role 0/1, channel 0/1), samples limit 16, repeat segment limit 2,
exact rate values of 100 to 1000 Hz, pretrigger limit 8. describe's mode 1 has max_segments 0, and rate_range is only min/max. It also listens to the pins' idle output as is.
`feed` takes explicit 0/1 samples in plan role order and advances the logical clock according to the number captured.
The sample period is accumulated with Fraction, and the clock is rounded to logical ms. The time uncertainty of a segment is 1 ms.
`advance`/`fail`/`seed` are explicit stimuli, not a physical sample source or real time.
Wire exchanges, samples, state, generation, data, segments and pins are saved in the trace.

The unit tests detect wire/packing errors, query applying settings, errors in generation/segments/resume/gap/short/error/pins, and double side effects on resend.
Rolling the generation back to 1, the old-shape start response, layout's C, missing actual_samples, accepting pretrigger without a trigger or mode 3,
a response that does not round segments down, the requested position of a future read, and wrong gap when no data is readable or with max=0 are also injected as faults.
Invalid stimuli, missing preconditions and unassigned bits are also checked.
`full_conformance` is false. analog/calibration, group, streaming/notifications, optional force/subscribe,
query during capture, missing history with an early trigger, other packing/rate/trigger, serial wrap / independent metadata lifetime,
persistent config, concurrency, physical sample/timing/uncertainty guarantees and the production Host are not checked.

```sh
uv run pytest -q tests/test_conformance_logic.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_logic.py
```

## Rate contract of the virtual bench

The rate of capture §3.3 uses the nearest feasible value within the declared range. It is not necessarily rounded down to at most the request.
`tests/test_virtual_bench_capture_rates.py` states VirtualCapture's internal choice of source/divisor explicitly and,
for small ranges, checks all requests with an independent oracle that enumerates all feasible values.
It checks logic's rounding up/down, equal distance and the min boundary, analog's shared ADC budget and fractional upper limit, the u32 source boundary,
an exact source (rate_range is declared with only min/max), query not applying settings, wire actual_rate and analog skew.
For two candidates at equal distance, both are accepted. This source-specific divisor is not required of other OEP probes.

This is an additional regression of the production virtual bench, and evidence separate from the exact rate checks of the independent LogicModel above.
It does not replace the third-stage checks of all analog capture/calibration, capture-group and streaming, or physical clock guarantees.

```sh
uv run pytest -q tests/test_virtual_bench_capture_rates.py tests/test_virtual_bench_capture.py
```
