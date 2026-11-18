# Checks of data notifications and batching

[日本語](data-conformance.ja.md)

This checks the data/event shared seq, the data shape and min_bytes/max_delay of SPEC core §10.2/10.3. The checking side's stimuli and expected values are set first and applied to an independent in-process sample. Hardware transports, response priority, the send queue limit and seq gaps from drops are a separate stage.

The conditions are 2 explicit fns, data stimuli with known bytes and event stimuli with known markers, and reception of all raw notifications. The interface decides the data position, so the adapter returns the start position of the external stimulus. The position the probe returned must not be copied into the expected values. The common checks independently verify the data header, position:u64, len:u16, following TLVs and the data content.

The contracts checked are: immediate send when both are 0, min_bytes only, delay only, sending on either condition, delay from the first byte, events not counted toward min_bytes, the data/event shared seq, per-fn seq, splitting within max_frame, the next batch after a batched send, subscribe resend and open in the same session (new corr and resend) not breaking unsent data/seq, and old data not carried over at session end. The number of data splits is not fixed; the positions and concatenated bytes, and each frame's seq and length are checked.

## Sample contract

`io.github.open-embedded-probe.stream` revision 1, fn 1/2, instance 0/1. Besides the subscription sample's resource ops 16 to 19, the required subscribe/unsubscribe and kind 1 / marker:u32, it has data notifications of byte strings. It is not a standard OEP interface and is not added to the normative registry.

The external synthetic stream has a byte position per fn and advances by the number of bytes stimulated. Subscription and seq reset do not roll back the stream position. The expected position and bytes are given to the model as external input. Within one subscription, data is concatenated in arrival order, and the delay is measured from the first byte. When a threshold is met, it is split into data notifications that fit in max_frame. Subscribe replacement / unsubscribe / session end drop the old subscription's unsent data in this sample. This is the sample's buffer policy and does not add a new norm for all interfaces.

For the sample's handling of ops/resources/errors/unknown values/TLVs, see the subscription and resource samples. It has no specific status/reject/describe tags/pin roles, external specs or chip-specific procedures. The accumulation buffer in the model is not a transport send buffer, and its capacity limits or asynchronous response order are not verified.

## Adapter and running

`DataChecks` runs the [subscription checks](subscription-conformance.md) after IF-DATA-ADAPTER (the adapter preconditions), then reports the IF-DATA-* data contracts. The core and resource checks are also applied separately to the same model. It is not added to the normal CLI automatically, and the supplementary report always has `full_conformance=false`.

Besides functions / stimulate / receive / decode_event of the [subscription adapter](subscription-conformance.md), the adapter provides the following.

- `feed(fn, data)`: supplies the given bytes from outside and returns the externally known leading position. The probe's responses or internal cursors are not the basis of the expected values.
- `loss_free=True`: explicitly declares equipment on which no drops occur with that stimulus amount and observation condition. Data splitting includes a stimulus of max_frame×2 + 17 bytes, so a hardware adapter needs a means not to press the send queue, such as continuously receiving from the transport during the stimulus.
- `observation_ms`: 1 to 50 ms for this data check. An equipment condition for observing the known 400 ms delay separately before and after arrival.

The SPEC allows dropping notifications that do not fit the queue. This check is not a new norm that forbids that behavior; it checks content, seq and batching under stimulus conditions without drops. It is not applied as is to equipment that cannot meet the conditions. If drops or missing bytes occur under the declared conditions, that run FAILs, and it is told apart from checking the drops the norm allows.

On a real clock, the OS monotonic clock is used. On a virtual clock, `wait` and `clock_ms` are connected explicitly to the same external test clock. The check of delay from the first byte also records and verifies that at reception the first byte's deadline has passed and the last byte's deadline has not been reached. A run where a slow observation passed both is not a PASS. The min_bytes side of the OR condition also checks observation before the delay. A PASS with no notification is a result for the given observation window.

`oep_client.conformance.data_model.DataModel` is an independent probe-side model. `oep_client.conformance.data_sample.SampleDataAdapter` has the checking side's byte cursor and builds expected values from the externally input positions. Raw notifications are saved before verification. Broken frames, missing/duplicate bytes, unknown fns and IO failures are not turned into skips either. After an IO failure, the later items are BLOCKED.

```sh
uv run pytest -q tests/test_conformance_data.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.data import DataChecks
from oep_client.conformance.data_sample import SampleDataAdapter
from oep_client.conformance.data_model import DataModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = DataModel()
    checks = Checks(model.handle, registry, 'virtual-stream-1')
    adapter = SampleDataAdapter(model, functions=(1, 2), observation_ms=50, loss_free=True)
    report = DataChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

Reserve a new JSON with `open('x')` and save the SPEC/checker/model source hashes, date and time, raw requests/responses/notifications, the data stimuli and expected positions, and observation times. Use the SPEC/shared lock items of `.env.example`. The model does not use ADDRESS/FRAMING/TCP_PEER and does not search for private benches or hardware.

The unit tests include literal bytes of u64 position / u16 len, broken notifications, mutants of thresholds/time/seq/resend/old buffer, and restart. For the seq wrap, 65,538 data/events are generated and decoded alternately, and 65535→0 is verified. The internal counter is not seeded, but this is a check with a virtual clock and in-process delivery, told apart from real-clock checks or evidence of seq wrap on the hardware wire.

Response priority, notification transport/disconnect, the send queue limit, seq consumption by drops, physical asynchronous behavior, fns not specified, and restart and seq wrap in real-clock runs are not checked.
