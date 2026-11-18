# Context, width and critical checks of TLV requests

[日本語](tlv-request-conformance.ja.md)

This checks core §2.2/2.3 with an explicit virtual extension. It uses the Core's real session, resend and refusal paths.
It is separate from OEP-specific operations and production Probe checks, and has no physical pins.

fn 1/2 declare io.github.open-embedded-probe.tlv-request and have op16/17.
The request is a TLV list with no fixed part, and tag 1 is optional. When omitted, each context's default is used.

| fn / op | Shape of tag 1 | default |
|---|---|---|
| 1 / 16 | u8 | 0 |
| 1 / 17 | u16 | 0 |
| 2 / 16 | count(u8), count bytes. count limit 48 | count 0 |
| 2 / 17 | enum u8. 0 is forbidden, 1/2 are defined, 3 and up are undefined. The fixture supports only 1 | 1 |

Tag 3 is a repeating u8, used in wire order. The response is the selected value's length (u16), the selected value, the repeat count (u8) and the repeated values.
The selected value of a counted value includes the count itself. This shape is a contract of the check extension only and does not add operation numbers to the SPEC.
The repeat count limit is 255; exceeding it is unsupported for that tag. The real frame limit also applies separately.

The per-context interpretation and state of the same tag are checked by alternating requests to different fn/ops.
A known tag uses the same shape and value regardless of critical. Unknown optional is ignored; unknown critical is unsupported with the received tag echoed.
Violations of known width/count/forbidden values are malformed; reserved values and values the fixture does not support are unsupported.
The state before and after refusal is the same. The first value of a duplicate and the repeat order are also checked.

```python
import json
from pathlib import Path
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.tlv_requests import TlvRequestChecks
from oep_client.conformance.tlv_request_model import TlvRequestModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = TlvRequestModel(lambda: 0, boot_id=17)
checks = Checks(model.handle, registry, "virtual-resource-1")
report = TlvRequestChecks(checks, model.request_state).run()
report["spec"] = identity
Path("/explicit/output/tlv-requests.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

Run it in the uv environment. The unit tests are uv run pytest -q tests/test_conformance_tlv_requests.py.
The public checker does not search for a private inventory or a neighboring SPEC. It stores raw exchanges and state verdicts.
With your own fixture, also store its schema, state observations and source/version.
A PASS is limited to these 4 contexts. It does not prove all request schemas, concurrency, all core/common/OEP-specific rules or a physical Probe.

That a Host puts critical on important items is a separate responsibility.
[Critical marking in Host requests](host-request-conformance.md) checks the payloads actually built.
Do not infer the conformance of the Host's sending policy from this Probe-side PASS.
