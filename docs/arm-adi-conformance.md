# Independent ARM-ADI checks and the logical fixture

[日本語](arm-adi-conformance.ja.md)

`ArmAdiChecks` is a third-stage check that checks `oep.target.arm-adi` with raw messages,
from debug §2/6 and core §2.3/4/5/7/9 of the selected SPEC.
The general interface checks (second stage) also run separately.
It does not claim conformance of ARM-ADI as a whole.

## Preconditions and setup

It uses `ArmAdiModel` and `SampleArmAdiAdapter` of the public Python package.
It uses no private benches, equipment settings or implicit references to neighboring repositories.
The SPEC checkout is passed explicitly. The SPEC commit and registry SHA, the checker/model SHAs,
the sent/received raw, and the adapter's stimuli and observations are saved in the result.

This logical sample assigns fn 1/2/3 to RVSWD/SWIO/SWD, 4 to console and 5 to ARM-ADI.
This is the sample's choice, not a rule for hardware fn numbers.
Connections, SID and replay use the current core's mechanisms, and no electrical SWD is run.
If the adapter's reset_scope is not logical-model, or the selected registry's op/ACK/status differ,
it refuses in the context and makes the later items blocked. A logical reset is not reused on hardware.

## Conditions checked

| Scope | Check |
|---|---|
| declaration | transfer/read_block/write_block, no features, max_length is the common u16. 16 bytes in the sample |
| empty transfer | done 0/status ok/ack 0/nvals 0, does not touch the target |
| mixed transfer | AP write and DP read, keeps the host's DP SELECT setting, count and order of read values |
| AP reads | Returns the explicit earlier AP read result, the next AP read and DP RDBUFF in raw order. An unrelated DP read does not clear pending |
| ACK and progress | Injects WAIT/FAULT/no response at each failure position. Returns only the reads up to done, and does not run the failed transfer or those after it |
| retry | On recovery from WAIT to OK, the packet repeats but the write runs only once. FAULT/no response are not re-run automatically |
| acceptance | Refuses req reserved bits (unsupported, mark 0 in the fixed part), truncated arguments midway, count, critical/broken TLVs, SID/ownership, resource type and the largest response before execution |
| resend | A request with the same corr returns the same response and does not add target operations |
| block basics | 0 words, the 16-byte limit and exceeding it, alignment, reading bus memory changed from outside, read after write, SELECT/CSW kept and TAR advance |

The count of 3 WAITs is this fixture's choice, not a count OEP fixes for all probes.
The first AP read value is not required to be 0. The fixture states the earlier read result explicitly as a non-zero value,
and checks that the probe does not delete it or reorder it on its own.
DP offset 0 read/write are not treated as the same register (DPIDR/ABORT).

The reference for posted reads is [Arm IHI 0031G](https://documentation-service.arm.com/static/622222b2e6f58973271ebc21),
B2.2.7 (RDBUFF) and the SWD read of B4. OEP's transfer does not hide this delay.
The UNKNOWN value when RDBUFF is read repeatedly is not made a conformance condition as a specific value.

## Running

Run the following commands at the root of the client checkout.
pytest checks the normal model, deliberate violations on the transfer side and the block side, invalid preconditions, and a wrong width of max_length.

```sh
uv run pytest -q tests/test_conformance_arm_adi.py tests/test_conformance_arm_adi_blocks.py
```

An example that saves raw exchanges with a selected SPEC:

```sh
uv run python - /path/to/oep-spec /path/to/arm-report.json <<'PY'
import json, sys
from pathlib import Path
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance.arm_adi_blocks import ArmAdiBlockChecks
from oep_client.conformance.arm_adi_sample import SampleArmAdiAdapter
from oep_client.conformance.interfaces import InterfaceChecks
from oep_client.conformance.arm_adi_model import ArmAdiModel
registry, identity = spec_identity(sys.argv[1])
model = ArmAdiModel()
common = InterfaceChecks(Checks(model.handle, registry, 'virtual-resource-1')).run()
model = ArmAdiModel()
checks = Checks(model.handle, registry, 'virtual-resource-1')
report = ArmAdiBlockChecks(checks, SampleArmAdiAdapter(model)).run()
report['arm_status'] = report['status']
report['status'] = 'passed' if report['arm_status'] == common['status'] == 'passed' else 'failed'
report.update(spec=identity, interface_checks=common, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256())
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed' or common['status'] != 'passed')
PY
```

## Not checked, and limits of the model

The block model runs the packet sequence of TAR write, DRW access and posted read collection.
The fixture's TAR wraps at the low 10 bits. When reading/writing 4 words from 0x23fc, it sets 0x2400 again,
and checks access to the correct bus address. Wrapping at 1 KiB is this fixture's choice.
Auto-increment beyond the low 10 bits depends on the implementation, so the probe does not assume it
([Arm IHI 0031D, C2](https://documentation-service.arm.com/static/5f900abff86e16515cdc063b)).

To stay small, the model sets TAR for each word, and on reads discards the DRW return and collects RDBUFF.
The checker does not prescribe a fixed packet count; it observes bus addresses, posted values, boundary resets, progress and that host settings are kept.
Failures are injected at the TAR/data/buffer stages, and on reads only the prefix of values that could be returned is returned.
On an RDBUFF collection failure, words not yet returnable are not added to done even if they were read from the target bus.
done for write is the number of words sent, and even if the fixture drops delivery to the target, the sent count is returned.
It also checks a single store execution on WAIT, block replay, and invalid access to the failed word / those after it.
Optimized pipeline methods and the policy for all delayed FAULTs are not checked.
Full decoding of AP bank/SELECT, sticky state and ABORT, ADIv6, SWD attach TARGETSEL/tid,
packet parity/turnaround/idle, electrical retries and timing, and the CPU's execution state are needed separately.
The frame capacity of all transports, handling of blocks beyond u32 addresses and integration with the production Host are also not checked.
A PASS of this third stage does not replace checking all core/common norms.
