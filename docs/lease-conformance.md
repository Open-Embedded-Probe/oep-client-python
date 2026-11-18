# Supplementary checks of response sending and lease

[日本語](lease-conformance.ja.md)

core §6.1 restarts the lease count from **when the response is generated** for a request from the holder that passed the session decision. This includes rejected, but refusals at the header, resend or another session do not renew it. The lease is not counted while the request runs. The start does not depend on the response waiting to be sent or send completion, and the same holds when the transport closes and the response is dropped.

`LeaseChecks` first runs the [resend checks under congestion](replay-pressure-conformance.md) (CORE-PIPELINE-*, CORE-PRESSURE-*) with the same adapter, then reports CORE-LEASE-INSTRUMENTATION and the following 6 (WHOLE, REJECTED, NO-RENEW, EXPIRE, FORCE, END).

An explicit logical writer controls completion of the whole response. It is not a check that proves send completion on physical USB/TCP.

- It stops and waits on the clock response, then sends only 7 bytes and waits. At response generation the deadline is moved lease_ms ahead, and later waits, partial sends and completion of the whole response do not change it.
- A malformed refusal that passed the session decision also renews at generation and does not change on send completion. A resend of a saved refusal response does not renew.
- A resend of a success, a query on session 0, a refusal at the header and locked for another session do not renew.
- Even while the result waits to be sent, once the earlier deadline passes it expires and releases resources/subscriptions. A success response sent late and its resend do not revive the ended session. A keepalive with a new corr gets no_session.
- With S's response stopped on primary, it re-checks boot/holder and forces another ID T on peer. S's response that completes later does not change T's deadline. Only T is ended on peer.
- end releases resources/subscriptions in its state processing. An earlier success response or the send completion/resend of end does not revive the session.

It checks with lease=1000 ms, and waits are 100 to 1050 ms. The adapter's independent clock confirms each wait actually advanced at least the given time. The deadline of `timing_state()` and the probe model's now_ms are taken before and after resuming sending. In renewing cases, the deadline matches "now at response generation + lease"; in non-renewing cases, it matches the earlier deadline itself. Times, all controls, raw requests/responses and state are recorded. An exact match of instantaneous clocks is not required of a real clock.

The sample adapter adds clock_ms, wait_ms and timing_state to `SamplePipelineAdapter`. Without clock_ms/wait_ms, it uses a monotonic clock and real sleep. The example and pytest pass the same virtual clock to the model and the adapter. clock and wait can be given from outside, and waiting times are not fabricated to fit the model's deadline. It uses the SPEC/shared lock of `.env.example` and adds no search for private benches or equipment.

```sh
uv run pytest -q tests/test_conformance_lease.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.lease import LeaseChecks
from oep_client.conformance.lease_sample import SampleLeaseAdapter
from oep_client.conformance.pipeline_model import PipelineModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    now = [0]
    adapter = SampleLeaseAdapter(PipelineModel(lambda: now[0]), clock_ms=lambda: now[0],
                                 wait_ms=lambda ms: now.__setitem__(0, now[0] + ms))
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = LeaseChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

The public core model treats the point where the normal synchronous API returns the whole response as the send. The explicit pipeline model uses defer_send and hands a renewal ticket per response to a single writer. No ticket is created for resends or earlier-stage refusals. A ticket is tied to the session ID and the lock generation and becomes invalid on end, expiry or force. The deadline is renewed at response generation and does not change when the ticket has been written (core §6.1). On a closed transport, unfinished tickets are discarded and the deadline stays as it is.

Time during execution is excluded from the lease, and the whole lease is counted again from when processing finishes and the response is generated. An open with a new corr in the same session changes the lease value, keeps resources/history, and renews at response generation. A resend of the same open does not renew. These long executions, transport disconnects and same-session opens are additional unit checks with a virtual clock, told apart from recorded real-clock checks.

`full_conformance=false`. Real USB/TCP send completion, concurrency, long ops on a real clock, restart, the host scheduler, silent loss recovery and the retention limits of large requests/responses are not checked. This is not a change of public wire ops or the SPEC.
