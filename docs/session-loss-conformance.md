# Checking recovery from lost results of end/open

[日本語](session-loss-conformance.ja.md)

Based on core §4.1/5.1/6/9, for end (the next corr at that point) and open 1 of a new session, the saved request is resent to the same logical transport with the same bytes/corr. This is not a change that builds recovery into the production Host.

`SampleSessionLossHost` decides from the wire only. It is an explicit sample with 16 bytes of request retention / 8 history entries, and the only unresolved request on the transport is the one in question. With the connection kept, the request or all responses are lost, and it waits 1050 ms against an operation wait of 0 ms and a logical transfer of 0 ms. After a reader barrier confirms that unreceived messages/credit are empty, it reads the unit/boot and resends the original request. It does not resend if the barrier fails or the boot changed. It does not move transports, and does not re-run state-changing operations with force or a new corr.

| Condition | Decision |
| --- | --- |
| end response lost | The resend confirms the saved success, and the release is also confirmed with lock_state on SID 0 and the sample inventory. The number is not advanced past end's corr. |
| response to a 17-byte end request lost | The resend is result_lost. Even if the release is observed, the original outcome is unknown. |
| end request does not arrive | The resend of the same request is the first execution. It releases the resources and completes with the original corr. |
| open response lost | It confirms the saved success's lease/boot/TLVs, and reads S's clock with a new corr to confirm current ownership. The resend alone does not renew the lease. |
| response to an 18-byte open request lost | The resend is result_lost. If S clock succeeds, current validity can be confirmed, but the original outcome and the actual lease value remain unknown. |
| open request does not arrive | The same open 1 runs for the first time, and the response and the current S are confirmed. |
| lease expiry after open | Even if the saved open success can be fetched again, use does not resume if S clock is no_session. |
| boot change / reader barrier fails | It stops without sending the resend or the next open. |

open is confirmed in the order clock/boot check/clock. A boot change during the re-read discards the observation. Even if lock_state is held, it is not taken as S's ownership. The reads are not an atomic snapshot, and are limited to the fixture condition with no concurrent writer. Old resources are not re-bound, and the operation result and the current state are recorded separately.

The unit tests detect the defects of resending with a new corr, inferring success from state, fabricating an unknown lease, ignoring expiry, ignoring a boot change and ignoring the barrier. They also check lack of input/control capability, a boot change during the re-read, and a resend of the old open after being forced to another session. The fixture cleanup's end is done separately after the decision returned by the recovery host.

```sh
uv run pytest -q tests/test_conformance_session_loss.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.session_loss import SessionLossChecks
from oep_client.conformance.session_loss_sample import SampleSessionLossAdapter
from oep_client.conformance.retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleSessionLossAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = SessionLossChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. No dependence on or automatic search of private benches is needed.

`full_conformance=false`. Physical reader stop/resync, TCP reconnection, recovery after abandoning several pendings, production Host integration, and arbitrary concurrent writers or extension side effects are not checked. There is no guarantee that the previous S can be moved to a new connection after a TCP disconnect.
