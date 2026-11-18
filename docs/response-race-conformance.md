# Races between pending responses and force

[日本語](response-race-conformance.ja.md)

Name the SPEC checkout explicitly and run `ResponseRaceChecks(SampleResponseRaceAdapter(), spec=path).run()`.
It matches the body SHAs of core/transports and the expected registry values. It uses no private equipment and no OS transport.
It drives the current Core.handle(defer_send=True) and response_sent through 3 logical writers and an explicit millisecond clock.
fn1/op16 is a sample extension that creates resources, not a standard OEP operation.

Each of the following 6 kinds runs with lease 1000/60000 ms (RESPONSE-RACE-<kind>-<lease>).

- FORCE: while old owner 7's success response waits, 8 forces and creates a new resource. Completing the old response later does not change the new lease/resource/cache.
- REJECTED: even when the old owner's request got a refusal response, the old completion notice does not renew the new owner.
- DOUBLE-FORCE: force 7→8→9, and complete the responses of 7 and 8 late. Neither affects 9.
- EXPIRE-NEW: end 8's new lease at the exact deadline, then complete 7's response. Resources and the lock are not revived.
- NO-FORCE: 8's open is refused with locked. That refusal response does not renew 7's lease. 7's deadline is set when 7's response is generated, and does not change on that response's send completion or on a resend (core §6.1).
- RETURN-ID: as a raw Probe stimulus, force 7→8→7. Because the internal ownership periods differ, the old 7's completion notice is not applied to the new 7. A Host must not reuse a session_id that has ended, and this condition is not a Host usage example. Old wire requests are not resent.

It stores the real request/reply bytes, completion tokens, times, the Core owner/generation/lease, resource numbers and live state, and the cache.
force releases the old resources and then sets up the new owner, and resource numbers are not reused within the same boot.
Old wire requests after force get locked, and after the later expiry no_session. An old pending response keeps the result at processing time.
token/generation are observations of the current Core's internal realization and do not define new wire fields.
The contract is that an old response does not change a later ownership period; another Probe is not required to use the same token representation.

It detects corr/result/token, early renewal, resource/cache mix-up, force refusal, the new lease deadline and confusion of ownership periods with the same ID.
For a corr fault, the earlier pending request is picked by order and matched byte by byte independently.
These are races with a fixed logical order; real CPU concurrency, physical transport completion, dependent release of all extension resources and production Host integration are separate checks.
full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_response_race.py
```
