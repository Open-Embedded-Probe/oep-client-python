# Checks of stop, resend and lifetime for a mixed-rate group

[日本語](capture-timeline-lifecycle-conformance.ja.md)

`CaptureTimelineLifecycleChecks` is a third-stage check based on capture §2.1/3.2/3.4/4.1 and core §6/8/9/10 of the explicit SPEC.
It uses the same independent model/adapter and known waveforms as the [capture timeline](capture-timeline-conformance.md);
ADC or logic is the fast one at 1000Hz and the other the slow one at 333Hz (one-shot is 16 samples).
No private bench, endpoint or Host capture decoder is needed.

For each of the 2 fast choices it checks 20 cases, 40 configurations in total.

- stop is done while both are capturing (9 ms) and when only the fast one has completed (20 ms).
  The partial segment of samples that arrived, short, start time, generation, serial_done, write_pos and raw data are matched.
  A stop on a completed track does nothing and succeeds, as the state table says.
- On a capture failure of the fast or slow one, the failed partial segment is not published, and the other is stopped.
  It checks the error value, the read of the failed track (write_pos 0) and the peer's short segment. If the peer has already completed, it keeps state 4 and its data and segments.
- A start resend is done during capture, and a stop resend after the partial segment has been completed.
  It checks that the response and state of the same request match and that the generation and data do not change.
- query is sent to both tracks during capture and when one has completed.
  It answers the 537Hz/5 samples the probe returned as candidates, and checks that the original settings, timeline and data are kept.
- Session change by end and force is checked during capture; lease expiry during capture and in a state holding retained data after capture completes.
  The bind, plan, subscriptions, captured data and segments are released, and the pins return to idle. The generation is kept.
- Running out of generations: if the group's generation is 0xFFFFFFFF, group start, and if a track's generation is 0xFFFFFFFF,
  group start and that track's own start, are refused with unavailable cause 2 before any side effect. The generation does not go back to 1, and the state does not change.
  The other generation advances independently.
- start initialization failure: the start of the first or second track is made to fail, and it checks that group start returns completed failed
  with the same fixed shape as on success (start_ns, the advanced generation, fn/generation in bind order).
  The group and the failed track are state 6 (with error), the other is state 1. The generation is not rolled back, and no data is published.
  A resend of the same request returns the same response, and the next start succeeds. A failed start of a single track also returns completed failed with generation(u32).
- State table: on 1 track that is not bound, it checks configure/query without a plan, and configure, start, stop and release in states 0/1/3/4/6.
  release validates the generation first even for one-shot, and does nothing if it matches. stop in state 3 emits a short segment, and stop in state 6 returns to 1.
- streaming error: after part of the segment being captured has been sent by notification, a failure is caused.
  write_pos is the end of the last published segment, and bytes at or after the received write_pos are to be dropped.
  The failed segment is not listed in segments, and stopped has reason 3 and error. The peer stops and ends with reason 1.

It checks that sample capture does not continue when the clock advances after a stop or an error.
A later start advances the generation, erases the earlier data, and refuses a read of the old generation with cause 6. The segment times and raw data of the resumed one-shot are also matched.

The lease check does not only confirm cleanup on the next wire request.
For the 1000 ms lease taken at start, the adapter observes that it is owned at 999 ms and released at 1000 ms,
then reads lock_state. status/read, which need no session, do not extend the lease.
This model puts the deadline into the same scheduler as sample arrival, and at the same time handles expiry first.
This is a control on the fixture's logical clock and does not guarantee physical clock accuracy.

The unit tests include the normal case, wire faults, semantic faults, resend side effects and missing preconditions.
Semantic faults detect a missed stop of some tracks, a missing short, erasure of completed segments, wrong generation/write_pos, changes by query,
cache left / generation reset at the end, old data / time base on restart, a delayed deadline, a missed peer stop on error, publishing the failed segment,
error value, write_pos, erasure/state change of the completed peer, generation wrap / side effects when used up / wrong cause,
a success response on start failure, a short fixed shape, state, the peer continuing to capture, rolling back the generation,
the state table (accepting configure during capture, erasing completed data, release that does not look at the generation, refusing start from state 6,
stop in state 6, query without a plan, side effects of stop in state 0), and write_pos / publishing the failed segment / reason / the peer continuing on a streaming error.
All 40 configurations are reported together.

`full_conformance` is false. It is a check with sequential stimuli for a known 2-track / immediate-start setup.
repeat/release, waiting for a trigger and force (state 2/5), other configurations, ADC layout/skew, retention boundaries,
races of simultaneous requests and errors, physical time and the production Host are checked separately.

```sh
uv run pytest -q tests/test_conformance_capture_timeline_lifecycle.py tests/test_conformance_capture_timeline.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_timeline_lifecycle.py
```
