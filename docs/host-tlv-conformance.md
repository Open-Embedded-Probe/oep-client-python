# Checks of the Host's TLV reader parts

[日本語](host-tlv-conformance.ja.md)

This checks the Host side of core §2.2/2.3. It is separate from checks of a Probe's sending conformance and is not applied to the production Host automatically.
Connect your own reader with an explicit adapter. There is no automatic search for a private equipment inventory or a neighboring SPEC.

The example payload is `value(u16), blob length(u16), blob, TLV list`.
Tag 1 is an optional u8 and the first value is used; tag 3 is a repeating u8 whose order is kept.
This is an example of an extension's adoption, not a spec that adds new shapes or numbers to real OEP operations.
The decoder returns `fixed` (integer), `blob` (bytes), `first` (integer or None) and `repeated` (list of integers).
A broken payload is refused with ValueError. For a broken known TLV, an implementation that ignores the value without using it is also accepted.

The checks cover unknown tags (including 127), known/unknown tags with the critical bit, the first duplicate value, repeat order,
short fixed parts / counted data / TLVs, tag number 0, the width of known values, and the u16 length boundary.
For the width of known values, a broken value of the same tag is placed after a correct value, and it checks that the later value is also validated and not used
(core §2.3. Implementations that do not use the payload, or that drop the broken value or its tag, are allowed).
Even for an unknown tag, a len past the end of the message is corruption. An implementation that clears the critical bit and reads the tag as known is refused.
This is not a rule that a Probe may send responses with the critical bit.

An example with an explicit SPEC checkout and output location:

```python
import json
from pathlib import Path
from oep_client.conformance.host_tlv import HostTlvChecks
from oep_client.conformance.host_tlv_sample import decode

report = HostTlvChecks(decode, spec="/explicit/path/to/oep-spec").run()
Path("/explicit/output/host-tlv.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

Run it in the uv environment as `uv run python example.py`. The unit tests of the parts are
`uv run pytest -q tests/test_conformance_host_tlv.py`.
The unit tests inject a SPEC identity, and the usage example above records the real checkout's commit, dirty and registry SHA.
The run report stores raw payloads, read results, verdicts and checker/sample source SHAs.
The user records the source and version of their own decoder separately.

The sample of length 65535 is a lexical/payload check that does not go through a transport, and it does not prove the payload fits within the frame limit.
Other fixed shapes and TLV definitions, response corr, notification demux, unknown kinds and seq, deadline handling during continuous notifications and
the whole production reader are not included in this PASS. The report's full_conformance is always false.

The real 8 operations of core revision 1, refusal payloads and known TLVs are checked separately in the [core shape checks](host-core-shapes-conformance.md).
