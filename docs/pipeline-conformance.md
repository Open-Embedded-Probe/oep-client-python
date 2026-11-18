# Supplementary checks of per-transport request acceptance and recovery

[日本語](pipeline-conformance.ja.md)

From SPEC core §4.4/7.1, this checks the per-transport max_frame, window, max_inflight and the order of requests/responses. window is the total OEP message bytes of unresolved requests including headers, not including the COBS/CRC/length wrapping. Each accepted TCP connection has its own independent window (transports §3). The resend cache is one for the whole probe, separate from the transport's acceptance window.

The host keeps to the declared limits. **A window_exceeded response to an over-limit request is optional**, and no response to a request over capacity is also allowed. Therefore the over-limit refusal check is not imposed on every probe. This supplementary check is only for logical models that explicitly state `reject-overflow`. Hardware wire limit checks, probes that choose silent loss, and the real host scheduler are handled separately.

The sample uses the routes interface; primary declares max_frame=64 / window=80 / max_inflight=3 in confirm, and peer 64 / 96 / 2. Sending three 10-byte requests to primary reaches only the count limit; sending 64-byte and 16-byte requests reaches only the window limit. Non-critical TLVs on clock adjust the size, and no side effects are added to the requests. Only session 0 is used on each transport, and sessions are not moved to another transport.

When a request is accepted, it reserves the transport's window, and **in this sample** holds it **until the whole response message has been written**. Completing processing alone does not release it, and a response partly sent is still unresolved. This is a conservative model policy, not a change that sets a new release point for internal buffers in the SPEC. After the send completes, the window is returned. An over-limit request is not accepted, and window_exceeded is returned in reception order. This is managed separately from the notification queue limit.

The peer's unit/boot/declarations are also matched.

- count boundary: accepts 10 bytes × 3 requests and keeps the window while the writer is stopped.
- window boundary: accepts 64+16-byte requests and counts 80 bytes including headers/TLVs.
- independence: even when primary is full and stopped, peer accepts and responds up to its own limit, and can accept again.
- count overrun: refuses the 4th, for which the byte window still has room, and accepts the next request with a new corr after recovery.
- byte overrun: refuses the 3rd, for which the count window still has room, and accepts the next request with a new corr after recovery.
- partial response: keeps the window even after 7 bytes are sent, and releases it when the whole response is sent.

Each response's transport, corr, reception order, outcome, boot and TLVs are checked in raw bytes. Rather than a self-report of remaining capacity, the accepted unresolved requests themselves returned by the instrumentation's `pending(route)` are compared with the checking side's request sequence to compute count and byte totals. However, this is a check that observes the model's instrumentation, not a direct measurement of hardware buffers.

Besides the control/trace capabilities of `SampleRouteAdapter`, the adapter provides an explicit `admission_policy='reject-overflow'` and `pending`. The service observation budget is 1 to 1000 ms (100 ms in the sample). All inputs/outputs, requests/responses and control times are recorded, and no public op is added.

```sh
uv run pytest -q tests/test_conformance_pipeline.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.pipeline import PipelineChecks
from oep_client.conformance.pipeline_sample import SamplePipelineAdapter
from oep_client.conformance.pipeline_model import PipelineModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SamplePipelineAdapter(PipelineModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = PipelineChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

The SPEC/shared lock are passed from the `.env.example` items. Private benches are not searched, and the equipment's ADDRESS/FRAMING/TCP_PEER are not reused for the model. These fixed dimensions are conditions of a check fixture that separates the boundaries, not constraints on permanent equipment or hardware declaration values.

Always `full_conformance=false`. The real host's send throttling, silent overflow loss, resend/refusal cache under pressure of session requests, concurrency, real TCP/USB and restart are not checked.
