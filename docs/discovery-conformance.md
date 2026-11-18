# Cursor and channel name checks of core discovery

[日本語](discovery-conformance.ja.md)

`DiscoveryChecks` is a first-stage supplementary check based on core §2.1/7.1–7.5.
Besides normal sequential paging, it checks cursors that point to an entry in the middle of a list page and to a TLV in the middle of an fn 0 describe page.
It does not refer to private benches; the SPEC checkout and output location are given explicitly.

Passing an explicit `supported_revisions=(1,1)` profile adds the revision range checks.

- list's first is matched at 0 to total, total+1 (within u16) and 65535. The items are the matching suffix of the first snapshot, and the end has count 0.
- fn 0 describe's first is also matched at every TLV position and past the end, and more and the TLV boundaries are checked.
- The same cursor results are required while the session is held and after end.
- channel_name (core §7.5) has a leading u16 channel, is within the declared channels, and there is one per channel. The name is
  1 to 16 bytes of `A-Z a-z 0-9 _ -` and does not repeat within the probe. The pin's display name (label) is the settings item `channel.{}.label` (core §13.8) and is not looked at here.
- describe of a nonexistent fn is unknown_function even when first is past the end, and does not change the declarations.

It does not prove all implementation-specific information when only the first snapshot is consistently wrong.
The name/instance/ops and describe of each interface are combined with InterfaceChecks.
What it checks is the current transport's frame; the minimum frame of other transports is not guessed.

Run it at the root of the client checkout.

```sh
uv run pytest -q tests/test_conformance_discovery.py
```

The pytest has 3 normal configurations, cursor/locked/unknown-fn faults, and invalid channel names.
The normal configurations are short names (several entries in the same page), 48-byte names, and no fns.
The public DeclarationModel is used with the fixture conditions given explicitly, without changing the model's own behavior.
This is a custom interface for the check, not an example of conformance of standard OEP features.
An example that saves the SPEC identity, checker/model SHAs and raw exchanges:

```sh
uv run python - /path/to/oep-spec /path/to/discovery-report.json <<'PY'
import json, struct, sys
from pathlib import Path
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance.discovery import DiscoveryChecks
from oep_client.conformance.declaration_model import DeclarationModel
from oep_client.virtual_core import tlv
registry, identity = spec_identity(sys.argv[1])
reports = {}
for geometry in ('short', 'long', 'empty'):
    model = DeclarationModel()
    model.extension.name = b'io.test' if geometry == 'short' else b'io.' + b'a'*45
    if geometry == 'empty': model.extension.functions = ()
    d = registry['core']['tlv']['describe']
    model.ep.static[0] = tuple(r for r in model.ep.static[0] if r[0] not in (d['channels'], d['channel_name']))
    model.ep.static[0] += (tlv(d['channels'], struct.pack('<H', 2)),
                           tlv(d['channel_name'], struct.pack('<H', 0)+b'GPIO0'),
                           tlv(d['channel_name'], struct.pack('<H', 1)+b'GPIO1'))
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    reports[geometry] = DiscoveryChecks(checks).run()
    reports[geometry]['wire_trace'] = checks.trace
report = dict(status='passed' if all(r['status']=='passed' for r in reports.values()) else 'failed',
              full_conformance=False, spec=identity, fixtures=reports,
              checker_sources_sha256=checker_sources_sha256())
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

The frames of other transports, invariance across firmware versions, concurrency, the Host's text display and all core norms remain not checked.
A PASS of this check is not treated as conformance of the whole core.


The additional revision range checks are used with the interval of revisions the fixture handles stated explicitly.
An integer tuple including revision1 is checked before session operations. The expected interval of the current virtual declaration fixture is (1,1).
With 25 pairs over the boundaries 0/1/2/254/255, an including range requires the highest supported revision, an unsupported range unsupported + tag0 + the supported TLV, and reversed order malformed.
The owned session is kept during the checks, and a resend of the latest clock is the saved response or result_lost. keepalive confirms that ownership continues.
This profile is not the minimum version of probe firmware. It is not used as evidence of running a fixture with several revisions.
Application in both directions per transport, declarations of other transports, and all retention side effects are a separate check scope.

Requests that change the range are check stimuli given to the Probe. A normal Host's re-confirm fixes min=max to the selected revision (core §7.1).
After the check stimuli, it returns to a confirm of the known revision1, then checks the clock resend and keepalive of the original session.
