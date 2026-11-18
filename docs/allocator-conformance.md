# Checks of the resource number limit and restart

[日本語](allocator-conformance.ja.md)

`AllocatorChecks` is a first-stage supplementary check based on core §2.3/5.1/6/9.
It uses the public `AllocatorModel` and `SampleAllocatorAdapter`, and the SPEC checkout and output location are given explicitly.
It does not search for private benches or neighboring checkouts. The encoding of the resource ops adopts the custom sample of [resource checks](resource-conformance.md);
only the resource number is carried as u32 as core §9 states (the numbers in the create response, close/use requests and the snapshot are u32, limit 0xFFFFFFFF).


- 0xFFFFFFFE/0xFFFFFFFF are assigned to different fn/kinds, and with all slots used, further creates are unavailable with cause 2.
  Existing resources can be used/closed; a kind mismatch is cause 6, and a closed number is no_resource.
- Even when close frees a slot, numbers that have been used up are not reused within the same boot.
- end and the 1000 ms lease boundary release resources. A resend of the old create is the recorded response (the same number) or result_lost and does not revive the resource.
  use in the old session is no_session. In a new session the allocator stays used up.
- Moving to a new session with force does not roll back numbers, and old resources are not carried over (use of an old number is no_resource, a create resend from the old session is locked).
- A resend of a failed create is the same saved response and does not change the allocator/resources.
- A restart changes boot_id, invalidates resources and history, and starts numbers from 1.

The boundary seeds 0xFFFFFFFE/0xFFFFFFFF and the clock are explicit controls of the logical fixture, not hardware ops.
The seed is set only on a fresh/empty allocator. This check does not claim to have made 2^32−1 real allocations.
To tell it apart from normal lack of capacity, it tries at most 2 simultaneous resources against a sample capacity of 8.
A number that failed only at reservation may be left as a gap, and next_id unchanged is not required for a general creation failure.

Run it at the root of the client checkout.

```sh
uv run pytest -q tests/test_conformance_allocator.py tests/test_conformance_resources.py
```

The pytest has the normal model, fault injection, precondition refusal, the case of reading the first value of a duplicate cause, and the u32 boundary.
An example that saves the raw exchanges of the selected SPEC:

```sh
uv run python - /path/to/oep-spec /path/to/allocator-report.json <<'PY'
import json, sys
from pathlib import Path
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance.allocator import AllocatorChecks
from oep_client.conformance.allocator_sample import SampleAllocatorAdapter
from oep_client.conformance.allocator_model import AllocatorModel
registry, identity = spec_identity(sys.argv[1])
model = AllocatorModel()
checks = Checks(model.handle, registry, 'virtual-resource-1')
report = AllocatorChecks(checks, SampleAllocatorAdapter(checks, model)).run()
report.update(spec=identity, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256())
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

All 2^32−1 allocations without a seed, failed reservations, other geometry, concurrent allocators with real resources, and all core/common norms are not checked.
This check alone does not claim the whole resource lifetime or hardware conformance.
