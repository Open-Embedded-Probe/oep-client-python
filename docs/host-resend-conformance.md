# Resend of the same request and return after an unknown result

[日本語](host-resend-conformance.ja.md)

Name the SPEC checkout explicitly and run `HostResendChecks(SampleHostResendAdapter(), spec=path).run()`.
It matches the reviewed SHAs of the core/transports body texts (shared with the response wait deadline checks) and the wait values. It uses no private equipment and no OS ports.
It connects SerialLink.send/exchange/resync/recover_transport, the virtual Core and ResourceModel
through a virtual clock and a scheduled stream. A fixture with max_frame64/inflight2 fixed in advance and owned session 7/lease60000.
It does not wait on a real clock, and temporary replacements of the module's time references are always restored.

The fixture's fn1/op16 creates a resource(kind u8), and the success payload is id(u16). op19 is a read-only state.
These are not standard OEP operations but a sample extension that tries the core's resend rules.

- LOST-COBS/LENGTH: the first response, which the Probe executed, is lost. After the lower bound of the wait, the Host resends the same full bytes and gets the same id. The Probe does not execute a second time and does not renew the lease on the resend.
- PIPELINE-COBS/LENGTH: only the second response of 2 requests is lost. Excluding the resolved first one, only the unresolved second one is resent with the same full bytes. There are still 2 resources.
- BAD-CRC: on an owned COBS port, a response with a broken CRC arrives, and the same request is sent again. The waiting time for a missing response is not required for this corrupted response.
- FAILURE-RECOVER: both the first and the resent responses are lost, and TransportFailed is returned. Next, after the recovery confirm, a read-only state query checks that the first resource already exists. The original state change is not repeated with a new corr.
- FAILURE-BLOCKED: the recovery confirm is also lost. The transport stays failed, and neither the state query nor a new state change is sent to the Probe.

length, as the fixture of a reliable non-TCP transport, also goes through a real resync.
TCP connection close/reopen, the link's own confirm waiting time and the real clock/physical transport are not included in this result.
The number of resends on corruption/loss is this client's resend=True setting. It does not mean the SPEC requires resending of every Host.
The lower bound of the wait follows core §4.4: max_op_ms + 1 second + transfer time (the fixture's op16 sets no short processing limit).
The fixture's max_op_ms is 2000 ms, and the checking side computes the lower bound independently from the report's values.
The sample uses SerialLink directly without going through Host, so it gives max_op_ms to SerialLink.expected_s explicitly.
The transfer time is computed independently from the frame length actually written and max_frame64, and it checks that the resend on loss starts at or after the lower bound.
The Probe-side replay (no re-execution, no lease renewal) matches core §5.1/6.1. Byte records are optional, but
this fixture's virtual Core behaves as a Probe that keeps records.
The reply bytes, resource next_id, live state and lease deadline are recorded in the Probe events.

It detects changed corr/payload, changed results, re-execution/lease renewal, early resend, resend of resolved requests,
failed flag/confirm omission, queries before recovery, continuing after recovery failure, and wrong results.
TCP close/reopen, the real clock/transport, own-link confirm time, the resend limit on corruption, other pipeline/cache capacities,
corr wrapping and the half-cycle limit (core §4.1/5.1), the production Host's choice of processing limit,
and integration with the production Host are not checked. full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_host_resend.py
```
