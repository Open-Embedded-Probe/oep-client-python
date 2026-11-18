# Logical checks of the I2C target

[日本語](i2c-conformance.ja.md)

`I2cChecks(checks, adapter).run()` checks fixture §3, plan §2 and core §6/8/9 of the selected SPEC in the third stage.
Pass `Checks(send, registry, unit)` and `SampleI2cAdapter(source)` explicitly.
The public side does not search for private benches, equipment settings or neighboring repositories.

The second-stage common declarations and I2C metadata are applied separately.

- Unconfigured, it ACKs no address. configure before plan and read_rx/preload_tx while unconfigured are refused.
- role 1/2 are assigned one each, to different channels. A set that does not satisfy this is unsupported (the role_assignment tag); a duplicate of the same (fn, role, channel) is malformed.
- In state 1, plan_apply with the same assignment changes nothing, and a change of assignment is unavailable cause 6 and does not change the plan, queues or pins.
- An initialization failure in configure is completed failed; it returns to state 0, clears queues, slots and totals, and keeps stretch. read_rx gets cause 6, and a new configure recovers.
- It checks the 7-bit width and reserved ranges of address, the legal boundaries, and that accesses from other than the selected address do not consume the queue.
- A write from START to STOP or the next START is returned as one frame, FIFO. An optional timestamp is checked for width and boundary time only when present.
- An address-only write is not counted. Exceeding max_length keeps the prefix, and a queue overflow drops the new frame.
  Even when both happen, errors increases only once, and the dropped frame is not counted in rx_frames.
- preload is FIFO. Even a short read consumes the slot; a long read fills the rest of the same slot with FF and does not mix in the next slot.
  When full it is refused with cause 2, and after a slot is consumed, preload works again.
- configure initializes RX/TX/totals and keeps stretch. A fixture without stretch gives unknown_operation.
  With stretch, it checks state 0, the limit, application only to write data bytes / after the address of a read, and disabling with 0.
- On a fixture that declares nack_from (features bit1, critical), giving 2 ACKs the address and the first data byte and NACKs the second and later,
  and the NACKed bytes also stay in the frame. A configure without it ACKs all bytes. A fixture that does not declare it refuses with unsupported (the received tag).
- The internal pullup follows the declaration. A logical pin is in the open-drain released state, and push-pull high is detected.
- A resend of the same read_rx/preload/configure request does not repeat side effects.
- An unknown critical TLV in configure is unsupported (the received tag), a broken TLV malformed. A short/extra fixed part of each op is malformed.
- release/end/expiry/force return it to unconfigured, drop queues, totals and stretch, and return the pins to idle.
  Old requests after force cannot change the new owner's target. status can be read on SID 0, and other operations require a session.

The independent `I2cModel` is a logical fixture with max_length 8, queue_depth 3, max_clock 400000 Hz and a stretch limit of 1000 µs.
The role candidates are channels 0/1, and idle is input. It uses an explicit logical ms clock and completed controller transaction stimuli.
Whether stretch / internal pullup / nack_from are present is stated in the constructor, and 4 configurations are checked.
It saves ACK and stretch counts and end times, queue/pin/journal, and wire exchanges.
These stimuli do not reproduce the edges, waveforms or real time of a real bus.

The unit tests check each configuration, common declarations/metadata, wrong wire responses in each case, FIFO / double errors / missed initialization / slot reuse / wrong timestamps / wrong driving,
double consumption on resend, omission of the optional timestamp, missing preconditions and invalid stimuli.
`full_conformance` is false. Physical I2C, persistent config, other pins/channel_group, queue above 255 / u32 counter boundaries,
concurrent controllers/writers and deferred responses, and production Host integration are not checked.

```sh
uv run pytest -q tests/test_conformance_i2c.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_i2c.py
```
