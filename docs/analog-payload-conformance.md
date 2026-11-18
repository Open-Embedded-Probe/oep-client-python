# Checks of analog success payloads and raw data

[日本語](analog-payload-conformance.ja.md)

`AnalogPayloadChecks(checks)` checks capture §1.2/3.2/3.3/3.7 of the selected SPEC in the third stage.
The caller states explicitly the success payloads of configure/query, the success payload of calibration, the captured data and the known raw samples,
the plan's roles, and describe's frontend numbers and mode.
layout.order is the plan's role numbers themselves. Gaps in numbers are handled too, and each row of raw samples is passed in ascending role number.
For example, with roles=(1,3) and order=[3,1], the second value of each row goes into the first slot.
The public checker does not search for equipment, private benches or a neighboring SPEC, and does not use the Host's capture decoder.

Checks of wire operations, generations and data lifetime combine this payload check with
`AnalogChecks` in the [analog operation guide](analog-conformance.md).

`run(configure_payload=..., calibration_payload=..., data=..., expected_samples=...,
roles=..., frontends=..., mode=...)` returns 3 items.
Successful resolution, wire width/transport, SID and resource lifetime are checked by the caller's other checks. Passing payloads alone is not proof of conformance of the execution.

- It checks positive num/den of actual_rate, layout's s=8/16/32, b=1 to 31, o+b≤s, C and the permutation in order.
- actual_samples for mode 1/2 and actual_segments for mode 2 are required, and checked to be 1 or more.
  Values returned for other modes are not used (the SPEC decides only the modes where they are required, so their presence is not a violation).
- One scale per plan role is required. skew may be omitted for roles with 0 delay. frontend_used is optional diagnostic information,
  checked only for the roles returned (it may cover only some roles). Unknown roles, duplicate roles and undeclared frontends are detected.
  zero/scale are i32. A negative scale, a zero outside the code range and scale 0 are not refused by added conditions.
- reference is optional. Width and source/how are checked only when returned. No invented rule excluding 0 mv.
- factory: one per frontend (0xFF does not depend on a frontend), scheme of 1 to 64 bytes and its character set, and raw_len/u16 matching raw are checked.
  The meaning of raw is left to the scheme's definition, and empty raw is also accepted. An empty success for the whole calibration is also accepted.
- vrefint is checked for 16 bytes only when present. Unknown non-critical response TLVs are ignored, and for non-repeating TLVs the first is used.
- data is N·C·s/8 bytes. Only o/b of the little-endian slots are read and matched with the known raw samples according to order.
  Unused bits may be 0 or 1, and factory values or scale are not applied to raw data.

The unit tests check 16 configurations of s/o/b, forward/reverse order and unused bits set to 0/1, and faults in structure, required information, enums, roles, calibration and raw data.
They detect a scale missing for some roles, and accept omission of frontend_used/reference/skew and a frontend_used response for only some roles.
Reading with the Host decoder, clipping of negative scale and conversion to the input pin are also compared with known values.
It is also applied to VirtualCapture's real success payloads and raw captures, and detects forbidden characters in a calibration scheme.

Whether the calibration response fits max_frame (§3.7) depends on the wire transport, so it is not checked here; it is checked by CALIBRATION-FIT of `AnalogChecks`.

`full_conformance` is false. Capture/refusal/resend/generation/trigger/notifications/resource lifetime, all analog operations, capture-group,
per-scheme interpretation of raw and guarantees of physical reference/skew are not checked. Schema checks alone do not count these as done.

```sh
uv run pytest -q tests/test_conformance_analog_payload.py tests/test_virtual_bench_capture.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_analog_payload.py
```

## Settings requests of the virtual bench and unchanged state

`test_virtual_bench_capture_requests.py` passes wire bodies and checks frontend selection/width/undeclared values/duplicates,
refusal of rate/samples/mode/trigger, and that a successful query does not apply settings.
It compares capture/config/data/segments/plan/GPIO before and after refusal. It is not a check that requires the lease/cache of the core that received the request
to stay unchanged. The core may legitimately renew or keep request history.

