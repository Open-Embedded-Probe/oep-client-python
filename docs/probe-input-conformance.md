# Probe roles and request headers

[日本語](probe-input-conformance.ja.md)

Name the SPEC checkout explicitly and run `ProbeInputChecks(SampleProbeInputAdapter(), spec=path).run()`.
It matches the body SHAs of core/transports and the expected registry values. It uses no private equipment and no OS transport.
It feeds raw messages into the virtual Core and observes the responses and the owner/resources/cache/lease directly.
fn1/op16 is sample resource creation, not a standard OEP-specific operation.


- DISCARD: feeds all 255 role values other than 0x01, at 10/11/64 bytes. Including known result/event/data, it checks no response and no state change.
  It cuts role 0x01 requests to 0 to 9 bytes. Because the header is incomplete, they are dropped without even a malformed response.
- CONTROLS: a 10-byte header or a short fixed payload is refused with malformed, and a correct request runs. A dummy backend that only drops does not count as success.
- EXPIRY: a discard at 999 ms does not expire early, and a discard input at 1000 ms still runs the expiry. Old history/numbers are kept; a short request after the end gets no response, and a proper request with the old SID gets no_session.
- ENDED / EXPIRED: after end and after expiry, it feeds the same roles and short requests as DISCARD and checks no response and all snapshots unchanged.
  It checks the seed to tell apart end's new history record and expiry's kept existing history. It also includes controls: a normal SID0 clock is answered, and a new request with the old SID is refused with no_session.

Snapshots before and after a discard are compared at the same logical time. Resource release when the clock reaches the deadline is told apart as a correct timer effect.
The owner is seeded with non-empty text, so owner changes that an anonymous owner would miss are detected.
It detects responses / history discard / last / allocator / generation changes and clock loss when unowned.
It also detects a real Core fault that drops roles correctly only while owned and runs other roles when unowned.
The no-response predicate `len(data) < 10 or data[0] != 1` in Core._handle is also recorded in the correspondence audit as a reviewed source.
The finite input checks at three message lengths are not taken as real send tests of all transports / frame limits / arbitrary payloads.
Physical framing, empty transport frames, the dependent resources of all extensions, and the combination of production Host role handling and recovery are a separate scope.
full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_probe_input.py
```
