# Deadline start points of the Host's receive loop

[日本語](host-deadline-conformance.ja.md)

Name the SPEC checkout explicitly and run `HostDeadlineChecks(SampleHostDeadlineAdapter(), spec=path).run()`.
It matches the reviewed SHAs of the body texts of core §4.4/7.5 and transports §3/6 and the wait values, and does not depend on OEP-specific config specs.
It does not search for private equipment. SerialLink._send/_exchange_once/_recv_for and LengthFrames.recv are run through
a ScheduledStream that returns scheduled byte strings and a virtual clock. No OS ports or real-clock sleep are used.

fn1/op16 with the argument wait_ms(u32) and an empty success payload is used as this fixture's extension.
It assumes a transport fixed in advance with session 7, max_frame64, inflight2/window256.
The meaning of the time is defined by this fixture; it does not add an operation to the OEP standard interfaces.
The fixture's op16 sets no short processing limit for the whole request.

The lower bound of the wait follows core §4.4: "the processing limit of the whole request + 1 second + transfer time".
The processing limit is max_op_ms; the argument's waiting time alone is not used as the limit.
fn 0 confirm/list/describe/clock/keepalive/lock_state use min(max_op_ms, core_quick_op_max_ms=1000),
and 1000 ms before max_op_ms is known (a lower bound of 2 seconds plus transfer time). The registry's core_quick_op_max_ms and max_op_ms_max are also matched.
The op16 request message is 14 bytes, the length frame 16 bytes, the COBS frame 19 bytes. The clock request is 10/12/15 bytes.
length has a transfer time of 0; UART (wire length + 64×3)×10/115200 seconds.

The production Host chooses the processing limit per request (Host.op_bound_ms, checked by the host-wait checks).
This sample adapter gives the processing limit explicitly to SerialLink.expected_s, and checks only the receive loop's start point and the handling of the lower bound.
The report's `bound_source` records this choice.


- HOST-DEADLINE-SINGLE: length/COBS, (argument, max_op_ms) = (0,1)/(0,5000)/(1500,5000)/(5000,5000), write 400/4000 ms. The reply completes 1 ms before the needed wait, and it checks that the Host can receive until that completion. The condition with an argument shorter than max_op_ms detects a lower bound from the argument alone.
- HOST-DEADLINE-TIMEOUT: length/COBS, (0,1)/(1500,5000), write 4000 ms, no response. It waits at least the needed time from the end of the write.
- HOST-DEADLINE-PIPELINE: length/COBS, (0,1)/(1500,5000), write 4000 ms. Two are sent together, and the second's wait starts from receipt of the first's complete response.
- HOST-DEADLINE-PIPELINE-TIMEOUT: the same pipeline with no response for only the second. It waits at least the needed time from receipt of the first's complete response.
- HOST-DEADLINE-QUICK: length/COBS, fn 0 clock (session 0), max_op_ms unknown/300/5000. For unknown and 5000 the processing limit is 1000 ms; for 300 it is 300 ms.

Each reply arrives as its first 2 bytes and the rest 10 ms apart.
It detects faults that count the second deadline from the arrival of the first byte or the end of the batch write.
The clock is controlled in ns, and only the time references of the implementation module under check are temporarily replaced and always restored.
_recover after a timeout is stopped with an observation hook. The results are not extended to resend/resync checks.
The exact match of the return time is a condition of this zero-processing-time fixture; a real CPU is not required to have exact response times.
With no response, at least the lower bound is required, and waiting longer is not forbidden.

The unit tests detect a short timeout, omission of argument/margin/transfer, the write start as the start point, the pipeline write / first byte as the start point,
early results/timeout, different corr, rewrites, wrong floor/length, and the real request/response bytes.
Real-clock accuracy, physical transports, resync/retry/own-link negotiation, different processing limits within a pipeline,
short processing limits set by operations, the production Host's choice of processing limit,
and integration with the production Host are not checked. full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_host_deadline.py
```