In state 5 with full segments, configure is refused with unavailable cause 6, and the stored segments and data are not erased.
Both logic/analog are checked. The rule that allows query even in state 5 with a plan is checked by SPEC §3.3 and
`test_virtual_bench_capture_query.py`.
This is an interface regression of the virtual bench's `Endpoint`, separate from the checks of the independent core conformance fixture (`core-v1`).

```sh
uv run pytest -q tests/test_virtual_bench_capture_requests.py
```

## Generations and data lifetime of the virtual capture

`test_virtual_bench_capture_lifetime.py` checks re-start of logic/analog and the u32 generation boundary, no change on refusal of read/release of an old generation,
release not applying to one-shot, and inclusive release for repeat.
A release that frees no segment stays full. It resumes only when space actually opens, and keeps the gap, the running position and
start_ns reflecting the model clock at resume. It enters state 5 when the last free segment completes.

stop finalizes the partial samples already taken as a short segment and does not create an empty segment.
error does not withdraw completed segments/data, and the next start updates generation/data/flags.
stop after an error returns to state 1 while keeping the data. The short segments of group stop are also checked on both tracks.
This is a check of the virtual model's logical time, not proof of hardware clock accuracy or the whole group contract.

```sh
uv run pytest -q tests/test_virtual_bench_capture_lifetime.py
```

`test_virtual_bench_capture_resume_calibration.py` repeats repeat's pause/release/resume 3 times and
checks that the wire records and raw data of the remaining segments, the generation and the original trigger time are kept.
It uses logic/analog, 3 kinds of fractional rates and release of 1/2 segments, and requires a gap only on the first segment after resume.
With a callback of known waveforms, it also checks that no channel values ahead of the model's current time are read.

The same check confirms that calibration's vrefint represents a measurement between the last start and the query time.
It includes the 3 modes, single/group start, immediate / waiting for a trigger, and re-start.
This fixture measures the reference voltage synchronously at start, and the first channel sample starts 5 µs after that.
So vrefint's ns and the segment's start_ns are not necessarily the same value.
This is the fixture's measurement model, not a check that requires the same delay or immediate measurement of general probes.
An empty calibration from a probe without measured values is still accepted. Physical ADC accuracy and measurement procedures are not checked.

```sh
uv run pytest -q tests/test_virtual_bench_capture_resume_calibration.py
```

## Notifications of the virtual capture

`test_virtual_bench_capture_notifications.py` reads the fixed part of raw events independently and also compares with the Host decoder.
It matches generations and polling results for logic/analog one-shot completion, short stop, error, repeat full/release and reachable triggers.
Nothing is sent when not subscribed, and when full the reserved stopped reason 2 is not sent.
Old notifications left in the queue until after a new start keep their original generation, and it also checks that the Host drops those not of the current generation.
Fault injection of altered generation/time/stop reason, missing notifications and unneeded triggers is detected.
stopped's error is matched only when reason is 3, and 0 is not required otherwise.
This is a regression of the fixture's notification behavior. Unreachable triggers, conformance of all notifications / streaming / concurrency and all analog operations are not checked.

```sh
uv run pytest -q tests/test_virtual_bench_capture_notifications.py
```

## Unreachable triggers and force

`test_virtual_bench_capture_waiting.py` uses cross_up threshold 0 on an unsigned ADC and
checks that state 2 is kept when the condition does not hold, and that no notification/data/segment is created.
It also checks no change on refusal of re-start/configure, stop, error 1/2/3 and release not applying.
force uses the current sample and shortens the segment only by the pretrigger amount still missing.
capture-group binds only immediate tracks, so group waiting and force are not handled here.

As with the one-shot fixture, future samples after the condition holds are synthesized immediately.
This is not proof of a physical waiting time. Natural early triggers and triggers of all modes are not checked.

```sh
uv run pytest -q tests/test_virtual_bench_capture_waiting.py
```

## Virtual bench with sparse analog roles

`test_virtual_bench_analog_roles.py` checks, for plans with only roles 1/3, 0/3 and 3,
configure/query, the assignment checks of frontend/trigger, raw data and Host parsing.
query does not change configured data, and refusal for unassigned roles does not change capture/plan/GPIO either.
It uses known sample sequences that tell roles 1 and 3 apart. It is not proof of the whole analog/group contract.

