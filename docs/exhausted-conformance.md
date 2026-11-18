# Abandoning several pendings when corr is exhausted, and recovery on a new transport

[日本語](exhausted-conformance.ja.md)

The 3 conditions none/prefix/queued are each checked with a new explicit model. This is a supplementary check of a sample host based on core §4.1/4.4/5.1/6; it is not an implementation of the production Host or of physical reconnection.

Create at corr 65533, create with an invalid kind at 65534 and end at 65535 are sent within the acceptance window. none loses all responses that were processed, prefix receives only the first success, and queued keeps them accepted but not run. After waiting 1050 ms for the first unresolved request, the whole transport is abandoned. It does not claim that a timeout occurred for each later request; abandoning the transport makes the pendings unknown.

- Received successes and their raw responses are kept. All unresolved requests become unknown, and repeated abandonment or late responses from the old reader do not change the results.
- The old session ID and transport are retired, and the host-side bindings to old resources are invalidated. The old transport is closed, and the unexecuted queue and the credit/output waiting to be sent are discarded.
- `SampleExhaustedHost`, on another logical transport, checks an explicit barrier, the unit/boot on SID 0, lock_state and the sample inventory. It does not send the old S to the new transport. If the boot changes during the reads, it stops.
- If the lock remains, it stops with lock-held. It does not guess the owner, and does not use force or re-run the old end. Only the queued fixture confirms expiry of the known 3000 ms lease with an extra wait, then retries recovery.
- Only when the release can be confirmed within the same boot does it choose an unused cryptographically random ID and send open 1 on the new transport. It resumes use after checking the lease/boot/TLVs and their correspondence to the response.
- If the new open's response is lost, the new transport is also isolated and becomes unknown. There is no automatic resend, force, or re-open with another ID.

Even when the release is observed, it does not guess whether the old end ran or the release came from expiry. The unknown of the original requests is not rewritten into success/refusal, and old resources are not recreated automatically. The resource allocator of the same boot carries over, and the new session's corr/history start from 1.

The fixture expresses connection close and another transport with a logical API. It is no guarantee of an implementation of physical reader stop, USB resync or TCP reconnection. Reads on a transport are not an atomic snapshot, and are limited to the condition with no concurrent writer. The new open does not use force, so if another owner takes it first, it does not take it back.

The unit tests detect the defects of ignoring the barrier, reusing the old ID, rewriting unknown, erasing received results, keeping old bindings and using force. They also check lack of input/control capability, another owner's lock, boot change, boot change during the reads, and loss of the new open's response. The checker matches the host's request log against the adapter's actual sent bytes.

```sh
uv run pytest -q tests/test_conformance_exhausted.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.exhausted import ExhaustedChecks
from oep_client.conformance.recovery_race_sample import SampleRecoveryRaceAdapter
from oep_client.conformance.retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    for mode in ('none', 'prefix', 'queued'):
        adapter = SampleRecoveryRaceAdapter(RetentionModel(), batch_mode=mode)
        checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
        report = ExhaustedChecks(checks, adapter).run()
        assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. It does not depend on or search for private benches. Unlike [end/open loss recovery on the same connection](session-loss-conformance.md), this check retires the old transport and so does not resend old requests.

`full_conformance=false`. Production Host integration, physical reader stop/reconnection, concurrent writers, external side effects of arbitrary extensions, and other batch sizes / retention conditions are not checked.
