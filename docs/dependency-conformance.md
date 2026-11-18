# Lifetime checks of resource-addressed streams and debug connections

[日本語](dependency-conformance.ja.md)

This applies to extensions that adopt the positioned stream of common §1 and the debug connection of §2 and explicitly state that a stream depends on a connection. Declaration checks of all fns and checks specific to standard OEP ops are done separately.

| Contract | Check |
| --- | --- |
| explicit addressing | Creates 2 connections and separate streams, and reads them apart by the resource ID in the request. read/marks work on SID 0 and do not consume. The last created target is not used implicitly. |
| global ID | Connections and streams share one ID space. 0 is not assigned, and closed numbers are not reused within the same boot. |
| clear | Erases only the read buffer, keeps the position and adds a clear mark (common 2). Does not erase the send queue. |
| stream close | Releases the stream and its buffer/queue, and keeps the parent connection. Re-creation gets a new ID, position/serial 0, empty buffer/marks/queue. |
| connection close | Releases all dependent streams first. Other connections and their streams do not change. |
| refusal | Released/unknown IDs are no_resource, another kind is unavailable cause 6. malformed, a foreign session and state-changing operations on SID 0 also change nothing. |
| resend | Resends of stream open/close/read, connection detach and host mark are the saved results. They do not revive a closed stream or close a new stream. |
| session | open with the same ID keeps resources. end / lease expiry / force with another ID release in dependency order and do not carry over to the new session. A read resend after end/expiry is past bytes and does not mean the resource exists. |
| target restart | An explicit logical restart stimulus does not close the connection; it keeps the stream's output/position/queue and adds the console's restart mark (0x41/havereset 1). |

`SampleDependencyAdapter` states explicitly the adoption of both parts, resource addressing, the fn/op mapping, a capacity of 8 resources and a 4-byte send queue, and the normative references. `DependencyModel` is an independent logical fixture. Each case starts from an explicit logical restart into a new boot. The release events are independent state observations, and they check the order in which the stream is released before its parent. This is no guarantee of physical memory erasure or electrical idle. The lease is checked with a real-clock wait or a virtual clock for unit tests.

The sample contract is as follows. TLVs may follow at the end of every response. The names belong to a custom extension, not a compatible implementation of the standard console/wire.

| fn | op | Request fixed part | Success response fixed part | lock |
| --- | --- | --- | --- | --- |
| 1 connection | 16 attach | empty | connection u32 | required |
| 1 | 17 detach / 18 use | connection u32 | empty | required |
| 1 | 19 list | first u16 | more u8/count u8/count connection u32s | not needed |
| 2 stream | 16 open | connection u32 | stream u32 | required |
| 2 | 17 read | stream u32/from u8/arg u64/max u16 | common §1.2 | not needed |
| 2 | 18 marks | stream u32/from_serial u32 | common §1.3 | not needed |
| 2 | 19 clear / 22 close | stream u32 | empty | required |
| 2 | 20 mark | stream u32/value u8 | empty | required |
| 2 | 21 write | stream u32/count u16/data | accepted u16 | required |
| 2 | 23 list | first u16 | more u8/count u8/count (stream u32/connection u32) | not needed |

Resource numbers are u32 and are not reused while the boot_id is the same (core §9, common parts §1/§2).

The lists are paged in creation order. It is a sample that allows several independent streams per parent; it does not implement the constraint that console mechanisms 0 to 2 have one stream per connection. Pin assignment and target selection are not included in this sample. The break stimulus is an explicit connection release by the logical fixture, not a check of broken-wire judgment or of the order "send the failure response, then close".

The unit tests detect release of the parent first, orphan streams, parent release by stream close, queue erasure by clear, output erasure by target restart, ID reuse, an implicit last stream, consuming read, resources kept after end, a wrong resource in marks, re-running detach, a missing required op declaration and invalid TLVs in empty responses. They also check missing adoption conditions, changed mark values in the selected SPEC, refusal of logical restart while owned, and the stage/unchecked scope of the results.

```sh
uv run pytest -q tests/test_conformance_dependencies.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.dependencies import DependencyChecks
from oep_client.conformance.dependency_sample import SampleDependencyAdapter
from oep_client.conformance.dependency_model import DependencyModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = DependencyModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = DependencyChecks(checks, SampleDependencyAdapter(model)).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. It does not depend on or search for private benches. It is not added to the normal CLI automatically.

`full_conformance=false`. Physical release / pin idle, broken-wire judgment / close after the response, standard mechanism constraints, target debug operations, delivery of notification data, production Host integration and other capacities / concurrent writers are not checked.
