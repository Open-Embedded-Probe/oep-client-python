# Conformance checks of resource lifetime

[日本語](resource-conformance.ja.md)

This checks SPEC core §4.2/5.1/6/9 separately from concrete resource ops. The public tests do not search for equipment or private benches. Pass an adapter fitted to the implementation, and an explicit unit, SPEC and communication entry point. A PASS of the normal core checks alone is not a PASS of resource lifetime.

`ResourceChecks(checks, adapter).run()` returns a supplementary report that includes the resource contracts and the interface declaration checks of the target fns. It first matches confirm, the unit, the core declarations and the interface declarations, and on failure stops resource operations. The environment it is applied to is isolated with a shared lock that no other user operates. Creating resources on hardware means operations that depend on connections and wiring, so fix the adapter and the allowed scope of the equipment first. The code does not fix a particular target or pins.

The adapter provides the following. It handles encoding and validating the response format, and the common checks decide pass/fail for no reuse, release and resend.

- `slots`: the `(fn, kind)` pairs. State explicitly all pairs of the target resources, including another fn and another kind on the same fn.
- `request(action, sid, slot, rid=0)`: returns the request bytes for create / close / use. corr is numbered by the Checks passed in. These choose operations that require a session.
- `decode_created(payload)`: validates a successful create response and returns the u16 resource ID.
- `decode_empty(payload)`: validates the success response of close / use by that interface's spec. The name comes from the sample's empty response, and it may also validate fixed fields of a real interface.
- `snapshot()`: returns the set of live IDs of all slots through an independent observation that does not push out the resend history, such as an observation on session 0. It does not hide duplicates by turning them into a set, and also detects unknown kinds and missing entries.
- `capacity`: the number of simultaneous resources reproducible on the test equipment (all slots can be held at once, limit 32). It creates up to the limit and checks that the next create is unavailable cause 2, that no partial resource is left, and that existing resources can be used. For an implementation that cannot create this condition, this whole check set is not applied as is. It is not just skipped; it stays in the plan as an unchecked capability.

The checks cover session required, monotonically increasing IDs shared by all fns/kinds, closed/nonexistent IDs, cause 6 for another fn/kind, create/close resends, observation of side effects, a new open with the same ID, open resend, end, natural expiry, force with another ID and lack of capacity. Only sessions it took itself are operated. After natural expiry it does not send end, and after force it cleans up the new session. A create resend after the end may return the past ID, but the resource must not come back.

A missing adapter is not a PASS. This entry point is an optional supplementary API, saved separately from the normal CLI report. The report always has `full_conformance=false`. Subscriptions/notifications, route disconnect, dependency order, electrical idle, restart, 65535 real allocations and fns/kinds not specified are stated as not checked. Results with a virtual clock that replaces `wait` and with the real clock are also told apart.

## Minimal sample extension

`oep_client.conformance.resource_model.ResourceModel` is a sample that runs in-process. It is not hardware or a USB stack. It passes an explicit extension hook to the `core-v1` core. The sample name is `io.github.open-embedded-probe.resource`, revision 1, fn 1 and 2, instances 0 and 1. It is not an OEP standard interface and is not registered in the normative registry.

| op | Request fixed part | Success response | session |
|---|---|---|---|
| 16 create | kind:u8 (1 or 2) | resource:u16 | required |
| 17 close | kind:u8, resource:u16 | empty | required |
| 18 use | kind:u8, resource:u16 | empty | required |
| 19 snapshot | empty | count:u8, count pairs of (resource:u16, kind:u8) | 0 allowed |

Integers are little endian. Following TLVs in requests are validated by the current core's rules. snapshot returns all live resources of the requested fn. 8 simultaneous resources across all fns/kinds. The 9th, or running out of numbers, is unavailable cause 2. Except for snapshot, a kind other than 1/2 is malformed. Except for create, a nonexistent ID is no_resource, and another fn/kind is unavailable cause 6. No subscription is declared.

`oep_client.conformance.resource_sample.SampleResourceAdapter` encodes the table above independently on the checking side. It does not import the probe implementation. A usage example is `tests/test_conformance_resources.py`. Combine it with Checks loaded with the explicit SPEC's registry, and specify functions=(1, 2), kinds=(1, 2), capacity=8. The boot at model start comes from OS randomness; only tests pass a fixed value.

The model's own tests also check the boundaries of restart and the ID limit. The stimulus that advances the internal counter to 65535 is a boundary check of the model, not evidence that 65535 were created on hardware.

The minimal real-clock run uses the following API from the public Python environment or the Arduino uv environment. The SPEC and lock paths are passed explicitly from the environment settings (`OEP_CONFORMANCE_SPEC`, `OEP_HW_LOCK`). It does not open hardware and creates resources only inside the model.

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.resources import ResourceChecks
from oep_client.conformance.resource_sample import SampleResourceAdapter
from oep_client.conformance.resource_model import ResourceModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = ResourceModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    adapter = SampleResourceAdapter(checks, functions=(1, 2), kinds=(1, 2), capacity=8)
    report = ResourceChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

When saving, reserve a new file with `open('x')`, and keep the SPEC commit/dirty/hash, the checker source hash, the model source hash and the run date and time together with the raw exchanges. Keep the output location separate from that of the normal CLI/USB model. The model does not use the ADDRESS/FRAMING settings, and no hardware adapter is chosen automatically.
