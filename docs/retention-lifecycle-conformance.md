# Supplementary checks of missing retention, history eviction and session/boot lifetime

[日本語](retention-lifecycle-conformance.ja.md)

This checks core §5.1/6/9 with the explicit 16-byte / 8-entry [retention sample](retention-conformance.md). It creates a missing request, a missing response and an evicted record each, and separates the handling on expiry and on force with a different ID. ID continuation within the same boot as end, and invalidation on a boot change, are separate contracts.

`RetentionLifecycleChecks` runs the [retention checks](retention-conformance.md) (CORE-RETENTION-*) after CORE-LIFECYCLE-CONTROL, then reports CORE-LIFECYCLE-EXPIRY-* / FORCE-* (REQUEST, RESPONSE, EVICTED) and CORE-LIFECYCLE-REBOOT.

The reset capability is checked before session operations, and only `reset_scope='logical-model'` is allowed. Hardware restart/transfer is not passed to this adapter.

- expiry × 3 states: creates a resource and a subscription with a 1000 ms lease and waits 1050 ms. The resource/subscription go away, but cache/high/last/boot/next resource number do not change. An identical resend is result_lost; with a missing response where the request is kept, a changed request with the same corr is malformed. A keepalive with a new corr is no_session.
- open with another ID after expiry: history starts from the new open. Resources are not handed to the new session, and resource numbers use the next number of the same boot.
- force × 3 states: re-checks its own S's boot/holder on peer and forces another ID T. S's resources/subscriptions/history are dropped, and cache/high start from T's open. S's old requests get locked on the original primary and do not change T's state. T's resource numbers are the next numbers of the same boot. Only T is ended on peer.
- logical restart: after placing a missing request, a missing response, a saved success, a response waiting to be sent and an accepted unexecuted request, the sample is rebuilt with an explicit different u32 boot_id. All history, sessions, resources, subscriptions, send windows / pending results are cleared, and uptime and resource numbers restart. confirm/identity on both transports confirm the new boot, and old S requests get no_session. The old open is not resent; a new session with another ID is opened, and resources are created from number 1.

In the lifetime contracts, S's requests use primary, and peer is used for session 0 queries and the other ID T. In the inherited retention checks of same-SID open/keepalive, the same owned SID is also sent from the peer transport to check that the subscription destination is kept. Ended IDs are not reused. A resend of the old open after restart could create a session anew, so it is done neither in this check nor in the recovery steps.

`SampleRetentionLifecycleAdapter` adds only a logical restart to the adapter. The model's boot_id is observed through state, and success is not judged from restart's reply alone. confirm/clock also check the real boot. An independent monotonic clock measures waits, and state/cache/allocator, all raw exchanges, control times and restart inputs/outputs are recorded.

`RetentionModel.restart()` accepts only a different u32 boot_id and refuses the same boot, out-of-range values and bool. It is a control to change the sample's boot reliably, not a simulated verification of a real boot random source. It does not add a restart entry point to the core / normal profile or physical equipment.

```sh
uv run pytest -q tests/test_conformance_retention_lifecycle.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.retention_lifecycle import RetentionLifecycleChecks
from oep_client.conformance.retention_lifecycle_sample import SampleRetentionLifecycleAdapter
from oep_client.conformance.retention_model import RetentionModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRetentionLifecycleAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = RetentionLifecycleChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. Private benches, equipment and ports are not searched. On equipment without the reset capability, or with physical scope, it FAILs without starting operations. No hardware reset settings are added to `.env`.

Always `full_conformance=false`. Physical restart / connection recovery, concurrency, the host's result_lost recovery policy, other retention limits and the boot_id random source are not checked. boot_id=0 is also valid, and the restart of uptime is also checked by an additional virtual clock unit test.
