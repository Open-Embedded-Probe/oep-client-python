# Core outline checks

[日本語](core-smoke-conformance.ja.md)

`CoreSmokeChecks` is an API that checks selected core contracts with small raw message checks.
It checks the core SHA and the header/corr definitions of the selected SPEC, and stops before sending on body text it has not reviewed.
How the pinning works is in [Conformance checks and SPEC versions](conformance-spec-compatibility.md).

```python
from oep_client.conformance import spec_identity
from oep_client.conformance.core_smoke import CoreSmokeChecks

registry, spec = spec_identity(spec_checkout)
checks = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec,
                         observe_effects=observe_selected_effects)
report = checks.run()
```

`send_message` is a callback that sends OEP message bytes and returns the response message bytes.
The caller provides connection exclusivity, transport framing, waiting time, log saving and the choice of equipment.
The API itself does not search for, connect to or flash devices, and does not refer to other projects' settings locations.

Five cases check confirm, core describe/list, clock, refusal of corr0, and the session's wrap and end.
The confirm response is read as a fixed part without flags (revision, max_frame, window, max_inflight, boot_id).
fn 0 confirm/list/describe/clock/lock_state are not refused even by an optional policy decision (unavailable cause 7), so anything other than success is a failure.
For a probe that refuses open/keepalive/end by policy, the caller prepares an allowed transport and peer before running.
It opens at 65534, continues keepalive with the same SID through 65535→1, and checks a resend with the same corr, end and its resend, and refusal of open for an ended SID.
open carries a non-critical owner, but echoing it for display is not a success condition.
Both a kept identical result and result_lost are allowed, and cache0 is not non-conforming.
list reads 6-byte entries without flags. There may be no named features; if there are, the shape of the selected list is checked.

`observe_effects` is a callback that returns a JSON-compatible state that independently observes only the effects of interest.
It compares execution counts, lease renewal counts and so on before and after a resend. Clocks or background counters that increase for unrelated reasons are not mixed into the same snapshot.
Without an observation callback, comparing responses alone cannot prove the prevention of double execution, so it stays in the report's unchecked.
A unit test that observes the snapshot of a test endpoint and independent observation of hardware are different evidence.

After a precondition case fails, no further requests are sent, and later cases are recorded as not_run.
A SID that could not be ended midway stays in unfinished_session, and a SID whose open was tried stays in observed.attempted_session.
On a transfer failure it may be unknown whether open ran, so the caller checks the transport and state and recovers.
This check does not take other sessions with force. If the target is in use, coordinate before running.

```sh
uv run pytest -q tests/test_conformance_core_smoke.py
```

It is not full core compliance. CRC/COBS/USB/TCP faults, all time conditions of the lease, fn 0's short processing limit (core_quick_op_max_ms), policy decisions, force, all rules when owner display is provided, resource lifetime, the Host's half-cycle limit / flushing of old responses, and individual interface behavior are separate checks.
The report always has full_conformance=false, and scope and unchecked are handled together with the results.
