# Core clock checks and the logical fixture

[日本語](clock-conformance.ja.md)

`ClockChecks` is a first-stage supplementary check of core §2.7/4.1/5.1/6/7.6.
Name the selected SPEC explicitly and run it with `ClockModel` and `SampleClockAdapter` from the public Python package.
No private bench, equipment settings or automatic search of neighboring repositories is needed.

It checks the following.

- The boot-relative u64 ns is kept at 0, the u32 boundary, the floating-point precision boundary, the sign boundary and near the top of u64.
- Repeating clock with the same SID 0/corr still returns the current time.
- The value returned is a real clock read between reception and sending. An estimated value within the interval also fails.
- clock on SID 0 does not change the owner, deadline, lease, corr history, resend cache or resources.
- A resend of clock in the owned session returns the saved response as is, does not rebuild the response from a new reading, and does not renew the deadline.
  On a resend the old time is correct, so the reading check for new requests is not applied. Reading the clock in internal processing is not itself forbidden.
- An observed clock does not extend the lease, and ownership ends at the deadline.
- boot_id 0 and the maximum u32 also match in confirm/open/clock as ordinary values.
- malformed/critical TLVs, a non-zero SID without a session, and another owner's SID are refused.

The fixture provides a controllable exact ns clock, logical times of reception/sending, reading records and the ownership state.
It states delays of 7/13 ns for execution and delivery, but does not require a specific reading position; it matches the interval with the real read records.
Choosing a resolution of 1 ns for the model does not require a 1 ns resolution of hardware.
A new boot rebuilds the time origin. This control is not used as a hardware reset.
The no-change checks of resources use the sample extension of [resource checks](resource-conformance.md).

Run it at the root of the client checkout.

```sh
uv run pytest -q tests/test_conformance_clock.py
```

The checker is also checked with the normal model, fault injection, invalid preconditions and allowed internal readings.
An example that saves the raw exchanges (name the SPEC checkout and a new output location explicitly):

```sh
uv run python - /path/to/oep-spec /path/to/clock-report.json <<'PY'
import json, sys
from pathlib import Path
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance.clock import ClockChecks
from oep_client.conformance.clock_sample import SampleClockAdapter
from oep_client.conformance.clock_model import ClockModel
registry, identity = spec_identity(sys.argv[1])
model = ClockModel()
checks = Checks(model.handle, registry, 'virtual-resource-1')
report = ClockChecks(checks, SampleClockAdapter(model)).run()
report.update(spec=identity, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256())
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

The real clock's accuracy/resolution, the real boundaries of the transport writer, agreement with the timestamps of all extensions,
delays/concurrent writers, all core/common norms and production Host integration are not checked.
A PASS of this check alone does not claim full core compliance.
