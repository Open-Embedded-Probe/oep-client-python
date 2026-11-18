# Outline checks of lease and force

[日本語](session-smoke-conformance.ja.md)

`SessionSmokeChecks` checks the selected session lifetime with raw requests.
It uses as its context a `CoreSmokeChecks` that carries the reviewed core guard.

```sh
uv run pytest -q tests/test_conformance_session_smoke.py
```

```python
from oep_client.conformance import spec_identity
from oep_client.conformance.core_smoke import CoreSmokeChecks
from oep_client.conformance.session_smoke import SessionSmokeChecks

registry, spec = spec_identity(spec_checkout)
core = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec,
                       observe_effects=observe_selected_effects)
report = SessionSmokeChecks(core, wait_ms=wait_or_advance_ms,
                           prepare_resource=create_selected_resource,
                           resources_released=selected_resources_are_released).run()
```

send/exclusivity/framing/waiting time/observation are the same as in the [core outline checks](core-smoke-conformance.md).
This API does not search for equipment, connect, flash, or read other settings locations.
Run it on a test endpoint the user has allowed it to occupy. The FORCE case moves ownership from a SID this check itself created to another SID.
Locks taken by other Hosts are not forced by this check. If the first open is locked, the precondition case fails and later cases stop.

It checks SESSION-SMOKE-CONTEXT (preparation), EXPIRY (resend and expiry), REJECTED-RENEWAL (deadline renewal by a refusal after the session decision) and FORCE.
The lease is 1000 ms. After 700 ms, a resend / SID0 / refusal of another SID are inserted, and after 400 ms more, expiry is checked.
A resend of the original keepalive after expiry may be the past result or result_lost, and it also checks that the lock does not come back.
In another case, an invalid keepalive from the owner is sent after 700 ms, and it checks that ownership continues 700 ms after that.
After force, requests with the old SID get locked, and finally end of the new SID is checked.

`wait_ms(ms)` is the caller's adapter that advances the target's clock by the given amount or waits that long.
A logical fixture with controlled time and real-time tests are recorded as different evidence.
Real time needs an environment where the requests and observations in between complete within the remaining lease; in an environment where processing delay cannot be controlled, conformance for these short timing conditions is not judged.
The report's wait is the requested ms, not a real-clock measurement. The accuracy of the clock, scheduler and transport is not treated as checked.

`prepare_resource(call)` is an adapter that creates a representative resource with the check's owned SID.
`call(operation, payload=b'', fn=0)` sends with the next corr and returns the raw response. A core operation can be passed by name, a specific operation as an integer.
The adapter chooses the needed fn/pins/operations explicitly and verifies the results. It does not advance SID/corr on its own; it uses this call.
`resources_released()` is a predicate that independently checks that the target resource is gone; it is required to be False right after prepare and True after expiry/end/force.
If both are omitted it can run core-only, and resource release stays in unchecked.

The core's observation callback includes only execution counts, deadline renewals, selected resources and so on before and after a resend. A clock that increases with the wait is not included in the snapshot comparison.
Without observation, there are only the selection checks by response and deadline, and the effects of resends/old SIDs remain unobserved.
After a failure, later cases are not_run, and the SIDs that could not be ended and the SIDs whose open was tried are reported. No automatic force/cleanup is done.

Deadlines during long operations, force and queues during execution, real timers, all lease values / owner display, and transport faults are outside the scope of this check.
The report always has full_conformance=false. A PASS on a raw test fixture is not to be read as conformance of the production virtual bench or hardware.
