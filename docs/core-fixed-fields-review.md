# Review of the core's fixed fields and omission

[日本語](core-fixed-fields-review.ja.md)

core §2.3 "do not place omittable fields in the fixed part" is applied to the revision1 core payloads.
The body SHA shares the pin of the [core shape checks](host-core-shapes-conformance.md).
CoreFixedFieldChecks takes the SPEC explicitly, and when the body text or numbers change, it is reviewed again.
The generated registry is used to match numbers. Widths and omission conditions are fixed in the ledger below from body §1.1/4.3/6.4/7.
There is no dependence on private benches and no automatic search of a neighboring SPEC.

All of the following fixed fields are required. Meaningful zero values and empty lists do not omit bytes either.
Omitting the following TLVs does not change the width of the fixed part. A variable name always has a name_len.

| op | Required request payload | Required success payload | Fixed prefix width (request/success) |
|---|---|---|---|
| confirm | magic4, min_rev u8, max_rev u8 | magic4, revision u8, max_frame u16, window u32, max_inflight u8, boot_id u32 | 6/16 |
| list | first u16 | total u16, count u8, count entries | 2/3+entry |
| describe | fn u16, first u16 | more u8 | 4/1 |
| clock | empty | boot_id u32, uptime_ns u64 | 0/12 |
| open | lease_ms u32, force u8 | lease_ms u32, boot_id u32 | 5/8 |
| end | empty | empty | 0/0 |
| keepalive | empty | empty | 0/0 |
| lock_state | empty | locked u8, remaining_ms u32 | 0/5 |

A list entry is fn u16, instance u16, revision u8, name_len u8, name.
The 6-byte fixed prefix and the name given by name_len are required, and name is 1 to 48 bytes.
Even with count0, total/count are present. Even with no declarations following describe, more is present.
Even with locked0, remaining_ms0 is present. open's lease_ms0 is the value that picks the default, and force0 is always sent too.
clock's uptime0 and unsupported's tag0 do not mean omitting a fixed field either.

The fixed payload of rejected is decided by the reason (§4.3).
The 9 kinds unknown_function, unknown_operation, malformed, unavailable, window_exceeded, no_session,
session_required, no_resource and result_lost are empty.
Only locked requires the 4 bytes of remaining_ms u32, and unsupported the 1 byte of tag u8.
The details of unavailable are optional TLVs; no fixed cause byte is added.
The owner of locked/lock_state is a TLV present only when set. Without an owner, the fixed part is the same.

The check runs CoreFixedFieldChecks against the adopted result decoder and a factory that returns independent logical Probe Checks.
`CoreFixedFieldChecks(decoder, probe_factory, spec="/explicit/oep-spec").run()`.
The public result reader part `oep_client.conformance.host_core_sample.decode` and ResourceModel's Core can be used as reference examples.
The factory returns an independent state for the check each time. It is not an API for unannounced session operations on hardware.
It checks REVIEW, REQUEST-FIELDS, RESPONSE-FIELDS, ZERO-VALUES and OWNER-TLV.
It checks stimuli with each request/response fixed field removed entirely, and with each field of a list element missing.
These are missing-field stimuli without a tail, and they do not prove all ambiguities with arbitrary added byte strings.

`uv run pytest -q tests/test_conformance_core_fixed_fields.py`.
It includes the normal case, a fault accepting a response with a missing field, a fault accepting a short request, confusion of zero values/owner omission, and detection of a ledger with confirm flags.
This is a review of the core payloads; all message headers, the requests/responses/events/data/TLVs of extensions,
and the fixed shapes and omission conditions of settings values (core §13) need a separate review. full_conformance=false.
