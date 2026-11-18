# Checks of the Host's notification dispatch and deadlines

[日本語](host-notification-conformance.ja.md)

This checks the Host side of core §2.4/10.1/10.2 with an explicit reader adapter and a logical clock.
It does not replace the production link; it checks with a virtual sample.
It does not search for private equipment settings or a neighboring SPEC.

The adopted example is fn 7/8, known event kind 1 (a 1-byte fixed part and TLVs) and the standard data shape.
Only role 0x02 is matched against corr. The same position in 0x03/0x04 is fn.
Responses with an unknown role, the request role, or a corr not being waited for are dropped.
fn 0 does not send notifications (core §10.2), so events/data of fn 0 are not adopted either, and the seq of other fns does not change.
Of all u8 role values, 0 and 5 to 255 are checked as unknown. kind is an adopted example with 1 as known, and 0 and 2 to 255 are all checked.
The fixed part of an unknown kind is not interpreted and is dropped, but it is reflected in the per-fn seq shared by data/event.
With an explicit read start position from 65534, 65535→0→1 is checked.
seq 0→2→3 detects a real gap of 1, without confusing it with fn 8.

The adapter API is `run(receive, clock, deadline=..., corr=..., expected=...)`.
receive takes an absolute deadline and returns a raw frame or None. clock is logical ms.
corr=None adopts the receive pump; an integer adopts waiting for the response with that corr.
expected is the next seq of each fn; this value and the clock are given explicitly by the test side.
The returned mapping has reply (bytes/None), notifications (a list of known frames), and expected and gaps (integers per fn).
When it detects corruption, it raises ValueError or returns a positive count in broken_frames, and does not include that sample in reply/notifications.
This check covers recognizing corruption and not using it. Transport reconnection and reframing are separate checks.
This API is the shape of a check adapter and does not define a general public Host API.

The clock advances at most 5 ms per receive and is clipped at the given read deadline. The overall absolute deadline is 20 ms.
An implementation that splits one read with a short deadline is also accepted.
Even when known notifications and unknown events continue, it checks that response waiting and the pump do not extend the deadline.
The same deadline is checked when responses with an unknown role, the request role or an old corr continue, and the notification seq does not change.
It also checks short responses, the event header / known fixed part, the data fixed part, data len overruns, TLV length overruns in notifications and number 0.
data lengths 0/1/255/256, the maximum u64 position and following unknown TLVs are normal samples.
The fixed shape of an unknown event is not known, so its body is not assumed to be TLVs and checked.
An adapter that does not finish within the stimulus limit of 20 reads also fails. This limit is not an OEP capacity rule.
The sample's gap computation targets forward samples and does not check handling of duplicates, reverse order or reconnection.

```python
import json
from pathlib import Path
from oep_client.conformance.host_notifications import HostNotificationChecks
from oep_client.conformance.host_notifications_sample import SampleHostNotificationReader

report = HostNotificationChecks(
    SampleHostNotificationReader(), spec="/explicit/path/to/oep-spec"
).run()
Path("/explicit/output/host-notifications.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

Run it in the uv environment as `uv run python example.py`. The unit tests are
`uv run pytest -q tests/test_conformance_host_notifications.py`.
The unit tests inject a SPEC identity; when used with a real checkout, the commit, dirty and registry SHA are recorded.
The report saves raw stimuli, read deadlines, end times, results and checker/sample SHAs.
The user adds and saves the source/version of their own adapter.

A PASS does not mean conformance of the production Host, the real clock / OS scheduling, other event shapes, transport recovery on corruption,
recovery/resend, or Probe notification sending. full_conformance is always false.
Samples that feed unknown kinds test the Host's tolerance of future values; this is not a spec that a Probe may send unassigned kinds.
