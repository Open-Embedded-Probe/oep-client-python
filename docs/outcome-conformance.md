# Checks of write acceptance and debug completion results with explicit adoption

[日本語](outcome-conformance.ja.md)

This applies only to interfaces that adopt common §1.4 write and §3 debug status/done. It does not infer from names or op numbers. Declaration checks of all fns, resource lifetime and the behavior of OEP-specific ops are needed separately.

| Contract | Check |
| --- | --- |
| write | count=0, success when all accepted, partial when only a prefix is accepted, failed when 0 accepted. Even when full, count=0 is success. accepted is at most count, and only the first accepted bytes of the input are added to the queue. |
| queue | Recovery of capacity after an explicit logical drain, and that the unaccepted part does not enter the queue. Acceptance does not mean delivery completion. |
| debug | status 0=ok, 1=wait, 2=line, 3=fault, 4=timeout, 5=state. success for full progress, failed for 0 progress, partial for partial progress. An accepted failure is completed, not rejected. |
| arrays | Only the results that correspond to done are read, and unknown TLVs after them are processed. done greater than the input count and missing results are refused. |
| ops without done | The success payload and completed failed / status u8 + TLVs on failure are told apart by op/outcome. |
| resend | Even when the queue's space or failure conditions change, the same request returns the saved response bytes, and writes/progress are not duplicated. |
| refusal | malformed and session_required on SID 0 do not change the queue/progress. |

The custom sample contract is fn 1 write op 20 (count u16 + data, response accepted u16 + TLVs), fn 2 batch op 16 (count u16, response done u16 / status u8 / done u32s + TLVs) and single op 17 (empty request, success u32 + TLVs, failure status u8 + TLVs). Queue capacity 4 bytes, batch limit 8, all ops require the lock. batch count=0 is defined as success in this sample. single is an example of the common failure shape, not an implementation of standard scan/attach/detach.

`SampleOutcomeAdapter` states explicitly the adoption of both parts, the fn/op mapping, the capacity, the limit and the normative references. `OutcomeModel` is an independent logical fixture that records the setting of failure conditions, queue drains and state observations through the adapter. Each behavior starts from an explicit logical restart into a new boot. These are not observations of physical UART delivery or target progress.

The receive-side helper `decode_debug_counted` reads in the same shape according to done even for an unknown status. `debug_succeeded` counts as success only when the known success outcome / ok status / all done hold. An unknown status or unknown outcome is not success. This is the check sample's classification; it is not integrated into the production Host and does not resend unaccepted parts automatically.

The unit tests detect a wrong outcome, acceptance other than a prefix, accepted too large, an accepted debug failure turned into rejected, done too large, missing results, a wrong shape for a single failure, and cache re-execution. They also check the failure classification of unknown status/outcome, missing adoption conditions and status changes in the selected SPEC. The virtual core's minimal extension `Completed` saves failed/partial into the response and the cache. It is also checked independently that a new failed/partial completion renews the lease and a cache resend does not.

```sh
uv run pytest -q tests/test_conformance_outcomes.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.outcomes import OutcomeChecks
from oep_client.conformance.outcome_sample import SampleOutcomeAdapter
from oep_client.conformance.outcome_model import OutcomeModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = OutcomeModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = OutcomeChecks(checks, SampleOutcomeAdapter(model)).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly. The public checks have no dependence on or search for private benches. They are not added to the normal CLI automatically.

`full_conformance=false`. Physical delivery / target progress, the meaning of custom statuses, release of debug connection resources, production Host integration, other capacities / result formats, concurrent writers and OEP-specific ops are not checked.
