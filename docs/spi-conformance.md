# Logical checks of the SPI target

[日本語](spi-conformance.ja.md)

`SpiChecks(checks, adapter).run()` checks fixture §4, plan §2 and core §6/8/9 of the selected SPEC in the third stage.
Pass `Checks(send, registry, unit)` and `SampleSpiAdapter(source)` explicitly.
The public checker does not search for private benches, neighboring repositories or equipment settings.
The common declarations and SPI metadata are applied separately.

- It checks state 0, configure before plan start, read_rx while unconfigured, one each of roles 1 to 4 on separate pins, and no change before refusal.
- It checks the mode/bit_order widths of configure, whether the LSB feature is present, unknown TLVs/raw tags, and mode/status.
- sck_filter (SCK-FILTER): if features bit1 is declared, sck_filter_ns is 1 or more; the TLV is accepted with 1, 0 or absent, and 2 or more and wrong lengths are refused with malformed.
  If not declared, a critical TLV is refused with unsupported (the received tag). The logical fixture is a configuration that does not declare it.
- Logical CS/SCK edges are given in the four modes and the declared bit orders, and it checks the CPOL/CPHA capture edges, MISO and MOSI storage.
  An 11-bit transfer that ends mid-byte is ceil(bits/8) bytes, with bits not arrived set to 0.
- No TX / insufficient TX / MISO when not armed is 0. MOSI when not armed is dropped and counted in transactions/errors.
- Even with CS active, 0 bits leave the queue/totals unchanged, and arm waits for the next transfer. A nonzero transfer consumes arm.
- Exceeding length keeps the real bit count and truncates only data to a prefix. A queue overflow drops the new transfer and counts it in transactions.
  Even when both happen, errors increases only once. It checks FIFO/pending and the totals that remain after read.
- An optional timestamp is checked for width and the clock at CS deactivation only when present. An empty read_rx checks count 0 and pending, and does not fix undefined bits values to 0.
- It checks arm length/count, busy, completed failed with no side effects when it cannot prepare, and immediate use once it can.
  A resend of a failed request also returns the past failed and does not arm anew.
- configure clears queue/arm/totals. A resend of the same configure/arm/read_rx request does not run old side effects.
- MISO switches between driven and HiZ with CS, and SCK/MOSI/CS stay inputs.
  For MISO, not only the computed send bytes but the driving state of the logical pin is observed.
- A plan that does not satisfy the set of roles 1 to 4 is unsupported (tag), and a duplicate of the same assignment malformed. In state 1 the same assignment changes nothing, and a change is cause 6.
- An initialization failure in configure is completed failed; it returns to state 0 and all zeros, arm gets cause 6, and a new configure recovers.
- release/end/expiry/force return to state 0, mode/order 0, no queue/arm/totals, pins idle.
  release/end/expiry/force with CS active while capturing 1 bit are also checked, and no incomplete capture or MISO driving is left.
  Old requests after force do not touch the new owner. status works on SID 0; other ops require a session.

The independent `SpiModel` is a logical fixture with max_length 8, queue_depth 3, max_clock 1000000 Hz, cs_setup_ns 0,
channels 0 to 3, idle input. It states two configurations explicitly: declaring LSB or not.
`cs`/`clock`/`transfer`/`ready`/`advance` are recorded logical stimuli.
clock reads the real logical MISO pin at the capture edge. ready is a stimulus for when arm cannot be prepared,
and its reason is not added to the norm. CS start/end, clock edges, pin driving, queue/state and wire exchanges are saved in the trace.

The unit tests check the 2 normal configurations, common declarations/metadata, wire faults per case, FIFO/arm/bit order/phase/uncaptured bits,
double errors and wrong counter/initialization/driving/timestamp, double side effects on resend, missed release while capturing with CS,
omission of optional timestamp/setup, unused bits of an empty read, missing preconditions and invalid stimuli.
`full_conformance` is false. Physical waveforms, the cs_setup_ns guarantee under load, 255 queue / u32 bits / counter limits,
other pins/channel_group, persistent config, concurrent controllers/writers/deferred responses and the production Host are not checked.
A valid arm in state 0 is unavailable with cause 6 regardless of the plan and does not change the state.
A resend of a refused request stays refused even after configure; only a new request can arm.
When configured but unable to prepare an arm, the probe answers completed failed, told apart from the unconfigured state's unavailable. The priority order when unconfigured and invalid length and so on overlap is not checked.

```sh
uv run pytest -q tests/test_conformance_spi.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_spi.py
```
