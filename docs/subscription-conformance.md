# Lifetime checks of subscriptions and events

[日本語](subscription-conformance.ja.md)

From SPEC core §6/9/10, the contracts of subscriptions and events are made common checks. The common checks handle the encoding and pass/fail of the subscription ops. An explicit adapter handles how to generate concrete events and validate their payloads. This is kept separate from events specific to standard OEP interfaces, batched data delivery, and physical USB/TCP verification.

The first condition of application is that an identifiable event can be generated from outside, once each, for two or more notifying fns. The adapter provides the fn list, stimuli, validation of event payloads, and reception of notifications within a given observation time. It does not change the request/response transport. There is no automatic search, dependence on private benches or automatic choice of a hardware adapter.

The common checks cover no subscription, session required, refusal of subscription operations from another session, separation between fns, subscription replacement and seq reset, seq kept on subscribe resend, idempotence and resend of unsubscribe, subscription kept on open with the same ID and on open resend, stop on end / natural expiry / force, an invalid subscribe not breaking the previous subscription, and data thresholds not applying to events. The role/fn/seq/length of notifications and the payload are validated separately, and the raw notifications and the times of stimuli/observations are saved.

The following scope is not included in a PASS at this stage: the data/event shared seq, seq wrap, data min_bytes/max_delay, response priority, notification transport, route disconnect, the send queue limit of max_frame×2, seq gaps from drops, and asynchronous processing on hardware. A PASS with no notification is a result for the given observation window, not an indefinite guarantee.

## Contract of the sample extension

The name is `io.github.open-embedded-probe.notify`, revision 1, fn 1/2, instance 0/1. It is not an OEP standard interface. It adds the required op 1 subscribe / 2 unsubscribe to ops 16 to 19, the same as the resource sample, with 8 resources in total across all fns. The subscribe fixed part is min_bytes:u16, max_delay_ms:u32, and the success response is empty. The unsubscribe fixed part is empty, and the success response is empty. Both require a session. Following TLVs in requests follow the core's rules. Unknown critical TLVs and short fixed parts are refused without side effects.

The fixed part of event kind 1 is marker:u32, followed by TLVs. The marker is the stimulus's identifier, not a target/pin number. On an external stimulus, only subscribed fns emit role 3. seq starts from 0 at each subscribe and increases within the same fn. Events do not wait for the data thresholds. A stimulus without a subscription emits no notification. Data notifications are not implemented. It has no specific status/reject, describe tags, pin roles, external specs or chip-specific procedures. Resources and subscriptions follow the core's session lifetime and do not cross boots.

## Adapter and running

`SubscriptionChecks(checks, adapter, wait=time.sleep).run()` returns the identity check (IF-SUB-IDENTITY), the behavior contracts (IF-SUB-*) and the declaration checks of the target fns. It first matches the unit, core/IF declarations and adapter capabilities, and on failure stops subscription operations and stimuli. After an IO failure of transport/stimulus/observation, the later items are BLOCKED. Rule violations are FAIL, and incomplete explicit settings are not skipped. `full_conformance` is always false.

The adapter's capabilities are as follows.

- `functions`: 2 to 8 explicit notifying fns. This is a condition of the bounded check equipment, not a limit on fn count in the OEP norms.
- `observation_ms`: 1 to 100 ms. The observation window of the equipment within which events arrive in this check set. If the equipment cannot meet this condition, make another window/stimulus plan, and do not use this check as conformance evidence.
- `stimulate(fn, marker)`: generates one event on the given fn. The marker is a u32 numbered by the checking side. It is an external stimulus, not an OEP request, and does not change the session's transport or resend history.
- `receive(timeout_ms)`: observes for the given time and returns all notification frames received in that window as a list/tuple of bytes. The host implementation must not drop unknown/broken/duplicate notifications.
- `decode_event(frame)`: validates the interface-specific kind / fixed part / following TLVs and returns the marker. The common checks handle the common header's role/fn/seq/length and the pass/fail of comparison. This adapter stimulates only events, so receiving data is also an expectation violation.

`oep_client.conformance.subscription_sample.SampleSubscriptionAdapter` decodes the sample contract above independently. It does not import the probe implementation. `oep_client.conformance.notification_model.NotificationModel` is an in-process model with events and resources. `.emit()` and `.drain()` are entry points for external stimulus/observation and do not simulate the response priority or send buffers of a physical transport. Use it separately from core-v1, the resource-only sample and profiles with targets.

```sh
uv run pytest -q tests/test_conformance_subscriptions.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.subscriptions import SubscriptionChecks
from oep_client.conformance.subscription_sample import SampleSubscriptionAdapter
from oep_client.conformance.notification_model import NotificationModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = NotificationModel()
    checks = Checks(model.handle, registry, 'virtual-notify-1')
    adapter = SampleSubscriptionAdapter(model, functions=(1, 2), observation_ms=50)
    report = SubscriptionChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

The same API is also used in the Arduino uv/pytest CLI plugin environment. Explicit paths are passed for the SPEC/shared lock. Use the SPEC/lock items of `.env.example`. ADDRESS/FRAMING/TCP_PEER are not used for this model. For JSON, reserve a new path with `open('x')` and save the SPEC/checker/model source hashes, times, raw requests/responses and raw notifications. The public tests do not refer to private benches.

pytest also runs unit checks with `wait` replaced by a virtual clock. They are told apart from real-clock records. The model's own restart checks verify that the core/resource/subscription history does not cross boots, but the real-clock checks of the public supplementary report do not include restart. The core and resource checks are also applied to the same notification model to check for regressions.
