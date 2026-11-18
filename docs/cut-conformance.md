# Logical checks of the response and release on a broken wire

[日本語](cut-conformance.ja.md)

`CutChecks` and the independent `CutModel`/`SampleCutAdapter` check OEP debug §2, common §1/2 and
core §5.1/9. The common declarations are the second stage; the preconditions and behavior the third.
The behavior cases use RISC-V DMI over an RVSWD connection and a dmseq console.
They work with only the public Python library, and the caller chooses the SPEC/registry.
`full_conformance` is false. It does not search for equipment settings or private benches.

The fixture's broken-wire policy is "when two line operations follow each other with no successful operation in between". The individual
packets within a request and retries inside the console are not counted as separate operations. This threshold is not normative.
A single line does not close the connection, and a successful operation clears the history. A refusal or a timer alone does not
decide a broken wire. For ambiguous delivery of internal packets and pin rest, use the [retry fixture](retry-conformance.md).

- When it is decided during a request, the connection and child streams are kept until the failed/partial response with status line has been written.
  After that, the child stream/protocol/mailbox, the connection and the pins are released in that order.
- `accept(request)` is a stimulus that processes the request and stops the response writer. It does not return the response body to the host; it returns only ready/corr.
  Passing the same request to `Checks.exchange` advances the writer. Raw requests/responses are recorded in this exchange.
- A resend of the same request after closing returns the saved result. It is not resent to the wire and does not revive resources. Using the old ID in a new request gives no_resource.
- When it is decided during a console read, the connection and stream are closed without a mark (there is no one to read it. console §2).
  A read on the old stream gives no_resource.
- The target is kept in the state of the last host operation before closing. A halted hart is not set running on its own.
  Other connections, their streams and the target state are not affected. No idle driving is left on closed pins.

`poll_cut(stream)` is an explicit console stimulus that causes a DMSTATUS read and the broken-wire decision.
`tick()` is a timer stimulus. It is a single-writer fixture that refuses other requests and console stimuli while waiting for the writer;
it is not a concurrent execution model. The order of decision/response-write/release is recorded in the state and adapter trace.
`closed_targets` is an observation record copied for the check, not a mechanism to provide closed resources to the host.
The live stream, receive/send buffers and protocol/mailbox are released, and reads on the old stream are refused.

```sh
uv run pytest -q tests/test_conformance_cut.py
```

From the Arduino uv/pytest environment, use this.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_cut.py
```

The response-write here is the handoff to a logical writer and does not prove real USB/TCP delivery or physical pins.
SWIO/SWD-specific disconnect paths, writer concurrency, lease expiry while waiting for the writer, other thresholds/budgets, resync/TO/framing, packet failures inside poll/wait,
internal writes that cause store, and production Host integration are not checked.

The lease boundary while waiting for the writer is handled by the additional [explicit logical clock checks](writer-lease-conformance.md).
