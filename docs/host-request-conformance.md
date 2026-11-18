# Checking critical marking in Host requests

[日本語](host-request-conformance.ja.md)

core §2.3 requires the Host to put critical on TLVs that, if ignored, make the request lose its meaning or cause harm.
Checks that a Probe refuses unknown critical alone cannot prove that a Host forgot to set it.
HostRequestChecks compares the payloads an explicit encoder actually returns with independent expected byte strings.
It provides the checks and an explicit encoder example.

The encoder contract is `encode(interface, operation, fixed: bytes, fields: list[tuple[int, bytes]]) -> bytes`.
The tags in fields are numbers 1 to 127 without critical. The caller builds the fixed part.
The encoder sets bit7 according to the context's policy, and keeps the u16 little-endian length, the values and their order.
Number 0, 128 and above, and values of 65536 bytes or more are refused with ValueError.
The input limits of this API are the adoption contract of the check part, not a request to change the Host API.

## Reviewed contexts

The policy is held by TLV name, and numbers are looked up from the selected SPEC's registry. If a TLV without a policy appears in a registry context, it stops until reviewed.

| Context | TLVs with critical | TLVs without critical | Basis |
|---|---|---|---|
| core open | — | owner | core §6.4 (owner is an optional display feature) |
| rvswd scan | max_speed, idle_clock | — | core §2.3, debug §1, §3 |
| rvswd attach | idle_clock, reset | — | debug §3 |
| swio scan | max_speed | — | core §2.3, debug §1 |
| swio attach | reset | — | debug §3 |
| swd scan | max_speed, targetsel | — | core §2.3, debug §1, §5 |
| swd attach | targetsel, reset | — | debug §3, §5 |
| logic configure / query (2 contexts) | format, carriage, trigger_opts, clock, glitch_filter, input_hysteresis | mode, rate, samples, segments, trigger, pretrigger, block_max | capture §3.3, §3.8 |
| analog configure / query (2 contexts) | format, carriage, trigger_opts | the basic settings above, block_max, frontend | capture §3.3, §3.8 |
| plan_apply | role_assignment | — | core §2.3, plan §2.1 |
| uart configure | format | — | fixture §2, Host policy |

The TLVs in the table of capture §3.3 are implemented by every target probe, so critical is not set on them. Those the table says to "send as critical" (format,
carriage, trigger_opts) and the TLVs of §3.8 (glitch filter, input hysteresis and external clock, all optional) are sent as critical. format does not need
critical at 0, but this policy sets it regardless of the value (the probe treats table TLVs the same even with the bit set, core §2.3).

core §2.3 gives "scan's max_speed" and "attach's reset" as examples to make critical.

For rvswd scan's idle_clock, the body text does not state critical (it writes "critical" only for attach's idle_clock).
If scan's rest setting is ignored, searching with the given line state loses its meaning, so the Host policy makes it critical.
swd scan's targetsel meets the core condition "the request is meaningless if it is ignored".
It is critical to keep the meaning of trying scan's given multidrop target.
attach's targetsel and scan's targetsel are TLVs of different contexts. No policy cache keyed only by number is shared across contexts.
The basic capture settings have a rule that they are all implemented, so the SPEC explicitly says no critical.
owner is optional information for display, so it is non-critical. A policy that sets the bit on everything does not meet this spec.
UART format is a TLV every UART fn implements, but the Host policy makes it critical so that the given communication format is not ignored and dropped to 8N1.

The SHAs of the core/debug/capture/plan/fixture body texts and the registry SHA are pinned to the values at review.
The consistency of numbers, context names and revisions is also checked, and on a change it stops until re-reviewed.
The public checks do not automatically search for private benches or a neighboring SPEC. The SPEC and output location are given explicitly.

```python
import json
from pathlib import Path
from oep_client.conformance.host_requests import HostRequestChecks
from oep_client.conformance.host_request_sample import encode

report = HostRequestChecks(encode, spec="/explicit/path/to/oep-spec").run()
Path("/explicit/output/host-request-policy.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

The checks are critical, noncritical, order/context, copying of values and invalid input.
Each input, expected payload and actual payload is saved. For invalid input, the tag/length and the refusal are saved.
It sends repeated frontend and plan assignments with different values, and sends the same tag1 alternately to wire/core/uart.
Omitting individual items is a check of tail building only, and does not claim success of a complete request that omits required TLVs.
Values of 0/256/65535 bytes are a part check that does not alter values, separate from the meaning/width of each TLV and conformance to transport limits.

The unit tests are `uv run pytest -q tests/test_conformance_host_requests.py`.
They cover the normal case, bit flips of each tag in each context, semantic faults, body text changes, registry/schema changes, the op guard,
detection of wrong policies (a wrong fixed part for attach, uniform critical for logic), and not adopting contexts without definitions.
Semantic faults include uniform critical, uniform non-critical, reordering, deduplication, loss of the fixed part, rewriting values,
endianness/width of the length, omission, breaking input limits, and a cache keyed only by tag.

This PASS is a partial check that proves tail building in the reviewed contexts (full_conformance=false).
Another extension reviews its important items from the extension's meaning and builds a similar oracle.
All request schemas, the choice of features to request / required arguments / defaults,
the Probe's refusal/no change, building all fixed parts, the production encoder and electrical safety on hardware are not proven.
When using an external encoder, also save its source/version, adapter and run environment separately.
The Probe-side rules for unknown/known TLVs are handled by the [request TLV checks](tlv-request-conformance.md),
and response reading by the [core response shape checks](host-core-shapes-conformance.md).

plan assignments are marked critical to avoid a capture that ignored them.

## Checking where the public API sends

`tests/test_conformance_host_critical_calls.py` uses the real Host and the public APIs of UART/rvswd/swio/swd (gpio set has no request TLVs).
attach places TLVs after the fixed part (method, swdio, swclk, max_speed).
UART format, the fixed part and reset of wire attach, rvswd idle_clock and swd targetsel are
compared with independent literal payloads. Each tag is answered as unsupported from a scripted peer, and
it checks that the Host keeps the received critical tag, raises an exception, and does not resend with the item removed.
A fault that drops each tag's critical bit is also detected.

The op numbers of the public API are looked up from the registry. This check matches the header's role/corr/fn/session and the payload, and does not match op numbers.
The whole public API including scan/capture/plan, physical side effects and interaction with real Probes that ignore unknown items are not checked.
