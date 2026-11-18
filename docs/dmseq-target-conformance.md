# Logical checks of target-side dmseq

[日本語](dmseq-target-conformance.ja.md)

`TargetDmseqChecks` checks the target rules and timeout of the selected SPEC's `target-console-dmseq.ja.md`
in the third stage `oep-interface`. Pass `Checks(send, registry, unit)` and
`SampleTargetDmseqAdapter(source)` explicitly and call `TargetDmseqChecks(checks, adapter).run()`.
The public code does not search for equipment or private benches.

The target is a logical fixture with an explicit short wait of 20 ms, long wait of 1000 ms, input capacity of 2 bytes and
output queue capacity of 6 bytes. These capacities and wait limits are not required of general implementations.
This result alone is not reported as meeting the spec's real-time lower bounds.

- target payload 0 to 6, write order DATA1→DATA0, CRC, S/A/SYN/TO and the 4 combinations of initial S/A.
- A bit 7=1 word that is not its own is resent immediately. Invalid M/CRC/K resend the same frame and do not advance acceptance or the deadline.
- DATA0=0 is not taken as an ack. It resends every short wait while keeping the waiting time until timeout.
- The boundaries of short wait 19/20 and long wait 999/1000 ms. A timeout adds TO to the same S/payload and recomputes the CRC.
  It keeps it posted and resends every 20 ms, and drops new output during the timeout.
- A valid late answer returns to normal operation, and the next frame clears TO. After an ack it returns to the long wait.
- A duplicate input of H is not passed twice; a new H is accepted even with the same content. M=0 does not update A.
- When the input is full/short, the payload and A are not accepted, and the same H is accepted after space recovers.
- The output queue is sent in order from the head. A logical mailbox round trip with an independent probe model checks OEP write/read,
  backpressure of target input, pending retention and resend, and duplicate suppression.

The `Target` of the independent `TargetDmseqModel` has an explicit logical clock.
It is stimulated with `target_begin(s, a, payload)`, `target_host_word(u32)`, `target_service(advance_ms)`,
`target_write_output(bytes)` and `target_read_input(count)`.
`target_link_poll(stream)` connects a live dmseq stream with the target's word and advances both once.
Snapshots of both target and probe and the DATA0/DATA1 operations of each poll are stored in the adapter trace.
The expected CRC is computed by polynomial division on the checking side, not by referring to the model's bitwise computation.

The unit tests include fault injection, missing preconditions and no-change checks for invalid stimuli.
They detect DATA1 order, too-early timeout, treating 0 as an ack, stopping on a foreign word, accepting invalid M, ack on duplicate/full input,
A updated by an empty ack, S flipped on timeout, missing periodic resend, accepting output during timeout, SYN kept, reversed output,
and reversed input in a round trip.

`full_conformance` is false. Real time, the clock while interrupts are disabled, target firmware integration,
other capacities/waiting methods, and OEP resync/TO/framing mark conditions are not checked. Even if the outer runner uses a real clock,
the waiting times of this target fixture are reported separately as results of the logical clock.

```sh
uv run pytest -q tests/test_conformance_dmseq_target.py
```
