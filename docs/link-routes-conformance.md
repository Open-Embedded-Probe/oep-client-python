# Checking link over several transports of the same Probe

[日本語](link-routes-conformance.ja.md)

LinkRoutesChecks checks core §4.4/5/6, transports §3 and link §2 with an explicit virtual model that has one Core.
Transport 0 is UART bridge with max_frame64, 1 is vendor bulk with 256, 2 is TCP with 1024.
These are logical transports; no real serial, USB or socket is opened.
The GPIO/plan of LinkTransferModel is shared as the protected state, and it checks that transfers do not change the existing plan, high-output pin or ownership.
It does not substitute three single-transport units; it uses the same boot_id, fn list, Core and resend table.

The adopted adapter states transport selection, logical close/reopen, snapshots and the per-transport wire trace explicitly.
The TCP connection generation is saved as epoch. close only closes the delivery path and does not release the session, plan or resend history.
After reopen, confirm and unit_id are checked, and the same boot_id and protected state are required.
This is told apart from proof of real socket/USB closing, discarding old received bytes, or transport framing.

```python
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.link_routes import LinkRoutesChecks
from oep_client.conformance.link_routes_sample import SampleLinkRoutesAdapter
from oep_client.conformance.link_routes_model import LinkRoutesModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = LinkRoutesModel(lambda: 0, boot_id=17)
checks = Checks(lambda raw: model.handle_route(raw, 2), registry, "virtual-resource-1")
report = LinkRoutesChecks(checks, SampleLinkRoutesAdapter(model)).run()
assert report["status"] == "passed"
# Save identity, report and the source/adapter versions to an explicit output location.
```

The second-stage InterfaceChecks are applied to each transport. RegisteredOpsChecks are also included in the specific checker.
The context checks the confirm transport/index/limit of all transports, the same boot_id/unit/fn/instance/revision,
fn0's all-transport declaration, absence of the optional port_speed, and the explicit preconditions for saving.
Based on the spec text, the length/pattern of source and sink are compared through raw exchanges, and snapshots are also saved.

| case | Check |
|---|---|
| ROUTE-LIMITS | Chooses transports alternately, such as small→large→medium, and checks source at 0/1/limit/u32 max and the largest sink on each transport |
| FREE-CACHE | Sends the same raw SID0 request with the same corr to other transports in turn, and gets different responses at each transport's limit. SID0 is not wrongly registered in the shared table |
| SHARED-LOCK | lock_state on all transports shows held. open with another ID is locked, SID0 transfers succeed, and the live plan/pins do not change |
| RECONNECT | Even after a logical close/reopen of TCP, the session/plan/pins are kept, and the holder's new transfers work |
| REPLAY-RECONNECT | Resends a 16-byte source within the saved range with the same bytes after TCP reconnection; the response is kept and not re-run. A change with the same corr is malformed |
| END-SHARED | The session's end frees lock_state on all transports, the plan/outputs return to idle, and SID0 transfers work |

**Host transport rule**: requests of one session ID are sent on one transport.
This fixture also keeps the holder's open, plan, GPIO, link and end all on TCP transport 2.
Transfers on other transports use SID0, and open refusals on other transports use another session ID.
Reconnection also happens on the same logical TCP transport, and it is not an example of moving the holder's requests to another live transport.
The fixed corr of FREE-CACHE is a sequential stimulus that checks the Probe's non-deduplication of SID0; the conformance of the Host's corr allocator is a separate responsibility.

The unit tests are `uv run pytest -q tests/test_conformance_link_routes.py`.
They include the normal case, common declarations on each transport, wire faults, semantic faults, missing control,
and an independent audit of the holder using one transport and of SID0 responses on other transports.
Semantic faults include limit/transport/boot/fn list, global source length, pin change, wrong SID0 caching,
independent per-transport locks, session/cache erased or boot changed by close, plan erased by reopen,
pin change on resend / acceptance of a changed request, and a lock left after end on the peer transport.
For the fault of losing ownership on close, it also asserts that the checker's cleanup does not hide the original violation.

Partial report (full_conformance=false). Window/inflight of truly concurrent multiple transports,
notification destination / revision selection, other resources, port_speed handshake/deadline, production and hardware are not proven.
The public checker does not automatically search for a private inventory or a neighboring SPEC. The observation source and version of an external adapter are saved separately.
This is not proof that all cases of the [single-transport transfer checks](link-transfer-conformance.md) apply to multiple transports.
