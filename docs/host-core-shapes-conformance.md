# Read checks of the core responses' fixed shapes and known TLVs

[日本語](host-core-shapes-conformance.ja.md)

`HostCoreShapeChecks(decoder, spec=...)` is a first-stage Host part check.
For core revision 1 of the explicit SPEC, it matches the success responses of the real 8 operations, 11 refusal reasons
and 33 known TLV contexts against independent byte strings.
`oep_client.conformance.host_core_sample.decode` is a connection example and is not built into the production Host automatically.
No search of private benches, equipment settings or sibling checkouts is needed.

What is passed to the decoder is the response frame and the requested op, plus the target fn for describe.
The response has no fn/op, so the caller states this context explicitly.
The end of the response is not decided using the request's count or the requested length.
The result is a dictionary of `corr`, `successful`, `fixed` and the adopted `tlvs`.
TLV values and names are kept as raw bytes; text sanitization before display is someone else's job.
A broken frame/payload is refused with ValueError.
For a broken known TLV, a reader that ignores the value without using it is also allowed.

Below is the ledger of fixed shapes from reviewing the core text. The widths are the payload's fixed part and do not include the 10-byte request / 5-byte response header.
Only TLVs can follow any row. A list entry does not carry its own length; only name_len decides the name's length.

| op | Request fixed part | Success response fixed part |
|---|---|---|
| confirm | magic4, min_rev1, max_rev1 (6 bytes) | magic4, revision1, max_frame2, window4, max_inflight1, boot_id4 (16 bytes) |
| list | first2 | total2, count1, count × entry. An entry is fn2, instance2, revision1, name_len1, name |
| describe | fn2, first2 | more1 |
| clock | none | boot_id4, uptime_ns8 (12 bytes) |
| open | lease_ms4, force1 (5 bytes) | lease_ms4, boot_id4 (8 bytes) |
| end | none | none |
| keepalive | none | none |
| lock_state | none | locked1, remaining_ms4 (5 bytes) |

The optional owner is a TLV in the open request and the lock_state/locked responses, and is not in the fixed part.
open's owner is non-critical per body §6.4. This reader check is not a check of the request encoder.
Among refusal payloads, only locked has remaining_ms4, only unsupported has tag1, and the other 9 reasons have no fixed part.
unsupported's tag 0 is a mark showing a value in the fixed part, told apart from TLV number 0.
Undefined core failed/partial payloads are not read in the success shape.

The known TLV contexts are as follows.

- The 9 common describe tags (including setting / setting_choice) are checked for both fn 0 and non-0.
  This includes role/base/bitmap of role_channels, n×3 bytes of channel_group, the base/bitmap limit of ops, and the name lengths of setting and setting_choice.
- The 9 tags of fn 0 describe are firmware/model/unit_id/channels/channel_name/netif/transport/chip/max_op_ms.
  Tag 0x40 of a non-0 fn is not read as fn 0's firmware.
- confirm's transport, owner of each of locked/lock_state, and unavailable's cause/channel/item/fn.
- unsupported has supported/channel/item/fn in confirm and channel/item/fn in other ops.
  supported's tag 1 is not read as cause or owner.
  An interface's index is in each interface's 0x40 to 0x7F, not a core tag,
  so it also checks that the registry's core `unsupported_payload` has no index.
- The settings ops (get to network) are matched only by number; the result shapes are not read by this check.

It checks fixed shapes, short headers / fixed parts / counted names, corruption of following TLVs, the width/count of known values,
the first value of duplicates and repeat order, separation of contexts, variable-length boundaries and bool.
KNOWN-WIDTH places a broken value of the same tag after a correct known TLV and checks that the later value is also validated and not used
(core §2.3. A reader that does not use the whole response, or that drops only the broken value or its tag, is allowed).
The second of a duplicate holds a different valid value. A high-bit alias of a known tag is also skipped as an unknown tag.
Even for an unknown TLV, a length past the end means it is broken.
It includes samples of list with 0/1/48/255 entries, channel_group with 0/1/255 members, and text/TLV values up to 65535 bytes.
These are boundary checks of payload parts and do not prove they fit in a transport's max_frame.

The body SHA and protocol revision / numbers / tag spaces are pinned to the values at review ([Conformance checks and SPEC versions](conformance-spec-compatibility.md)).
When the SPEC changes, it stops until the inputs are read again, and shape compatibility is not inferred just because the numbers are the same.
A future revision that changes fixed shapes is not applied unconditionally to this revision 1 reader.

The unit tests include a normal report, wrong fixed shapes, wrong known values, use of a broken later duplicate, semantic faults,
a reader allowed not to use broken known values, detection of SPEC changes, and detection of index as a core tag.
They catch requests with wrong element lengths, accepting short shapes, using a critical alias, caching across contexts, the last duplicate value,
sorting repeats, reading bool only as 0/1, and so on.

```python
from oep_client.conformance.host_core import HostCoreShapeChecks
from oep_client.conformance.host_core_sample import decode

report = HostCoreShapeChecks(decode, spec="/explicit/oep-spec").run()
assert report["status"] == "passed"
```

```sh
uv run pytest -q tests/test_conformance_host_core.py tests/test_conformance_host_tlv.py
```

`full_conformance` is false. The shapes of all interfaces, request generation / critical policy, Probe sending,
aggregation of several pages / presence of required declarations / meaning of metadata, corr/resend/recovery/notifications,
the production Host and physical behavior are outside this check.
The [general TLV reader](host-tlv-conformance.md) and [unknown value classification](host-value-conformance.md) are also kept as separate reports.
