# Checking the required link source/sink

[日本語](link-transfer-conformance.ja.md)

LinkTransferChecks checks source/sink of the selected `oep.probe.link` revision1 with raw requests/responses and the state of protected targets.
It covers the data pattern of link §2, lengths from the transport's max_frame, arbitrary sink data, TLVs, no change on refusal, no lock needed, and resend.
It explicitly adopts a conforming configuration without port_speed, and checks that a port_speed request gets unknown_operation.
The handshake, switch after response and verify/idle/session deadlines of a configuration with port_speed need separate checks.

## Virtual model and observation

LinkTransferModel combines the current Core and the GPIO/plan model.
fn1 is plan, fn2/3 are GPIO, fn4 is link. The transport max_frame has 3 adopted configurations: 64, 256 and 1024.
Each check does an explicit logical restart and reads the new boot_id back with confirm.
It plans channel1 to GPIO fn2 role1 and drives it high. The plan, pin/idle, driving journal and the core's ownership are
snapshotted from the real model, and they are required to match before and after transfers/refusals/resends.
The lease renewal time and the resend cache are legitimate changes of the core, so they are not subject to this invariant.
The seed and the snapshots before and after requests are saved in the report's state_observations.
It also checks that the seed actually created the plan, output and ownership; an empty snapshot alone does not pass.

The common interface declarations are checked first with InterfaceChecks, and the required and optional ops with RegisteredOpsChecks.
For source/sink success and refusal, the common Checks check resolution/detail/corr, then the specific payload is read.
These PASSes do not prove all core norms. The public checker does not search for private benches or a neighboring SPEC.

```python
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.interfaces import InterfaceChecks
from oep_client.conformance.link_transfers import LinkTransferChecks
from oep_client.conformance.link_transfer_sample import SampleLinkTransferAdapter
from oep_client.conformance.link_transfer_model import LinkTransferModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = LinkTransferModel(lambda: 0, boot_id=17, max_frame=1024)
checks = Checks(model.handle, registry, "virtual-resource-1")
common = InterfaceChecks(checks).run()
specific = LinkTransferChecks(checks, SampleLinkTransferAdapter(model)).run()
assert common["status"] == specific["status"] == "passed"
# Give the output location explicitly, and save identity, report, checks.trace and the source/adapter versions.
```

## Check items

source requests length 0/1, max_frame−8/−7/−6 and the u32 maximum.
The response len is min(length,max_frame−7), byte k is k&255, and the whole response is within the transport's max_frame.
In the 1024 configuration, the repetition of the pattern from 255 to 0 is also checked.
Only after source's fixed value and after sink's count byte are the response/request TLV boundaries.

sink sends arbitrary data of all 0, all 255 and a counter with count 0/1 and max_frame−13/−12.
The whole request fits in max_frame, and the response is a success with no fixed part (following TLVs are allowed).
A short fixed part / insufficient count, ignoring unknown optional, echo of the received tag for unknown critical,
number 0, and a truncated TLV that is not known are also matched against the core's rules and the state before and after refusal.

While the lock is held, transfers work on SID0 and also with the holder's SID. Another SID gets locked, and the old SID after end gets no_session.
After end, source/sink on SID0 still work. For resends, it checks byte equality of the same request, and malformed and no change for a changed request with the same corr.
The resend stimuli are source 16 bytes and sink 3 bytes. The fixture saves request/result up to 72 bytes.
The adapter returns the real saving limit, and if it is less than 23 bytes, it stops for missing adoption preconditions.
A resend of a large response is not assumed to succeed. result_lost for a response that cannot be saved is a legitimate refusal of the core.

The unit tests are `uv run pytest -q tests/test_conformance_link_transfers.py`.
They include the normal case, common declarations, wire faults, semantic faults, missing seed, missing control and missing saving preconditions.
Semantic faults detect length/limit/pattern/empty data, pin/plan change, wrong acceptance of a short fixed part/count,
ignoring unknown critical / broken TLVs, loss of the tag mark, acceptance of a wrong SID, change of the resend payload, wrong acceptance of a changed request,
and acceptance of undeclared port_speed. The third-stage report of each profile has full_conformance=false.

Physical transfer amounts/delays, the max_frame of each of several transports, transport framing, port_speed,
true concurrency and invariance of resources other than GPIO/plan, and production Host/Probe conformance are not proven.
The origin and version of the state an external adapter returns are recorded separately. A constant snapshot is not evidence of the implementation state.
