# Logical checks of wire retries

[日本語](retry-conformance.ja.md)

The checks of OEP debug §2/4.1 are provided by `RetryChecks`, and an explicit logical fixture by `RetryModel` and
`SampleRetryAdapter`. Any project that uses OEP can use them with only the public Python library.
The caller chooses the SPEC/registry explicitly and passes it to `Checks`. It does not search for private equipment settings or benches.

Of the three stages, the common declarations are the second stage, and the preconditions and behavior are the third.
`full_conformance` is false. It runs independently on a dedicated fixture.

- The host's DMI steps are not repeated, for reads or writes, if they may have arrived. DATA writes and
  resumereq are included. The failed step and those after it are not counted in done, and the answer is failed/partial.
- busy can be retried as not accepted. The fixture retries at most 2 times. When used up, it answers with wait.
- The probe's own DMSTATUS read can be retried on line. The fixture retries at most 2 times; the second time temporarily at
  half speed. The connection's speed and the target state do not change.
- Driving stops during the exchange after a line failure. Only no pull or the correct idle direction is accepted.
  A pull-up is not accepted on the clock with RVSWD idle_clock=1. After success it returns to normal idle.
- This fixture does not close the connection on line alone. Success on another connection does not restore the failed side's
  pins. A resend of the same request with a failure response returns the saved result and is not resent to the wire.
- An invalid DMI step later in the request is also refused before touching the wire.

`plan(parent,[0,1,2,3])` passes a result sequence of ok/wait/line/fault. Writes of 0/2 are modeled as
having arrived, which makes the harm of a resend observable. The delivery decision on fault is a fixture condition, not a norm.
`probe_read(parent)` is a stimulus that explicitly causes a probe-internal read; it is not a new OEP operation.
`idle(parent,0|1)` is also a logical fixture setting and does not mean an implementation of the attach TLV.
state/trace store the packet speed, the delivery decision, and the mode/level/pull after failure.

```sh
uv run pytest -q tests/test_conformance_retry.py
```

From the same uv/pytest environment on the Arduino side, use this.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_retry.py
```

It does not prove waveforms, electrical characteristics or the real clock. Broken-wire judgment and the release order after the response, console link-lost,
resync/TO/framing, packet failures inside poll/wait, internal writes that cause store, other retry budgets and
production Host integration are not checked. Logical checks and hardware conformance are recorded separately.
