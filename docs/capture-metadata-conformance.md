# Describe checks of capture

[日本語](capture-metadata-conformance.ja.md)

`CaptureMetadataChecks(checks, selection).run()` runs with the fn/names of logic/analog/capture-group given explicitly.
It is the third stage for revision 1 and includes the registered-op checks. All requests are reads on SID 0.

The capture operations of analog are checked by a separate checker in the [analog operation guide](analog-conformance.md).
For group binding, simultaneous capture, generations and lifetime, see the [capture-group operation guide](capture-group-conformance.md).
Success of describe alone does not prove conformance of capture operations or data lifetime.

- capture: features (0x05) defines no bits in revision 1, so it may be omitted when the value is 0. If present, only its u32 shape is checked,
  and bit values do not cause a failure (they may be defined later, capture §3.5/§4.3).
- track: one or more plan role_channels (0x01) or channel_group (0x06); the 9-byte fixed shape of mode with one row per mode;
  max_segments 0 for mode 1/3; the 8 bytes of rate_range with 1≤min_hz≤max_hz; channels u8; the 8 bytes of trigger with types including immediate (bit0).
  At least one mode is declared, and which of 1/2/3 is chosen does not matter. release/subscribe, which become required per mode, are checked by the [registered-op checks](registered-ops-conformance.md).
- analog: one or more frontends with the 9-byte fixed shape (frontend, range_min_mv, range_max_mv), one row per number.
- capture-group: the element count n≥1 of tracks and the shape of the u16 list.

`CaptureMetadataModel` is an independent fixture that holds only the declarations of the standard interfaces.
The common declarations of all fns and the registered ops are checked separately.
The fixture declares mode 1/3 and subscribe/unsubscribe on fn 14, mode 1/2 and release on fn 15, and sets the probe's channels to 3.
It does not implement the advertised ops and is not used as a complete probe. The fixed shapes checked here are not evidence of the real behavior of capacity/rate/frontend
or of conformance of track bind. configure/capture/read/release/notifications/generations/plan lifetime need additional checks.
`full_conformance` is false. The public side does not depend on private benches.

```sh
uv run pytest -q tests/test_conformance_capture_metadata.py
```

From the Arduino uv/pytest environment, give the path of the public client explicitly.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_metadata.py
```

## Time checks of the virtual capture

Regressions of capture behavior run separately from the describe checks.

```sh
uv run pytest -q tests/test_virtual_bench_capture*.py tests/test_virtual_bench_analog_roles.py tests/test_capture.py
```

`test_virtual_bench_capture_initial_trigger_time.py` checks 12 configurations with an injected clock and waveforms: logic/analog,
one-shot/repeat/streaming, single/group. It does not search future waveforms at start;
it stays waiting until the condition is first observed in elapsed samples, and emits no trigger/data notifications.
The trigger time and generation after detection are also checked. Even in tests with known waveforms, the clock is advanced explicitly before detection is checked.

query works with a plan in states 0 to 6 and group-bound as well, and does not change settings, captured data, generation or bind.
The regression of this rule is checked by `test_virtual_bench_capture_query.py`.

Completion of one-shot capture is checked separately by `test_virtual_bench_capture_one_shot_time.py`.
With a natural trigger, force and immediate capture, single/group, logic/analog, it checks that a read before the segment completes is empty,
that no segment/stopped completion notification is sent before completion, and the generation and one-time notification after completion.
A midway stop keeps only the elapsed samples as a short segment, and a capture error does not publish the incomplete segment.
Even when the realized rate is fractional, it completes after the last sample and the skew of analog's last channel have elapsed.
A 1-sample logic capture can complete at start time, and analog waits for the start offset.
Keeping reads of an incomplete segment empty is this fixture's storage policy, not a rule that forbids other Probes from returning part of what was captured.
The nanosecond boundary tests are also part checks of this model clock, not checks that set the hardware time uncertainty to 0.

The virtual waveform is a function that returns a value from the sample index, and past values are generated at completion and at stop.
This check uses that function's time-stamped input; it is not proof of a buffer that continuously observes and stores real input.
Full temporal causality including the offset/skew of repeat/streaming, and hardware conformance, are checked separately.


`test_virtual_bench_capture_channel_time.py` checks channel times and force/group.
The analog condition decision waits up to the skew computed from the position in layout.order, not from the numeric plan role.
With sparse roles on 2 channels and the last role on 4 channels, it queries between the base sample time and the channel time.
The notification time of a natural trigger is the time the condition held on that channel. The group also keeps this time and
maps the trigger_index of each track, including the source, to the nearest base sample. The direction of choice at equal distance is not fixed.
force notifies the execution time, and that time is not moved by offset or sample rounding.
Before the first sample it is index 0; otherwise the distance to the nearest sample is checked.

The repeat/streaming tests check, with logic/analog, 1/2 channels and 3 actual_rates, that future channel values are not read.
In fault injection, only the capture clock is advanced by 24 samples, and the same time oracle detects generation of future data.
The tests through Endpoint use an integer-ms clock and the skew advertised by configure, and match the notification times and generations of both source and group.
The remaining boundaries of pause/resume/stop, the capture history buffer, real transports and the physical clock are checked separately.
