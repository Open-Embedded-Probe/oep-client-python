# Checks of notification transports, response priority and partly sent queues

[日本語](route-conformance.ja.md)

This checks the contracts of SPEC core §10.4 and transports §3 with a model that has two explicit transports and instrumentation. It controls received requests, notification generation and send stops/partial sends, and observes all raw messages and the unsent suffix of each transport. It does not search for equipment automatically or depend on private benches.

The common contracts are: responses return to the requester, notifications reach only the subscriber, responses to received requests are sent before notifications, the remaining bytes of partly sent notifications total at most max_frame×2, other transports keep processing while sending is stopped, notifications dropped inside the probe also consume the data/event shared seq, and a transport disconnect alone does not release the session/resources/subscriptions/resend history.

The minimal sample is `io.github.open-embedded-probe.routes` revision 1, fn 1/2. Resources/subscriptions/data/events are the same as the stream sample. It has two logical connections, primary/peer. Session S's requests go only on primary, and peer is used for session 0 queries. After primary is disconnected, peer forces another session T, and only T is ended on peer. S is not resent on the new transport.

The model has one writer per transport. A partly sent message is completed before the next message, and responses to the remaining received requests are chosen before new notifications. A notification consumes seq when generated, and notifications on a closed transport or that do not fit the queue are dropped. The queue limit is checked in unsent OEP message bytes. Transport framing overhead such as COBS/CRC/length, real OS/USB/TCP buffers and asynchronous races are not included at this stage.

The queue remainder and the retention of subscriptions and resend history are observed through instrumentation. This is not a spec that adds public hardware ops. This check is not applied as is to hardware without the observation capability. Evidence on the hardware wire, per-transport window/max_inflight and real TCP reconnection remain separate checks. The report is always partial conformance.

## Adapter and evidence

`RouteChecks(checks, adapter).run()` returns primary's identity/declarations and the behavior contracts. peer's unit/boot/declarations are also checked within identity. The adapter provides explicit distinct transports primary/peer, functions, exchange, hold(route, byte_budget), submit([(route, request)]), service, receive(route), queue(route), stimulate, feed, close, state and create_resource. `trace` stores the arguments, results, raw bytes, times and errors of each control.

`queue` returns the waiting notifications and the unsent suffix of a partly sent notification as bytes, and the common checks sum them. `state` observes the holder, resources, subscription seq/route and the kept requests/responses. These need instrumentation, and values guessed from normal public hardware ops are not substituted. `feed` returns the position of external input. `create_resource` encodes and verifies the resource op of the explicit interface.

`oep_client.conformance.route_model.RouteModel` uses the stream model's generation processing and adds a notification queue and a single writer. The logical primary/peer are treated as separate connections to the same TCP listener (with no socket/framing). `oep_client.conformance.route_sample.SampleRouteAdapter` does not import the model and calls the observation target passed explicitly.

The example is used from the public Python environment or the Arduino uv environment, with the SPEC/shared lock given explicitly.

```sh
uv run pytest -q tests/test_conformance_routes.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.routes import RouteChecks
from oep_client.conformance.route_sample import SampleRouteAdapter
from oep_client.conformance.route_model import RouteModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = RouteModel()
    adapter = SampleRouteAdapter(model)
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = RouteChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

The SPEC/checker/model hashes, raw exchanges and the adapter trace are saved to a new JSON. It uses the SPEC/shared lock of `.env.example`, and ADDRESS/FRAMING/TCP_PEER are not reused for the model. The public tests do not refer to private benches.

For a partial send, a max_frame notification is advanced by 7 bytes, and the remaining suffix is matched independently. Adding the next max_frame notification makes the remainder max_frame×2−7. A 10-byte event does not fit, consumes seq and is dropped. The notification already started is completed, and the output order is the clock response, then the next notification. The drop check generates 32 data/events alternately and checks the content/position/seq of the kept notifications and the following seq 32.

The disconnect check runs last. S is not cleaned up on the closed primary; on peer, its own S is forced with another ID T, and T is ended. Even if takeover fails, S is not resent on another transport. A hardware adapter needs a separate plan that includes re-checking unit/boot/owner and expiry by lease.

`service_budget_ms` (100 ms in the sample) is the equipment's observation budget for deciding that service returns while sending is stopped. 1 to 1000 ms is given explicitly, and exceeding it FAILs this run. It is not a norm that changes OEP's max_op_ms. Just before takeover, it re-checks that peer's boot and the instrumentation's holder are still its own S, and does not force on a mismatch.
