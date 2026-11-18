# Checks of notification seq wrap, resubscription and resend

[日本語](notification-sequence-conformance.ja.md)

`NotificationSequenceChecks` is a second-stage supplementary check based on core §5.1/10.2/10.3/10.4 and transports §3.
It uses the public `RouteModel` and `SampleNotificationSequenceAdapter`, and the selected SPEC checkout is given explicitly.
It does not search for private benches or neighboring checkouts.


| Behavior | Observation |
|---|---|
| WRAP | Emits data/events alternately from seq 65534: 65535→0→1. Shared within the same fn; another fn is independent from 0 |
| DROP-WRAP | Checks the max_frame×2 limit and drops with 32 frames while sending is stopped. Including around the wrap, dropped frames also consume numbers |
| REPLACE | Replacement of seq/destination. Does not change another fn's seq/destination, and does not send one notification to both transports |
| DISCARD | With a frame that has started sending (1 byte or more) and 2 unsent frames left, it resubscribes on the same transport, resubscribes on another transport, and unsubscribes. The old subscription's unsent frames are dropped, and a frame that has started is completed before the response on the same transport is returned. Another fn's unsent frames and responses on other transports are not affected |
| UNSUBSCRIBE | Unsubscribed / unsubscribe / unsubscribe again succeed, and another fn's subscription/seq/conditions/transport and existing resources are kept. No new notifications are sent after unsubscribe, and resubscription gives seq0 |
| REPLAY | A resend of an old successful subscribe returns the same response. It does not roll back the new subscription's seq/destination/conditions |
| REPLAY-REFUSED | A resend of an old refused subscribe also returns the saved response and does not touch the new subscription |
| REJECTED | Before refusing malformed / unknown critical TLVs, the subscription's seq/destination/conditions/resources are not changed |

The sample's `RouteModel` and `NotificationModel` drop that fn's unsent frames on resubscription/unsubscribe, and keep and complete a frame that has started sending.
The fixture states fn 1/2, primary/peer and max_frame 64 explicitly. The seq seed and logical reset are instrumentation for the check,
not public hardware ops. The seed is possible only when the fn is subscribed and the notification queue is empty.
It does not prescribe a particular way of choosing which notifications to keep; it matches the content of the remaining frames, the generation order including the wrap, and the next seq.

The destination replacement check sends raw requests in a fixed order with the same SID to primary/peer.
This is a stimulus to observe the probe's "process as the same request regardless of transport" and "do not change the destination on a resend".
transports §3 requires the Host to use one session on one transport, so this stimulus is not reused as a normal Host usage example.
Misjudgments under competing transport delays are not treated as conformance, and the Host's one-transport constraint itself is not verified by this check.

Run it at the root of the client checkout.

```sh
uv run pytest -q tests/test_conformance_notification_sequence.py tests/test_conformance_routes.py tests/test_conformance_data.py tests/test_conformance_subscriptions.py
```

The pytest has the normal model, violation injection (including DISCARD and unsubscribe), invalid preconditions and so on.
Response priority, partial sends, queue amounts and transport disconnection are covered by running RouteChecks separately.
An example that saves the SPEC identity, sent/received raw, stimuli/observations and checker/model SHAs:

```sh
uv run python - /path/to/oep-spec /path/to/notification-report.json <<'PY'
import json, sys
from pathlib import Path
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance.routes import RouteChecks
from oep_client.conformance.route_sample import SampleRouteAdapter
from oep_client.conformance.notification_sequence import NotificationSequenceChecks
from oep_client.conformance.notification_sequence_sample import SampleNotificationSequenceAdapter
from oep_client.conformance.route_model import RouteModel
registry, identity = spec_identity(sys.argv[1])
model = RouteModel()
routes = RouteChecks(Checks(model.handle, registry, 'virtual-routes-1'), SampleRouteAdapter(model)).run()
model = RouteModel()
checks = Checks(model.handle, registry, 'virtual-routes-1')
report = NotificationSequenceChecks(checks, SampleNotificationSequenceAdapter(model)).run()
report['sequence_status'] = report['status']
report['status'] = 'passed' if report['sequence_status'] == routes['status'] == 'passed' else 'failed'
report.update(spec=identity, route_checks=routes, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256())
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

The payloads of all OEP-specific notifications, real OS/framing/writer buffers, concurrent producers, Host behavior,
capacity of all transports and conformance to all core/common norms remain not checked.