```sh
uv run pytest -q tests/test_conformance_analog_payload.py tests/test_virtual_bench_analog_roles.py
```

## The first natural trigger and missing pretrigger

`test_virtual_bench_capture_pretrigger.py` checks that pretrigger is not used as a waiting time, and that capture happens at the first time the condition holds.
Missing history is returned as a short segment, and sufficient history keeps only the requested count.
trigger_index is the position within the kept segment, not the sample number from the start of capture.
It matches logic edge/level, analog cross_up/down (including sparse roles), pretrigger 0 and missing/sufficient,
raw data, time and generation-tagged notifications.
Samples before the start are not used for edge/crossing comparison.
one-shot waits for the time of the last sample before publishing the segment. During capture, no segment is returned.
It is used together with the [capture time checks](capture-metadata-conformance.md#time-checks-of-the-virtual-capture). Physical latency and the contracts of all modes are not checked.

```sh
uv run pytest -q tests/test_virtual_bench_capture_pretrigger.py
```

## query not applied even during capture and while bound

`test_virtual_bench_capture_query.py` queries states 0 to 6 with a plan and each group-bound state.
It queries from another Host that has not opened, and checks that the current settings/data/segments/generation/plan/GPIO/bind/notification queue do not change.
Checks that compare state fix the clock. A separate check advances time and checks that capture proceeds with the original settings while
the candidate rate/samples/mode of query are not applied. Without a plan, cause 6;
an invalid rate 0 during capture is malformed and does not change the current capture.
Refusal of configure during capture / while bound continues and is not treated the same as query.
It is a check of the virtual bench's `Endpoint`, not a check of the independent core conformance fixture or proof of the whole query contract.

```sh
uv run pytest -q tests/test_virtual_bench_capture_query.py
```

## Start trigger of repeat/streaming

`test_virtual_bench_capture_trigger_modes.py` checks logic/analog in both modes.
With a start condition that does not hold, state 2 is kept and no data/notifications/segments are created.
force and the first trigger of known waveforms keep history, and only the first segment of repeat gets trigger_index / short when missing.
A midway stop also keeps that index. streaming sends triggered and data, and does not send segment notifications.
streaming's running numbers and write_pos on error are computed from the actual completed segments, not counting the dropped pre-start history.

Known waveforms also detect the condition from elapsed samples only. Signal changes after waiting are checked by the separate test below.
Physical latency, temporal causality of all modes including offset/skew, and storage gap / the contracts of all stream segments are not checked.

```sh
uv run pytest -q tests/test_virtual_bench_capture_trigger_modes.py
```

## Trigger signals that change after waiting

`test_virtual_bench_capture_changing_trigger.py` checks single captures of logic/analog in all 3 modes.
It uses a fixture that replaces a signal that does not hold at start with a waveform that maps sample numbers to time.
The waveform does not change past samples, and changes from source sample 20.
While waiting, only elapsed, not yet observed samples are checked, and the value just before an edge/crossing is kept.
Querying again at the same time does not rescan, and re-start resets the observed position.
Trigger notifications and captured raw use the same signal source, `VirtualCapture.value(role, sample)`.

This fixture is a timestamped input function and does not prove asynchronous GPIO/ADC input on hardware.
It does not search for future triggers at start and does not synthesize one-shot data ahead of time.
Notifications are matched separately at the time a natural trigger/force holds and at capture completion.
Real-time latency and independent conformance of storage gap / concurrency are needed separately.

```sh
uv run pytest -q tests/test_virtual_bench_capture_changing_trigger.py
```


## Channel times

A natural analog trigger waits for the observation time including the role's skew, and returns that time as trigger_ns.
The nearest base sample does not necessarily match the frame of the channel that detected the condition (for example the last channel of 4).
force's trigger_ns is the execution time and is not rounded to the track's offset or sampling period.
The scope is stated in the [capture time checks](capture-metadata-conformance.md#time-checks-of-the-virtual-capture).
The data, generation and plan role decisions of captured segments are used together with these time decisions.
