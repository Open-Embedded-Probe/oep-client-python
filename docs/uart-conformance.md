# UART logical behavior and plan lifetime

[日本語](uart-conformance.ja.md)

`UartChecks(checks, adapter).run()` checks the explicit plan fn 1 / UART fn 2 in the third stage.
Codes/tags are read from the selected registry. The public code does not depend on private benches.

The independent UartModel takes RX 0/TX 1 out of 8 channels as candidates and handles plans with both or one of them.
Free pins are Hi-Z. When the plan is taken, the stream is created and TX is held high.
Receive ring 64 bytes, mark ring 5 entries, send queue 4 bytes, formats 0/4/17.
The logical divider clock is 1843200 Hz (integer division 1 to 65535). configure returns the actual value within ±5%. status before configure is baud 0, format 0.
Requests of 1755429/1940210 baud are within range with 1843200; the neighbors 1755428/1940211 are out of range and refused.
The clock is explicit logical ms. This fixture does not prove the feasibility or current of a physical UART.

- Before configure, TX stays idle and nothing is received (status 0/0, write refused). Reception starts after configure. Write refused for RX-only; configure/write for TX-only.
- An initialization failure in configure (caused through the logical fixture's control) is completed failed with baud 0. It returns to unconfigured, drops unsent TX, and received bytes can still be read. A new configure recovers.
- configure/status, 8N1 when format is omitted, undeclared formats / reserved values, raw critical tag, fixed width and first-wins.
- Stream/config/pin state unchanged before and after refusal, unknown TLVs and malformed/count, whether a lock is needed.
- Re-configure keeps the stored received bytes/position/marks.
- read order, non-consumption, more/gap, overflow, framing/parity lost, clear/host mark.
- write prefix acceptance and success/partial/failed, zero length, no queue duplication on resend. A logical drain does not mean physical delivery.
- Changing the assignment after configure is unavailable cause 6 and changes nothing. Plan release/re-creation drops data and the session's configure, but does not roll back the byte position and mark sequence within the same boot.
- end/expiry/force release the stream/queue/pins and keep the position. Old requests after force do not touch the new owner.

The fine boundaries of the common stream are checked separately as the second stage in the [adopted parts checks](uart-stream-conformance.md).
Other pin candidates/channel_group, writer concurrency,
physical baud/waveform/delivery and the production Host are not checked. `full_conformance` is false.

```sh
uv run pytest -q tests/test_conformance_uart.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_uart.py
```
