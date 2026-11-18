# Pre-checks of standard wire attach and console open

[日本語](attach-conformance.ja.md)

`AttachChecks`, as third-stage `oep-interface`, checks wire attach requests and refusals and the constraints of console open based on the selected SPEC. Compliance of the core and common parts uses other checks alongside.

| Contract | Check |
| --- | --- |
| fixed part | `method(u8), swdio(u16), swclk(u16), max_speed(u32)` (debug §1). A short fixed part and max_speed 0 are malformed. A reset TLV of the wrong length is also malformed. |
| critical | The probe treats a known TLV (reset) the same with or without the bit, and a refusal returns the received raw tag. Unknown critical is unsupported with the raw tag; unknown noncritical is ignored. |
| same tag | For the same tag without the critical bit, the first is used. Later copies are also validated, and a wrong length is malformed. |
| pre-check | A speed below min_clock_hz, a pin set outside the declaration, SWIO swclk != 0xFFFF and an unsupported method are refused with mark 0 in the fixed part. The allocator/resources/buffers/logical target access records before and after refusal are matched. |
| conflict | A pin in use is unavailable cause 1; the wire's connection limit is cause 2. It does not join an existing connection implicitly, and does not close one to make room. |
| attach response | A fixed 13 bytes of connection(u32)/id/flags/speed + TLVs. speed is at most the requested limit. It also reads the connections list (14 bytes with connection u32 + variable tid) and the trailing TLVs, and matches fn/pins/speed/ID against independent state. |
| bring-up failure | completed failed / status line + TLVs. No new live resource is left, and the output of existing connections/streams does not change. |
| console open | connection(u32)/mechanism are the fixed part. Returns only stream(u32) (4 bytes). The streams list has 9-byte rows of `stream, connection(u32), mechanism`. A mechanism outside the declaration / unknown / preset none is unsupported payload 0. |
| console resources | An unknown connection is no_resource; a request with a stream as parent, or an ARM/SWD connection, is unavailable cause 6. |
| mechanism conflict | Mechanisms 0 to 2 have one stream per connection. An open with the same mechanism as the existing stream or a different one is cause 6 either way. |
| multiple targets | Separate streams are created for separate connections, and the creation order, connection and mechanism in streams are matched. A read on SID 0 reads the separate output and does not consume it. |
| resend | A resend of attach/open after detach/close only returns the past ID/response and does not repeat bring-up or resource creation. A re-created stream has a new ID and empty state. |
| session | attach/console open on SID 0 are session_required; attach in another's session is locked. Even with an unknown critical TLV, the session decision comes first. |

`SampleAttachAdapter` states the standard names/fns, the logical pin candidates, the declared subset of mechanisms and the normative references explicitly. `AttachModel` is an independent fixture without a physical target. fn 1 RVSWD, 2 SWIO, 3 SWD, 4 console, 8 logical channels, at most 2 connections per wire, 8 resources overall, min_clock_hz 1000, and the console declares SDI/DMDATA. The condition of refusing undeclared dmseq is also checked.

The logical pin sets are RVSWD (0,1)/(2,3)/(6,7), SWIO (0,0xFFFF)/(2,0xFFFF)/(6,0xFFFF), SWD (4,5). They are stated with channel_group. scan is not implemented. Only the logical success of method 0 is checked, and halt or electrical speed selection is not proven. The behavior of the reset line, idle_clock and targetsel are not checked here (`WireOptionsChecks`). For the reset TLV, only acceptance on the sample's reset channel (8), refusal with the raw tag, and use of the first copy (hold) are checked.

The virtual core passes extension-specific critical TLVs to checking and execution through the explicit extension entry points `request_tags` / `dispatch_request`. An extension without the entry points uses the common processing. The session, header and cache decisions come before the entry points. A resend of the same request does not repeat TLV parsing or execution.

The unit tests detect acceptance of max_speed 0, ignoring unknown critical, refusing known noncritical, loss of the raw tag, preferring the later tag, not validating later copies, target access on refusal, extra bytes in the console response, accepting an ARM console, ignoring mechanism conflicts, exceeding the speed limit, re-execution on resend and invalid metadata in connections. They also check adoption conditions / selected SPEC changes, refusal of fixture restart while owned, and the priority between the extension entry points and session/cache.

```sh
uv run pytest -q tests/test_conformance_attach.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.attach import AttachChecks
from oep_client.conformance.attach_sample import SampleAttachAdapter
from oep_client.conformance.attach_model import AttachModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = AttachModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = AttachChecks(checks, SampleAttachAdapter(model)).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly; it does not depend on or search for private benches. It is not added to the normal CLI automatically.

`full_conformance=false`. Physical bring-up/scratch/speed/pin idle, scan, reset/idle_clock/targetsel behavior, target interface operations, console mailbox/framing/delivery, SDI write with count=0, notifications, other capacities / concurrent requests and production Host integration are not checked.
