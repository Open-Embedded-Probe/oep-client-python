# Supplementary checks of re-reading the state after result_lost

[日本語](recovery-conformance.ja.md)

core §5.1 states that a state-changing request that may or may not have run is not re-run automatically with a new corr, and that after giving up on resending, boot_id and the state are read again. result_lost proves none of success, refusal or non-execution of the original request. The re-read state is not the original result itself either.

It observes the probe's retention contract and the sample host's recovery policy separately. It does not impose the sample resource operations, the way of reading the state or the single-writer condition on general extensions. It is not a change that builds recovery into the production Host.

`SampleRecoveryAdapter.discard_reply()` completes the logical exchange, records the whole raw response in the evidence, and does not return it to the host. It does no hardware transfer or restart and causes no physical timeout. The sample op sets no operation wait, so 0 ms, and the logical transfer time is also 0 ms. Before a resend it waits 1050 ms on an independent clock and checks that this is not below the core's host_wait_add_ms=1000. On hardware, add the operation wait and transfer time, and check transport resync/reconnection separately.

`SampleRecoveryHost` does not access the model's state/cache/allocator; it uses only the public wire's confirm/identity, S's clock and the sample inventory (op 19, SID 0). After a resend of the same request gets result_lost, it checks the following.

1. Check the unit and boot_id. If the boot changed, the mapping to old resources is discarded, and they are not tied even if the numbers are the same.
2. Read S's clock with a new corr on the same transport to check that the current S is valid. If no_session/locked, the mapping to the old session's resources is not returned. "locked" from lock_state alone is not taken as its own session.
3. Read the resource list of each sample fn, and validate resource number 0, duplicates, type and length.
4. Check boot/session once more. On a restart or expiry during the reads, the list is discarded. The lists of several fns are not an atomic snapshot.
5. Return the obtained state. The original operation's outcome always stays unknown.

An independent checker checks from the raw request list that no create/close/open/end/force or the original state-changing request was sent during recovery. Only the checker uses the model-side state, as an oracle, and matches it with the read results and checks that resources/allocator did not change. S's clock with a new corr is a normal new request, so it renews history and lease. The result_lost resend itself does not renew.

- A success response lost by a missing request, a missing response or history eviction: the identical resend is result_lost. On a fixture with an explicit single writer, one more resource in the same slot is observed-added. This is not treated as restoring the original success response.
- A lost refusal response, and an old number not sent that becomes result_lost: unchanged-state. It is not asserted as failed or not run.
- A lost success response of close: observed-absent. A lost response of a close refused for another type: still-present. In neither case is close redone automatically.
- Two more in the same slot, or no single-writer guarantee: ambiguous-state. No candidate resource is chosen.
- Expiry: session-unavailable. No list or candidates are returned.
- Explicit logical restart: boot-changed. No list or candidates are returned. The old open is not resent.

The single-writer condition is a premise of this fixture and does not guarantee attribution of resource increases in general. The current absence/presence of the close target is not the operation's outcome either. Extensions whose external side effects do not show in the state need a separate matching procedure in the extension spec.

```sh
uv run pytest -q tests/test_conformance_recovery.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.recovery import RecoveryChecks
from oep_client.conformance.recovery_sample import SampleRecoveryAdapter
from oep_client.conformance.retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRecoveryAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = RecoveryChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

It uses the SPEC/shared lock of `.env.example` and does not search for private benches. The evidence also saves the lost original raw responses, but their content is not passed to the recovery host.

`full_conformance=false`. Physical loss/timeout/reconnection, production Host integration, concurrent writers and atomic state reads, force during re-reading, recovery semantics of arbitrary extensions and other retention sizes are not checked.
