# force during recovery and per-transport unknown results

[日本語](recovery-races-conformance.ja.md)

core §5.1 makes all unresolved requests on a transport whose resends were given up unknown. It does not drop final results already received or give up requests on other transports. Until the boot/session checks during recovery finish, the list of old resources is not used for the next operation.

The batch conditions `none`, `prefix` and `queued` each run on a new explicit model. One result is not treated as checking all conditions. The normal core checks are also run separately.

## Session replacement during reads

At points explicitly armed by `SampleRecoveryRaceAdapter`, only the fixture checks its own S's boot/holder from peer and forces to another ID T. T creates resources on another fn. S keeps sending from primary and is not moved to peer.

- Just before the first S clock: gets locked and does not adopt the list.
- After receiving the list response of the first fn: even if S's list and T's list are mixed, the last S clock detects locked and the whole is discarded.
- Just before the last S clock: even a replacement after the lists were read discards the lists.

The wire-only recovery host returns session-unavailable/unknown and returns no candidate resources. T's resources, allocator, cache/high/last, lease and subscriptions match exactly the snapshot right after the fixture created T. The recovery host's raw requests are only checks and reads; force/create/end are recorded separately as the fixture's peer exchanges. This is not a check that adds guarantees for arbitrary concurrent writers to the normal single-writer condition.

## Abandoning several unresolved requests

Three requests are sent to primary within the acceptance window: a successful create, a create with an invalid kind, and a successful create. On peer, another transport, a SID 0 clock with the same numeric corr as on primary is placed unresolved. The ban on duplicate corr applies to unresolved requests on the same transport; even for a different session, it applies on the same transport.

| Condition | Fixture-side processing | Responses the host receives | After giving up primary |
| --- | --- | --- | --- |
| `none` | Processes the 3 and records all responses | none | all 3 unknown |
| `prefix` | Processes the 3 and records all responses | only the first | keeps the first completed; the remaining 2 unknown |
| `queued` | Accepts the 3 but does not run them | none | all 3 unknown |

The sample's operation wait and logical transfer time are 0 ms. After securing 1050 ms on an independent clock as the wait for the first unresolved request, the transport is given up. It is not treated as each later request timing out one by one. By the rule of giving up a transport, the remaining unresolved requests also become unknown.

From the host, executed successes/refusals and unexecuted requests are not told apart. The fixture's raw responses and allocator confirm the actual processing, but that information is not passed to the host. The source primary is closed, and the unexecuted queue, pending sends and acceptance credit are discarded. Peer's acceptance credit and the host's pending are kept, and peer's response is received as normal.

Even if peer's SID 0 inventory confirms the current state, the outcomes of the individual unknown operations are not restored. S is not moved to peer to resend state-changing operations. The final force/end to T is an explicit fixture cleanup to release its own S, not an automatic action of the recovery host.

## Records of the sample host

`SamplePendingHost` does not access the model/transport; it records request bytes, transport, boot, session, corr and received responses.

- It keeps per-route max_frame/window/max_inflight, and refuses duplicates of unresolved corr on the same transport.
- abandon makes all pendings of that transport unknown. Received completed/rejected and pendings of other transports are kept. Repeating it does not change the results.
- Even when a result_lost response is actually received, the original operation is not asserted as rejected. The status is unknown, and the observed raw result_lost response is kept.
- It advances a local reader generation and does not adopt late responses from the old reader. This is not a spec that adds a new number to the wire.
- After abandonment, it accepts no new requests. An explicit resume needs a recovered transport and a confirmed boot. Responses from the old reader are not adopted even if the corr is the same in the new generation. IDs of abandoned unresolved sessions are not reused.

This ledger is the recording of messages taken out by the decoder; it does not implement checks of physical framing or operation-specific payloads. The checker separately checks the batch's response headers, the create resource IDs/order and the boot/payload of peer clock. Switching the local generation alone does not complete a TCP reconnection or USB resync.

```sh
uv run pytest -q tests/test_conformance_recovery_races.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.recovery_races import RecoveryRaceChecks
from oep_client.conformance.recovery_race_sample import SampleRecoveryRaceAdapter
from oep_client.conformance.retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    for mode in ('none', 'prefix', 'queued'):
        adapter = SampleRecoveryRaceAdapter(RetentionModel(), batch_mode=mode)
        checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
        report = RecoveryRaceChecks(checks, adapter).run()
        assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. The public side does not search for private benches. All raw exchanges, force points, the host ledger, state comparisons and the independent wait window are saved.

`full_conformance=false`. Physical timeout/framing/reconnect and reader stop, production Host integration, arbitrary concurrent writers / atomic snapshots, other batch sizes and retention limits, and recovery semantics of arbitrary extensions are not checked.
