# SDI/DMDATA mailbox checks of the standard console

[日本語](mailbox-conformance.ja.md)

`MailboxChecks` checks §1 to 3 of the selected SPEC's `oep-if-console.ja.md` in the third stage
`oep-interface`. Pass `Checks(send, registry, unit)` and
`SampleMailboxAdapter(source)` explicitly and call `MailboxChecks(checks, adapter).run()`.
The public code does not search for private benches or equipment settings. Raw exchanges, stimuli and DMI access records stay in the report.

The target is a logical sample that explicitly provides mechanism 0/1.

- SDI checks L=0 to 7; for L=1 to 7 it receives in the order DATA0 → read DATA1 → DATA0=0. For L=0 it does nothing. Even for a short frame, DATA1 is read.
  L=8 to 255 are not frames, so they are not received and are cleared with DATA0=0 (console §3.1).
- SDI has no send queue. A write to an SDI stream is unsupported, including zero length (payload 0).
- DMDATA does nothing for all 128 values of the state byte with T=0. With T=1, it checks the empty frame L=4, reception for L=5 to 11,
  and clearing for L=0 to 3/12 to 63. Bit 6 is ignored. DATA1 is read only when 4 bytes or more are received.
- For a valid target frame, DATA0 is written exactly once and DATA1 is not written. Up to 3 leading bytes of the send queue are
  consumed, and 0 is answered when it is empty. An invalid frame is cleared with 0 and does not consume the send queue.
- A write with a 4-byte capacity checks full acceptance, partial acceptance, full, zero length, recovery of space and order.
  accepted means acceptance into the queue, not confirmation of delivery to the target.
- While halted, DATA0/DATA1 are not accessed. Polling stops while a debug request runs.
  On resume, DMSTATUS is read before DATA0. The queue and receive buffer are not changed while stopped.
- read does not consume, and clear drops only the receive buffer. A write resend does not refill the queue. The same frame after an ack is not delivered again.

The independent `MailboxModel` adds a mailbox on top of the attach sample.
`mailbox_frame(stream, data0, data1)` places logical u32 words, and `mailbox_poll(stream)` is run explicitly.
`mailbox_halted(stream, bool)` and `mailbox_debug_busy(stream, bool)` are stimuli of external state,
not real target operations. It reads DMSTATUS anyhalted and records DATA0/DATA1 accesses.
The events of a state snapshot cover only the latest poll, and each poll's snapshot is stored in the adapter trace.
Each case does a logical restart into a new boot. No hardware or firmware is operated.

The unit tests include fault injection. Independent checks detect missing/extra DATA1 reads/writes, violations in clearing invalid frames,
overwriting its own response, wrong consumption / reversed order / shortening of the queue, access while halted/in debug, DMSTATUS order violations,
acceptance of SDI write, misreading bit 6, and an ack not reflected.

```sh
uv run pytest -q tests/test_conformance_mailbox.py
```

`full_conformance` is false. dmseq, target self reset / restart mark, arbitration with real standard riscv-dm
requests, physical DMI/timing/delivery, production Host integration, other queue/ring capacities and concurrent pollers are not checked.
The state switches of this sample alone are not taken as verifying the wire implementation of standard debug requests.
