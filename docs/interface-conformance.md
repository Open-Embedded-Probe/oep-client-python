# Compliance checks of declarations used for all interfaces

[日本語](interface-conformance.ja.md)

`InterfaceChecks` applies the same checks to standard and custom interfaces. It does not refer to expected values of OEP-specific ops or to private equipment. It takes only the explicit SPEC registry and wire exchanges, and reports results in the `interface` stage.

Besides the preconditions, the same checks run on each fn. The listing order is 9/2/5; fn 2/9 use the same name and revision 1, and fn 5 the same name and revision 2.

| case | Norm / observation |
| --- | --- |
| NAME | core §12 rule 1. Two or more non-empty labels. Leading/trailing hyphens are forbidden. The character set and 1 to 48 bytes are checked by the list check. |
| OPS | §1.2/2.5/7.4/10.3. ops required, the bitmap limit, common reserved numbers forbidden, subscribe/unsubscribe as a pair. |
| FIXED | §2.3/7.4. max/min_clock_hz and features are u32, max_length is u16. Value ranges and meanings are not guessed. |
| CHANNELS | §7.4/7.5. The header of role_channels and the union of repeats, count/fixed elements of channel_group, the range of the declared channel count. |
| PAGES | §7.3. The response from each TLV position is matched against the suffix of the whole. more, an empty tail, 65535, and TLVs fitting in the frame. |
| STABLE | §7.2/7.3. list does not change within the same boot; describe does not change without a session, while held, or after end. |
| ABSENT-OPS | §1.2/4.3. Requests for 0 to 255 not in ops are sent on SID 0 and are refused with unknown_operation before session_required and the like. |
| INSTANCE | §7.2. Instances do not overlap within the same (name, revision). Starting at 0, consecutive numbers and fn order are not required. Separate from other revisions. |

When reading the meaning of common tags, the first value of a non-repeating tag is used. Unknown non-critical tags are kept without interpretation and are included in the page position computation. role_channels checks only the channels of set bits. Undefined role values, the ordering of clocks, the meaning of group numbers and the required set of standard ops are not guessed by the general checks.

[Common parts](../../oep-spec/interfaces/oep-if-common.md) such as positioned streams, debug connections and status are applied only when the interface states that it adopts the part. read/marks/attach are not required of all fns. The lifetime of resources, subscriptions and notifications is checked together with explicit adapter checks, and the side effects, wiring and electrical observations of specific ops are checked in the third stage.

The unit tests detect names, fixed widths, channel range/count, ops, pages broken only at a given position, declarations that change only while held, the refusal priority of undeclared ops, invalid instances and missing ops. They also check 48-byte names, standard names, custom names, unknown tags, repeating tags and different revisions. On an endpoint without fns, each fn check is recorded as not applicable, and it is not taken as full compliance.

```sh
uv run pytest -q tests/test_conformance_interfaces.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.interfaces import InterfaceChecks
from oep_client.conformance.declaration_model import DeclarationModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = DeclarationModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = InterfaceChecks(checks).run()
assert report['status'] == 'passed', report
```

This is an example with an explicit virtual sample. To apply it to hardware, give the wire exchange, unit, SPEC and shared lock explicitly. It is not added to the normal CLI automatically, and no new settings are added to `.env.example`. The public side does not search for private benches or neighboring checkouts.

`full_conformance=false`. Execution of declared ops, the required ops of standard interfaces, the behavior of common parts, resources/subscriptions/notifications, effective constraints of pin selection, invariance across firmware versions, the minimum frame of other transports and concurrent writers are not checked. The custom model is a declaration fixture, not a probe that implements free channel state or pin operations.

Besides the common character/label rules, a name starting with `oep.` requires the probe/wire/target/fixture layer and a following name.
Legal unregistered names and other reverse-DNS names can also be checked. Definition authority, reverse-DNS ownership and the intent of names are the scope of a separate design review.
