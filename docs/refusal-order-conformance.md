# Refusal order for requests with several invalid conditions at once

[日本語](refusal-order-conformance.ja.md)

Name the SPEC checkout explicitly and run `RefusalOrderChecks(SampleRefusalOrderAdapter(), spec=path).run()`.
It matches the body SHAs of core/transports and the expected registry values. It uses no private equipment and no real transport.
It sends raw requests to the current Core and the sample resource extension and checks the order of core §4.3.

It feeds 10 kinds of requests in 6 states: owned/expired times normal retention / not enough retention for the request / not enough retention for the response.
It sets the real remember_max to 2048/10/16 to create situations where only the request or only the response cannot be kept. The boundary is not made by faking the cache.

- corr0, unknown fn, unknown op and session_required on SID0 come before the resend, ownership and payload conditions.
- A matching resend returns the saved response. History remains even after expiry. Not enough retention gives result_lost.
- Bytes that differ from the kept request are malformed. Even when the response is lost, consistency with the kept request is checked first.
- An unrecorded corr at or below the past maximum is result_lost. This comes before current ownership and payload interpretation.
- A foreign SID is refused with locked/no_session even when the payload is also invalid.
- When an invalid kind, an unknown critical TLV and an explicit admission refusal are all present at the last stage, any one of the applicable reasons is fine. No extra fixed ranking is added among malformed/unsupported/window_exceeded.

Early header refusals, resends and foreign-SID refusals do not change the owner/resources/cache/lease.
A new refusal response to a valid owner renews the lease and the history without changing resources.
A new no_session to the last SID after expiry is also recorded in the history, but it does not revive the lease or resources.
Do not confuse this history update with "a bug where refusal has side effects".
admission_reason=6 is the current Core's explicit model hook, not proof of physical buffer capacity.

It also checks that recording a new refusal does not break existing history, and that request/result bytes are recorded according to the retention limit. The cache observation is normalized debug information, not the wire format.
It also checks the history update of a new refusal on the last SID that ended.
Combinations of payload/resource states of all extensions, and integration with real admission, transports and the production Host are not checked.
full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_refusal_order.py
```
