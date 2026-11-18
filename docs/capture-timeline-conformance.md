# Checks of group capture with different rates

[日本語](capture-timeline-conformance.ja.md)

`CaptureTimelineChecks` is a third-stage check based on capture §2/3.2/3.4/4.1 and core §10 of the explicit SPEC.
`SampleCaptureTimelineAdapter` drives the clock and the notification draining of the independent `CaptureTimelineModel`.
Private benches, the endpoint, sibling checkouts, the Host's capture decoder and other capture checkers are not taken in.
The layout is solved independently from the configure response, and the raw data is matched against known waveforms.

In this fixture plan=1, ADC=2, logic=3, group=4.
ADC role1/3 and logic role0/1 are assigned to separate channels, and one-shot with an immediate trigger and streaming are declared.
The group, as in v1, has only immediate bulk start, without trigger, pretrigger or force.
Each track takes samples on an independent rational grid starting from the group's start_ns (index 0 is the start time).
`advance_to(ns)` is the logical time since boot, and `drain()` takes out the notification frames made up to that time.
For sample index i, the waveforms are ADC role1=(17i+3) mod 4096, role3=(4095−29i) mod 4096,
logic role0=i mod 2, role1=⌊i/3⌋ mod 2. These are controls for stimuli and do not add new ops to the OEP wire.

It checks 24 configurations.

- mode is one-shot (16 samples) and streaming. The bind order is (ADC, logic) and (logic, ADC).
- The rates of the first/second tracks are 1000/333, 333/1000 and 1000/500 Hz.
- The start time is right after boot and 1234567 ns later (a start_ns not aligned to a ms boundary).

For one-shot it checks the following.

- The start response is start_ns(u64), generation(u32), n, n×(fn, generation); generations advance by 1 and do not wrap.
  The same values are read back in the group's and tracks' status.
- write_pos during capture counts only the settled bytes of samples that have arrived (not including logic's unsettled last byte).
- read answers a position at or above write_pos (write_pos, +1, +4096) with position=write_pos, gap 0, len 0.
  max=0 is also an empty success, with position and gap as for a normal read. The more bit matches what remains up to write_pos.
  One-shot retained data can be read from position 0 with no gap, and a read of an old generation is refused with cause 6.
- After completion, each track has one segment (serial 0, position 0, 16 samples, start_ns=group start_ns, trigger_index 0xFFFFFFFF, flags 0, generation).

For streaming, it checks that a group start before subscription is refused with cause 6, then subscribes both tracks (min_bytes 0, max_delay 0) and starts.
Notifications are taken out at 2/9/23/41/58 ms, and at 58 ms the group is stopped and the rest is received.

- Data frames are within max_frame, and every frame has TLV 0x01 generation and 0x02 segment_info (24 bytes).
- The position/start_ns/uncertainty of segment_info match the segment in segments, and start_ns is a time on the track's grid.
  samples is 1 or more, does not decrease, and does not exceed the number of samples that had arrived when the frame was made.
- A frame's data is within one segment and does not cross a segment boundary. Until a segment ends, only settled bytes are sent.
- The received positions continue from 0 without gaps and end at write_pos after the stop (this fixture does not drop).
- Segments continue without gaps, and only the last one is short. No segment events are sent, and stopped comes once with reason 1, error 0 and the generation.
- read need not return data sent by notifications. If, after advancing by gap, there is no readable data, position=write_pos, gap 1, len 0.
- The number of samples taken until the stop matches the number that arrived, and the bytes of the assembled segments are matched against the known waveforms.

In both modes, it checks that after session end the bind, plan, subscriptions, notifications and captured data are released, the pins return to idle, and the generation is kept.

The unit tests include the normal case, wire response faults, track faults, group faults, declarations, invalid stimuli and missing preconditions.
Track faults inject counting samples not yet arrived, unsettled bytes, raw data, segment times, wrong read position/gap/max=0,
frames crossing segments, missing segment_info / sample count / time, the generation TLV, and segment events in streaming.
Group faults inject a response with generation 0, accepting start without a subscription, subscriptions left after the end, and an offset start_ns.
All 24 configurations are reported together.

`full_conformance` is false. It checks a known logical fixture with 2 tracks and immediate start.
repeat/release, drops in streaming from lack of send room or subscription loss, other group configurations, ADC layout/skew,
retention boundaries, races during asynchronous capture, physical clock accuracy and the production Host are needed separately.
The time matching is against this fixture's integer ns representation; it is not a guarantee of a physical probe's accuracy or a tolerance based on the reported uncertainty.

Stop, errors, running out of generations, start failure, the state table and session lifetime are checked by the [separate lifetime checker](capture-timeline-lifecycle-conformance.md).

```sh
uv run pytest -q tests/test_conformance_capture_timeline.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_timeline.py
```
