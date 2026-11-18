# Notification output with bounded retention: a representative fixture

[日本語](notification-output-sample.ja.md)

`tests/test_conformance_notification_output.py` is a limited raw fixture that checks core §10.3/§10.4.
It does not implement a public general notification check API or a production virtual profile.
It defines subscribe/unsubscribe and a kind1 event with a fixed 58-byte payload on its own `io.example.notice`, and checks the common part of notifications apart from OEP-specific operations.

```sh
uv run pytest tests/test_conformance_notification_output.py
```

fn7, SID1 and the event token are choices for the test. They are not made the spec of hardware fn/pins/wiring.
The stimulus the caller passes is handled by `event(token)`, retention and output by `BoundedOutput`, and independent decoding by `decode(bytes)`.
To move this to hardware, prepare the target's operation, stimulus and observation adapters, and also record the real driver's send state.

It uses max_frame64, 1 inflight, cache 0/1, one frame slot for notifications and one response slot.
It accepts a request that reads after only 7 bytes of a notification are written out, finishes the rest, then returns the response. Dropping a partial frame or inserting another frame into it is not allowed.
When a notification not yet started and a response are both pending, the response goes first.
An event that arises while the slot is in use is dropped, its seq is consumed, and a gap is left in the next event's seq.
One notification packet is 66 bytes including the length, at most max_frame×2=128 bytes. It is not a Python object or a RAM measurement of all endpoints.

Under the representative condition of 48 bytes of data retention / max_delay_ms0, a subscription with min_bytes65535 is refused with unavailable cause2, and the existing condition/seq are kept.
The next valid subscription starts from seq0 and drops the old subscription's unsent notifications. unsubscribe also drops unsent notifications, and no new events are sent after it.
Faults are injected into sender fragmentation, subscription refusal/replacement, unsubscribe, seq gaps and response priority, and each case detects them.
The report includes all raw inputs, message exchanges, output chunks, the chosen observations and unfinished SIDs.

The one frame slot is this fixture's policy; the same slot count is not required of every implementation.
The transfer is a logical length stream; real OS/USB/UART writes, timed batching, data notifications, the whole subscription lifetime, state during long reads and continuous load are not checked.
It is a representative case with controlled stimulus and output order and does not claim full conformance. The report has full_conformance=false.
