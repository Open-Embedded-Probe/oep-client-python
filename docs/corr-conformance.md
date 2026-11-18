# Boundary checks of corr u16

[日本語](corr-conformance.ja.md)

The corr of core §4.1/§5.1 is a wrapping number that does not use 0. The host moves the number forward per session_id, and after 65535 comes 1.
If the forward distance `d = (new − last) mod 65536` is 1 to 32767, it is a new request; otherwise (including the same number and exactly half a cycle), it is result_lost if not in the records.
Records half a cycle or more in the past are dropped and not used for matching. Numbers on SID 0 do not affect the session's history. Only a successful open of a new session with a different ID replaces the resend state.

`CorrChecks` first runs the [resend cache retention limit checks](retention-conformance.md) (CORE-RETENTION-*) with the same adapter, then reports the following CORE-CORR-*.
These are supplementary checks added to the normal core checks; they do not require a 16-byte retention size or the sample resource op of a general probe.

- ASCENDING: advances to 32767/32768/32769/65534 in steps of less than half a cycle; each new request creates a resource only once, and an identical resend returns the saved result.
- HALF-GAPS: from open's corr 1, a new request at distance 32767 is accepted; distances 32768/32769 are result_lost, not run, and change neither resources nor history.
- WRAP: after 65535 comes 1, and the session's resources and history are kept. A number half a cycle or more behind 1 is result_lost and not run.
- ZERO: corr 0 is malformed, including unknown fn, another session and SID 0. The state does not change.
- FREE: completes 65535/1/32768/65535 on SID 0 one at a time and does not change S's history or lease.
- WRAP-END: end after a full cycle releases the resources, and after the end, end and the request just before can still be resent with saved results. open 1 of another session replaces the history, and the next request is built from 2.

It uses `SampleRetentionAdapter` explicitly and observes raw exchanges, cache/high/last/deadline, resources and the allocator. It waits 5 ms on an independent clock before a resend and also detects a wrong lease renewal.

```sh
uv run pytest -q tests/test_conformance_corr.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.corr import CorrChecks
from oep_client.conformance.retention_sample import SampleRetentionAdapter
from oep_client.conformance.retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRetentionAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = CorrChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

Set the SPEC and the shared lock as in `.env.example`. The public side does not search for private benches.

The checker's automatic numbering in `Checks.request()` goes from 65535 to 1 and does not use 0. When packing fails on an invalid header, no number is consumed. An explicit corr is also used for abnormal-case checks including 0.

`full_conformance=false`. The target is number boundaries and saved state on a logical model; physical USB/TCP, concurrency, recovery from result_lost and other retention sizes are not checked.
