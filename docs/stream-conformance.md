# read/marks checks of positioned streams with explicit adoption

[日本語](stream-conformance.ja.md)

Common parts apply only when the spec of each interface states that it adopts them. They are not inferred automatically from names or op numbers.

| Feature | Application |
| --- | --- |
| oep.fixture.uart | Adopts common §1. Addressed directly by fn; position/mark serial are not rolled back within a boot. |
| oep.target.console | Adopts common §1. It addresses stream resources, so resource creation/release is also checked separately. |
| oep.fixture.logic / analog | The segmented, generation-tagged format of the capture document. This read/marks check does not apply. |
| custom extensions | Applies only when the extension's spec and adapter state the adoption of common §1, the addressing and the op mapping explicitly. |

This adapter states explicitly for a custom persistent-fn sample the mapping of read/marks/clear/mark/write, the normative references, byte ring 64 and mark ring 5. Declaring the 5 ops is required, and one without write does not conform just by successful reads. Behavioral conformance of write is not included in this check.

| case | Check |
| --- | --- |
| EMPTY | Empty read for all from values, an empty mark ring. |
| POSITION | u64 positions beyond 32 bits, max=0 / small max, empty response for a future position, reading from now. |
| FRAME | Splitting up to the frame limit, more, restoring all bytes, non-consumption by repeated reads. |
| GAP | Byte ring overflow, the lost position difference, oldest/now, lost mark. |
| SELECTORS | The last mark of a given kind / any kind, a nonexistent kind, bytes kept after a target reset event. |
| PAGES | Inclusive serial selection, 22-byte fixed elements, more, several marks at the same position, empty response for next. |
| WRAP | Sets the initial serial of a new boot to 0xFFFFFFFE, actually generates marks, and checks paging across 0. |
| EVICTION | Evicted/unknown/future serials go to the oldest, a kept serial is inclusive, next is empty. |
| EVICTION-WRAP | Generates 8 marks from 0xFFFFFFFC and checks wrap and eviction at once. The kept span and detail order are matched independently. |
| MOVING-PAGES | Reads pages from 0xFFFFFFFE and adds 3 marks before the next read. Even when the cursor is evicted, it resumes from oldest, detects the missing 1 mark, and reads to the end without duplicates. The position is 2^40+19. |
| MARK-EVICTED | When the mark of the given kind has left the ring, go to now. The bytes are kept. |
| MARK-GAP | When only the bytes at the mark position are lost, go to the oldest bytes with gap. |
| LIMIT | Reads positions just below the u64 limit, with no wrap. |
| CLEAR | Erases only bytes and keeps the position, clear mark, no duplicate mark on a resend of the same request. |
| PERSISTENT | detail and resend of host marks, position/serial/bytes kept across end / a new session. |
| REFUSALS | clear/mark/write require the lock. Short read/marks, unknown from, no change after refusal. |

read/marks are used on SID 0, and the independent stream state is matched before and after each response to check non-consumption. Unknown response TLVs are processed after the fixed part / data / mark array. A mark's serial/kind/position/detail are matched with the known stimuli, and time_ns is checked with the interval of wire clocks around the stimulus and monotonicity.

The sample contract is fn 1, read 16 (from u8/arg u64/max u16), marks 17 (from_serial u32), clear 18, mark 19 (value u8), write 20 (count u16/data). Only read/marks need no lock. write accepts all input into the queue in this logical model, but does not prove physical delivery, send capacity or partial/failed.

`StreamModel` is an independent logical fixture. External bytes and mark stimuli are given through the adapter. Initialization of boundary values is recorded each time as an explicit logical restart into a new boot. The position/serial of the same boot are not rolled back to create boundaries. The serial wrap is a condition that puts the initial value just before the boundary; it does not mean 2^32 stimuli or a hardware restart were done. The reset event is a mark stimulus, not a real target reset.

The unit tests detect consuming read, gap/more/future position, exclusive marks, numeric sort across wrap, serial reset at end, position reset on clear, bytes erased on reset, and invalid time/kind/serial. They also check enum changes in the selected SPEC, missing adoption conditions / addressing / geometry / reset scope, a missing write declaration, fixture reset while owned, and invalid seeds. Faults on arrivals between pages detect empty responses / re-presenting old pages, numeric sort after additions, truncation of kind/position, exceeding capacity, skipped serials and consumption of bytes by marks.

MOVING-PAGES is a producer stimulus with a fixed order between requests. It does not claim to verify simultaneous threads during request processing.
The kept span of 5 marks and the next serial is less than 1/4 of u32, and this is not extended to proof for any ring capacity.

```sh
uv run pytest -q tests/test_conformance_stream.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.stream import StreamChecks
from oep_client.conformance.stream_sample import SampleStreamAdapter
from oep_client.conformance.stream_model import StreamModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = StreamModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = StreamChecks(checks, SampleStreamAdapter(model)).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. It does not depend on or search for private benches. It is not added to the normal CLI automatically.

`full_conformance=false`. The lifetime of resource-addressed streams, delivery/partial/failed of write, notification data, physical UART / target reset, the debug status/done parts, other ring/frame conditions and simultaneous producers during requests are not checked.
