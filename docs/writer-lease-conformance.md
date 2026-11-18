# Lease checks for standard resources and the response writer

[日本語](writer-lease-conformance.ja.md)

`WriterLeaseChecks` checks core §5.1/6.1/9 in the first stage.
`WriterLeaseModel`/`SampleWriterLeaseAdapter` is an explicit logical fixture with a wire connection and a dmseq console.
The common declarations are re-run separately as the second stage, and cut separately as the third stage.
It works with only the public Python library and the explicit SPEC/registry, and does not search for private benches or equipment settings.

The fixture has lease 1000 ms, max_op_ms 5000 ms and a single writer.
`advance(ms)` moves the logical clock, and `execution(ms)` sets the logical execution time of the next valid DMI request to run.
No real-clock sleep or USB transfer is used. The clock is the fixture's continuous counter; the endpoint keeps the boot uptime separate.

- The count restarts at 1000 ms from when the response is generated; waiting for the writer and completing the write do not renew it (core §6.1).
  A rejected response that passed the session decision also renews it. A resend of the same request does not.
- An execution time of 1500 ms is longer than the lease, but the deadline is not counted during execution. It counts from response generation and keeps counting while waiting for the writer.
- When the deadline is reached while the writer is stopped, the owner and its child stream/protocol/mailbox, the connection and the pins are released.
  The target/halt state just before closing is kept. Both connections are released. The stream is released with its buffer, and no mark is left (console §2).
- A connection already judged as a broken wire is closed the same way, as cleanup on session expiry.
- Responses left in the queue are returned with their original results. An old writer token does not revive ownership or resources.
  A resend of a saved result does not re-run. A new request from the old session gets no_session, or locked if a new session owns the probe.
  New resources do not reuse old IDs of the same boot.

`closed_targets` is an observation copy for the check, not a live resource.
Lease progress is handled by `tick()`. A case that stopped the writer always resumes it in finally and cleans up.

```sh
uv run pytest -q tests/test_conformance_writer_lease.py
```

From the Arduino uv/pytest environment, use this.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_writer_lease.py
```

Send completion on a real transport, real clock error, writer concurrency, races with force, other budgets/geometry and
production Host integration are not checked. `full_conformance` is false.
