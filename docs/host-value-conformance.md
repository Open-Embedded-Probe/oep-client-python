# Host classification of unknown values

[日本語](host-value-conformance.ja.md)

The failure classification of core §2.4 and the enums that keep their values are checked separately.
The reader is given explicitly, as are the SPEC checkout and the output location. It is not applied to the production Host automatically.

The shapes adopted are the done/status/words of standard debug read_block, the tried/count/entry of wire scan,
the fixed part of core confirm and its transport TLVs, and the cause TLV of rejected unavailable.
An unknown resolution, outcome or reject reason is a failure; the shape of an unknown payload need not be interpreted.
For status, only 0 is success; an unknown status is also a failure. A reader of the fixed shape must not change done or the order of words on its own.
A reader that reports an unknown failure with ValueError is also accepted.

The kind of a cause or a scan entry keeps the received integer value even when unknown.
confirm is a 16-byte fixed part with no flags.
Causes 0x01 to 0x3F belong to the core; 0x40 to 0x7F are used by the interface of the requested fn. Both are kept as integers when unknown.
This is not a rule that a Probe may send unknown values; it is a read check of the Host against future values.
The whole u8 value range is enumerated. For reason, values not defined in the selected registry are used as stimuli.

The decoder API is decode(frame, shape), where shape is debug/scan/confirm.
It matches successful and each fixed field of the returned mapping. For failure values, not using them via ValueError is also accepted.
It checks that cause and scan kind can be passed to the display layer as integers. The actual display in a UI is a separate check.

```python
import json
from pathlib import Path
from oep_client.conformance.host_values import HostValueChecks
from oep_client.conformance.host_values_sample import decode

report = HostValueChecks(decode, spec="/explicit/path/to/oep-spec").run()
Path("/explicit/output/host-values.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

Run it in the uv environment as uv run python example.py.
The unit tests are uv run pytest -q tests/test_conformance_host_values.py.
The report keeps the SPEC commit/dirty/registry SHA, raw frames, read results and checker/sample SHAs.
The user saves the source/version of their own reader separately. The public runner does not search for private benches or neighboring checkouts.

A PASS is limited to the read range of the chosen shapes. It does not prove other status/reason/flags, all TLV widths,
all payloads, the production Host, target execution or transport recovery. full_conformance is always false.
