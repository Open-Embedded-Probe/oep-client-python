# Logical operation checks of capture-group

[日本語](capture-group-conformance.ja.md)

`CaptureGroupChecks(checks, adapter).run()` checks capture §4.1, plan §2 and core §4.3/4.4/6/8/9 of the explicit SPEC
in the third stage. Pass `Checks(send, registry, unit)` and `SampleCaptureGroupAdapter(source)`.
The public checker/adapter do not search for private benches, sibling checkouts or equipment settings.
The common declarations and group/track metadata are applied separately.

- Unbound state 0, the time sentinel before start (all ones) and generation 0. Refusal of start with cause 6, no-op stop and empty bind.
- bind order, re-giving the same list is a no-op (state and data are kept even during capture), and bind does not change the generation.
- Refusal of bind and no change. A duplicate fn is malformed, an fn not in tracks is unsupported (mark 0, fn TLV).
  Unconfigured / a state other than 1, 4 or 6 (capturing alone) / mode mismatch / a non-immediate trigger are cause 6, a track bound in another group is cause 4,
  and lack of simultaneous resources (different rates in this fixture) is cause 2. The cause and the fn TLV of the track concerned are read back.
- bind has no v1 TLVs. An unknown critical TLV is unsupported with the tag as received, and a TLV without the critical bit is ignored.
  The same holds for the request TLVs of start/stop/status.
- On a 64-byte transport, a bind of 8 tracks whose start response (18+6n) does not fit is refused with unsupported (mark 0), and 7 tracks, which fit, are accepted and can start.
- While bound, single configure/start/stop/force and plan_apply/plan_release are refused with cause 4. Re-giving the same plan set is a no-op.
  query does not apply candidates even while bound or capturing, and does not change other tracks or the bind.
- A group that is capturing (state 3) refuses an empty bind / another list with cause 6. A completed track (state 4) can be re-bound to another list and started.
- The start preconditions are checked for all tracks first, and a re-start during capture is cause 6. It checks the fn list in bind order in the start response, independent generations of the group and each track,
  and the status readback. The last generation 0xFFFFFFFF can be used, and a later start is refused with cause 2 for the group and for tracks.
  The generation continues even after re-binding.
- The captured raw ADC/logic data, the common start time of completed segments and immediate segments (trigger_index 0xFFFFFFFF, flags 0).
- group stop stops all tracks and keeps each partial segment as short.
- A repeat pause of a single track makes group state 3. release is per track and does not change the peer's segments/bytes.
- A capture failure of ADC or logic gives group state 6, stops the peer, and keeps completed segments.
- bind/start/stop on SID 0 are session_required. status can be read on SID 0.
- Double side effects of bind/start/stop resends. end / lease expiry / forced open release the bind and plan and
  return to the unconfigured state while keeping the generation. It also observes that the pins' electrical state is not changed during capture.

The independent `CaptureGroupModel` has ADC/logic tracks and a group.
It reuses the settings, encoders and segment rings of the independent `Analog`/`Logic`, and implements the group's bind/start/lifetime separately.
It does not take in `virtual_bench_capture.VirtualGroup` or the Host's capture decoder.
The ADC oracle is `AnalogPayloadChecks`; for logic, the assigned bits are read independently and matched with the raw values.

The fixture's fns are plan=1, ADC=2, logic=3, group=4. ADC role1/3 are assigned to channel0/1,
logic role0/1 to channel2/3. The maximum channel count of both is 2.
As auxiliaries, fn 5 has another group (the counterpart for cause 4), and fn 6 to 12 have logic with 1 role (channel fn−2),
stated explicitly in `adapter.auxiliary`. channels in core describe is 16.
max_frame is 256 bytes (64 only for the frame check, with an explicit `frame(64)`), one-shot/repeat, track force present,
no group/track subscribe. The capture resources handle only groups with the same rate; different rates are refused with cause 2.
The checker's capture stimulus is 1000 Hz and passes paired rows with explicit ADC 12-bit values and logic 0/1 values.
This restriction is not added to the general OEP group spec.

Each element of `feed(rows)` is `{2: [ADC role1, role3], 3: [logic role0, role1]}`.
The logical sample clock advances one row at a time.
`advance`/`fail(fn,error)`/`seed(group,adc,logic)`/`frame(limit)` are explicit stimuli.
`invalidate(fn)` injects a broken start precondition and checks that other tracks are not started first.
Wire exchanges, group/track state, data, segments, pins and the clock are recorded in the adapter trace.

The unit tests detect wire response faults, semantic faults and double side effects on resend for each case.
Detection of semantic faults is confirmed down to failing with the check's `Violation`.
Missing preconditions, invalid stimuli, and that the fixture's channel advertisement matches the implementation are also checked.
That the metadata structure is correct and that the fixture matches its advertisement are separate checks.

`full_conformance` is false. subscribe/stopped notifications and per-track notifications, streaming,
other resources or group configurations, failure of start itself (completed failed), serial retention boundaries,
concurrency / persistent config, physical synchronization/skew/accuracy and the production Host are not checked.
Success of a finite synchronous fixture is not treated as all group operations or hardware conformance.

```sh
uv run pytest -q tests/test_conformance_capture_group.py tests/test_conformance_analog.py tests/test_conformance_logic.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_group.py
```

The first segments of tracks with different actual_rate started together immediately are also checked by a separate checker in the [group timing check guide](capture-group-geometry-conformance.md).
That check states the raw observed payloads and the known sample origins explicitly,
and does not extend the capture capability of this same-rate independent operation fixture.

Independent capture with different rates is checked by a separate fixture in the [capture timeline guide](capture-timeline-conformance.md).
The resource limits of the same-rate operation fixture are kept as they are.
