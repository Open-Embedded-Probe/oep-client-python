# Specific declaration checks of standard wire / console

[日本語](oep-declaration-conformance.ja.md)

`OepDeclarationChecks`, as third-stage `oep-interface`, checks the required ops and the specific describe declarations of the selected standard names at revision 1. Its results are kept independent of the first-stage core compliance and of the second-stage `InterfaceChecks` usable for all fns and the checks of the adopted common parts.

The mapping between fns and standard names is given explicitly. There is no automatic application to all fns by name and no equipment search. Ops, tags and enums are read from the selected SPEC's registry. Here the 4 fns RVSWD / SWIO / SWD / console are selected.

| Check | Norm and application |
| --- | --- |
| fn/name/revision | The standard name and revision 1 of the selected fn exist in list, and the selected SPEC also defines revision 1. |
| wire ops | attach / detach / connections are required. scan is optional, so its absence is not a defect. |
| console ops | open / read / close / streams are required. marks / clear / mark / write are optional, and mark is declared only together with marks. |
| max_connections | u8 when declared. When omitted, recorded as 1. The real capacity for simultaneous connections is checked separately by behavior checks. |
| wire pins | Usable sets are stated with channel_group or role_channels. RVSWD / SWD have roles 1 and 2 on different channels; SWIO has role 1. Out-of-range channels are refused. Electrical use of a group and search of all candidates are not proven. |
| SWIO clock | The fixed bit time is declared as a positive u32 min_clock_hz. The real line period is not checked. |
| console mechanisms | tag 0x40 mechanism is declared as a list of names (core §7.7), one or more, with no repeated names. Standard names (sdi, dmdata, dmseq) or reverse-DNS names. A fn that declares write lists dmdata or dmseq. |

Each item's check uses only confirm/list/describe on SID 0. It takes no session and does no target operations. Merely carrying a standard name is not taken as conformance of op behavior. Standard-specific conformance of fns not selected is not reported either.

`OepDeclarationModel` is an independent fixture that holds only the declarations of standard names. The 3 wires have no scan; RVSWD declares max_connections 2 and the other 2 wires omit it; SWIO declares a fixed clock; console declares only DMDATA. Both role_channels and channel_group forms are used. attach and console delivery are not implemented. It is a check where a virtual fixture returns declarations, not evidence of a running probe.

The unit tests detect a missing required op, mark without marks, missing mechanisms / names that are neither standard nor reverse-DNS (including `none`), the width of max_connections, missing pin declarations / the same pin / out of range, missing / wrong width / 0 SWIO clock, and mismatched selected fn / standard name / revision. They accept no scan, the minimal console ops (open/read/close/streams) and an SDI-only console without write, and do not ignore required ops added to the selected SPEC. An invalid selection is reported as a failure, and each third-stage result and the SID 0 requests are checked.

```sh
uv run pytest -q tests/test_conformance_oep_declarations.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.oep_declarations import OepDeclarationChecks
from oep_client.conformance.oep_declaration_model import OepDeclarationModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
selection = {1: 'oep.wire.rvswd', 2: 'oep.wire.swio',
             3: 'oep.wire.swd', 4: 'oep.target.console'}
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = OepDeclarationModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = OepDeclarationChecks(checks, selection).run()
assert report['status'] == 'passed', report
```

It is not added to the normal CLI automatically. Name the SPEC/shared lock of `.env.example` explicitly; the public checks do not depend on or search for private benches.

`full_conformance=false`. The existence of the corresponding target interface, critical TLVs and refusal conditions of requests, the electrical procedures of attach/scan, console mechanism conflicts/delivery/framing, the common stream/status/resource lifetime, hardware pins and production Host integration are checked separately.
