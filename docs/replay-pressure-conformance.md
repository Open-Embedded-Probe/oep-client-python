# Supplementary checks of session resend and refusal cache under congestion

[日本語](replay-pressure-conformance.ja.md)

This checks core §4.3/5.1/6.1 on top of the [per-transport acceptance model](pipeline-conformance.md). The acceptance window is per connection, and the resend history of the last successful new session is one for the probe. The refusal order is header, resend, session, then other state decisions, and window_exceeded belongs to the last stage. When a request of a new session passes the resend decision, a rejected is also recorded. Mere congestion of acceptance does not skip this order.

Dropping an over-limit request without responding is also allowed by the SPEC. This check is for an explicit sample that chooses `reject-overflow` and does not make over-limit refusal required of general probes. Out-of-range stimuli are also sent on purpose to check the fixture's refusal policy. This is not a change that lets a normal host exceed the limits.

`ReplayPressureChecks` first runs the [per-transport acceptance checks](pipeline-conformance.md) (CORE-PIPELINE-*) with the same adapter, then reports CORE-PRESSURE-INSTRUMENTATION and the following 5 (INFLIGHT, REJECTED, PRECEDENCE, PEER-CACHE, TAKEOVER).

The sample create(kind u8) of fn 1 actually creates resources, and the returned non-zero u16 IDs, raw responses and the observed resource list are compared.

- 3 creates fill the acceptance window, and while the responses are stopped, the first request is resent as is. It returns the same response, and there are still 3 resources. A resend after sending resumes also does not change the lease/cache/resource state.
- 3 requests fill the window, and a create with a new corr is refused. Even after the window recovers, the same request gets the same window_exceeded and is not run. A changed request with the same corr is malformed; with a new corr, the create succeeds exactly once.
- After filling the acceptance window with pending responses, it sends a saved request, a changed request with the same corr, unknown fn/op, corr 0, another session, and a resource request on session 0. They are answered by the decisions that come before congestion, and the saved result is returned. Refusals at the earlier stages do not change the lease/history/resources.
- From peer, it sends clock 12 times on session 0 with the same corr as S's saved request. S's history is not consumed, replaced or updated. S's resends happen only on primary.
- On peer, it re-checks boot/holder and forces a new open with another ID T. S's resources and history go away, and the shared cache starts from T's open. S's old requests get locked on the original primary, and S's saved success is not returned. Only T is ended on peer.

All session S requests are fixed to primary. There is no configuration that moves S to peer to try the shared cache. S of each case is chosen at random, and T is also random and different from S. force is done only to an S it holds itself. Applying this to a hardware adapter needs a separate equipment plan.

The instrumentation returns holder, last, high, deadline, cache and resources explicitly from the route adapter's `state()`. It compares all request/response bytes and the deadline itself, and does not substitute a mere "resend count" or a success counter. Before session operations, it checks that the sample's resource op is declared and that the observation capabilities and fields exist.

The public core model takes an optional `admission_reason` and applies the refusal only after the header/resend/session decisions. The normal core is not given a reason. Refusals of the pipeline model also go through the same core history management. This is not an addition of public wire ops or a SPEC change.

```sh
uv run pytest -q tests/test_conformance_replay_pressure.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.replay_pressure import ReplayPressureChecks
from oep_client.conformance.pipeline_sample import SamplePipelineAdapter
from oep_client.conformance.pipeline_model import PipelineModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SamplePipelineAdapter(PipelineModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = ReplayPressureChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. The public tests do not search for private benches. Raw exchanges, all controls, times, declarations and SPEC/checker/model hashes are saved to a new artifact. Always `full_conformance=false`.

Real TCP/USB, recovery from silent loss, concurrency, restart, the host scheduler and the retention limits of large requests/responses are not checked. Processing based on completion of the whole response is in the [lease checks with a logical writer](lease-conformance.md). There is no check that starts counting from physical send completion.
