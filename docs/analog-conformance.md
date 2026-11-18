# Logical operation checks of analog capture

[日本語](analog-conformance.ja.md)

`AnalogChecks(checks, adapter).run()` checks capture §1.2/2/3/3.7, plan §2 and core §4.4/6/8/9 of the explicit SPEC
in the third stage. Pass `Checks(send, registry, unit)` and `SampleAnalogAdapter(source)`.
The public checker and adapter do not search for private benches, sibling checkouts or equipment settings.
The common declarations and capture metadata are checked by separate checkers.

It reuses the check procedures of `LogicChecks` for wire exchanges, generations, data lifetime and resource lifetime,
and reads analog success payloads and raw data independently with `AnalogPayloadChecks`.
The Host's capture decoder and the virtual probe's encoder are not used to generate the expected values.
Combining several tracks is checked by a separate checker in the [capture-group operation guide](capture-group-conformance.md).

- configure/query/start without a plan, whether a SID is needed, the settings contract, widths, zero values, critical equivalence, raw tag and first-wins.
  pretrigger without a trigger and configure/query with an undeclared mode 3 are refused with unsupported for that tag.
- The rate range, samples rounded down to the capacity, the repeat segment count rounded down to the limit and its value when omitted, trigger type/role/value, pretrigger.
- The response requires actual_rate/layout, actual_samples for mode 1/2, actual_segments for mode 2 and a scale per role.
  skew, frontend_used and reference may be omitted; their shape and roles are checked only when returned.
- With a plan, in states 0 to 6, query does not change the capture settings, generation, data, segments or pins.
- role 1/3, layout.order=[3,1], 16-bit slots with offset 2/bits 12. Raw data with unused bits set to 1 is
  read back by role number in little endian. A negative scale or calibration is not applied to stored raw values.
- Waiting for an upward/downward threshold and 2 samples of prehistory, trigger_index of the completed segment and the generation.
- frontend selection/omission, width, undeclared values, unplanned roles, and refusal of duplicates of the same role. On refusal the capture does not change.
  The result of the selection is checked by the sign of scale, and frontend_used is matched only when returned.
- The start response is only generation(u32). The first generation; a start after FFFFFFFF is refused with unavailable cause 2 and not reused even after configure.
  Refusal of read/release of an old generation, data erasure on re-start/configure.
- read has u16 max/len. max/more/gap, non-consuming reads on SID 0. At or above write_pos: write_pos, gap 0, len 0.
  When all segments are released and no data is readable: write_pos, gap 1, len 0. max=0 also follows the same position and gap rules.
- Segment paging, repeat full/pause, inclusive release/resume, remaining segments and data, gap on resume.
- short stop, dropping the incomplete segment and keeping completed segments on error, start after error.
- Double side effects of start/release resends, and resource closing by plan replacement/release/end/expiry/forced open.
- calibration queries not changing anything, factory kept, and the fixture's reference value and time for the last start.
- If the calibration response fits max_frame exactly, all of the factory raw is returned. If it exceeds by 1 byte, it is refused with
  unsupported with mark 0 in the fixed part; truncating raw, omitting vrefint or partial do not count as success. The same holds when vrefint grows at start and exceeds it.

The independent `AnalogModel` is a fixture with one-shot/repeat, with query, without force/subscribe.
Rates of 100 to 1000 Hz are exact, samples limit 16, repeat segment limit 2, pretrigger limit 8. describe's mode 1 has max_segments 0,
rate_range is min/max, and frontend has only its number and input range.
It has 2 frontends and 2 ADC channels, and assigns role 1/3 to channel 0/1.
max_frame is given explicitly as 256 bytes. That is a size the whole required configure response for 2 channels fits in,
and it does not require this size of every probe. calibration is refused with unsupported (mark 0) if it does not fit.

Each row passed to `feed` is u12 values in ascending plan role number. The fractional period is accumulated according to the number captured
and converted to logical ms. `advance`/`fail`/`seed` are explicit stimuli for the clock, capture failure and generation boundary.
`factory` is a stimulus that sets the length of the factory raw the fixture holds (0 to 4096 bytes), and it returns the set raw.
The model's common capture state machine is shared with the independent `LogicModel`, but the ADC settings, trigger, encoder and calibration are a separate implementation.
The bench's `virtual_bench_capture` and the Host decoder are not taken in.
reference is a fixture value measured synchronously at start's logical clock, and does not define the measurement delay of general probes.

The unit tests detect wire response faults for each case, semantic faults and double side effects on resend.
Semantic faults include rolling the generation back to 1, missing scale, truncated calibration / omitted vrefint / partial,
a missing gap when no data is readable, and accepting an undeclared mode 3. Passing a fixture that omits optional response rows,
invalid stimuli and missing preconditions are also checked. It runs together with the common declaration/metadata, logic and analog payload checks.

`full_conformance` is false. group, streaming/notifications, optional force/subscribe,
other ADC shapes / rate / trigger boundaries / reference voltage measurement methods, serial wrap / retention boundaries / concurrency / persistent config,
physical ADC accuracy/skew/clock and production Host integration are not checked.
The structure of an empty success from a probe without calibration is accepted by the payload checker.
This is not a check that requires this fixture's vrefint value or its presence of general probes.

```sh
uv run pytest -q tests/test_conformance_analog.py tests/test_conformance_logic.py tests/test_conformance_analog_payload.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_analog.py
```
