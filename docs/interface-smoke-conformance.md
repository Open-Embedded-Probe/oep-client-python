# Interface outline checks used for all extensions

[日本語](interface-smoke-conformance.ja.md)

`InterfaceSmokeChecks` checks selected contracts common to standard OEP and custom extensions.
It uses a core context that carries the reviewed core guard.

```python
from oep_client.conformance import spec_identity
from oep_client.conformance.core_smoke import CoreSmokeChecks
from oep_client.conformance.interface_smoke import InterfaceSmokeChecks

registry, spec = spec_identity(spec_checkout)
core = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec,
                       observe_effects=observe_selected_effects)
report = InterfaceSmokeChecks(core).run()
```

The callback, exclusivity, framing, waiting time, observation and result saving are the same as in the [core outline checks](core-smoke-conformance.md).
There is no processing that searches for or connects to equipment, and no dependence on another project's settings.

It prepares confirm/core describe/list and checks each fn's name, ops, the fixed widths of common tags, the declared pin candidates, the uniqueness of instances within (name, revision), and that repeated describe does not change. Gaps in instance numbers and independence from fn order are allowed. Keeping identifiers across firmware updates is outside the scope of this single-boot check.
For operations not provided, one representative is chosen per fn, and it is checked that it gets unknown_operation without a session.
This covers the contract that refusal of an operation not provided comes before session_required.
With an observation callback, effects before and after the refusal are compared and saved; without one, the report records that they were not observed.

When there is no named feature, the per-fn checks are not applicable, and the result of the preparation case is kept.
When a fn's precondition case fails, further requests stop. Failures or failed preparation are not skipped as missing equipment.
The result records the SHAs of the checker/core/helper/reused declaration checks, the SPEC, requests/responses and observations.

```sh
uv run pytest -q tests/test_conformance_interface_smoke.py
```

The standard OEP required operations, the behavior of provided operations, resources/lifetime at end, all ops not provided, all paging conditions and electrical pin behavior are outside the scope of this check.
A UART without marks or write is not a failure condition of the common check. The UART's own configure/read/status and so on are checked by its own checks.
A custom extension with only one operation can use the same common check.
The report always has full_conformance=false and does not claim the third stage was run.
