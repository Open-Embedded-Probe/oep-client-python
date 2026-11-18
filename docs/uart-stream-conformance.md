# Common stream checks applied to a plan-owned UART

[日本語](uart-stream-conformance.ja.md)

`SampleUartStreamAdapter.adoption` states the adopted conditions: fn 2 with read/marks/clear/mark/write,
byte ring 64 / mark ring 5 / TX queue 4.
It reuses the StreamChecks asserts for position, paging, overflow, mark cursor/wrap/eviction and the u64 boundary.
It also checks clear resend and write acceptance/result/prefix/resend.

The independent stream sample ([stream checks](stream-conformance.md)) keeps its receive buffer even at session end, but a UART is a plan resource and goes away at end.
That sample's own persistence is not applied to the UART by mistake. For each condition, the check records wire open/plan_apply before checking,
and cleans up with wire end. The UART's configure/plan lifetime is left to the third-stage checks.

After a logical restart, it sets u64 position / u32 sequence seeds on the unowned fixture.
The injection of external marks is a stimulus for the common cursor/selector parts; it is not evidence that the UART actually detected a target reset.
Position/mark stimuli, wire exchanges, and plan creation/cleanup are all recorded in the trace.
Physical delivery, concurrent producers, notification data and other ring/frame geometries are not checked. `full_conformance` is false.
The public side does not depend on private benches or equipment settings.

```sh
uv run pytest -q tests/test_conformance_uart_stream.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_uart_stream.py
```
