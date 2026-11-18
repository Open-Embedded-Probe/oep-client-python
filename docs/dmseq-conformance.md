# Checks of probe-side dmseq framing

[日本語](dmseq-conformance.ja.md)

`DmseqChecks` checks the host rules of the selected SPEC's `target-console-dmseq.ja.md`,
`oep-if-console.ja.md` §1 to 3 and `oep-if-debug.ja.md` §4.6 in the third stage
`oep-interface`. Here the host of the dmseq document means the OEP probe side.

Pass `Checks(send, registry, unit)` and `SampleDmseqAdapter(source)` explicitly and
call `DmseqChecks(checks, adapter).run()`. The public code does not search for equipment or private benches.
The adapter's mailbox words, poll and reset stimuli and state, and each check's raw exchanges are saved in the report.

It assumes the selected console declares mechanism 2.

- It checks the normative CRC-8 check values FB/F3 and word examples 3298/F300, payload lengths 0 to 6, and the boundary 3 at which DATA1 is read.
  The checking side computes the CRC by integer polynomial division and does not use the model's bitwise computation.
- For N=7 and all-ones words, an 8-bit corruption of each CRC byte, and all 128 state bytes with T=0, there is no response, no change of synchronization and no queue consumption.
- It checks the 4 combinations of unsynchronized S/A, does not fix the unused state while unsynchronized to 0, and checks separately the first SYN, a normal duplicate and a SYN duplicate.
  Even with the same S, if the previous acceptance was non-SYN, a new SYN resynchronizes. A new SYN drops the old pending.
- Even with the same payload, if S flips it is accepted as new output. TO alone does not resynchronize, and the sequence rules apply.
- pending is resent until ack and is not taken from the queue twice. Even on a duplicate target frame, if A acks the pending H,
  H flips and the next up to 2 bytes are loaded. It checks an empty queue and refilling, and writes of 0 to 5 bytes with a 4-byte capacity.
- During halt/debug execution, the mailbox is not touched, and on resume DMSTATUS is read first. clear does not drop pending/queue/synchronization.
- The stimulus of a detected havereset keeps the connection and buffer/queue, returns dmseq to unsynchronized, and adds restart/detail 1.
  The mark is read back with OEP's marks op. Even if a SYN with the same S comes right after reset, it is accepted as new.
- The synchronization, pending and queue of two connections are independent. close releases the state, and a new stream gets a new ID and is unsynchronized.
  A write resend does not regenerate the queue or resources, in live state or after close.

The independent `DmseqModel` adds mechanism 2 to the SDI/DMDATA fixture. It keeps
synced/last_s/last_syn/h/pending per stream. `reset_target(connection)` is
a logical stimulus of a detected target reset; it does not reset hardware or actually observe havereset.
Each case does a logical restart, and all mailbox accesses are saved in per-poll snapshots.

The unit tests include the normal path, the normative CRC examples, missing preconditions and fault injection.
```sh
uv run pytest -q tests/test_conformance_dmseq.py
```

`full_conformance` is false. The target-side resend/timeout algorithm,
arbitration with real riscv-dm requests, physical DMI/timing/delivery, production Host integration, other capacities and concurrent pollers are not checked.
The probe-side framing checks alone are not treated as conformance of the target side or the whole console.


The mark conditions of console §2 are also checked.
A new SYN after synchronization (same S / different S) requires restart/detail 2, a valid TO (new/duplicate) lost/detail 0x40 (target_timeout),
and a CRC mismatch lost/detail 2 (framing); they are read back with the marks op.
It detects faults that emit restart/resync or lost/TO from a CRC mismatch, N=7, or SYN/TO in host-owned words.
A restart for the first synchronization is not required, and the number of marks for repeated observations is not fixed.
The unit tests also check models with missing marks, false notices of invalid flags, and aggregating marks of the same frame.
The virtual model adds a mark for each observation. No aggregation history or wire change is needed.
