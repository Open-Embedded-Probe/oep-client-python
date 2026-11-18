# Conformance checks and SPEC versions

[日本語](conformance-spec-compatibility.ja.md)

The contracts the checks require and the contracts of the selected SPEC must match.
Before the freeze, the spec changes even with the same protocol revision 1, so the checks are pinned to the reviewed body text.

Each checker carries in its code the SHA-256 of the body text it reviewed (`REVIEWED_CORE_SHA256`, `REVIEWED` and so on).
With a SPEC checkout whose body text differs, it stops before opening the device. The SHA of the core body text is also recorded in the SPEC information of the run result.
`tools/sync_registry.sh` writes the SHAs of the SPEC body texts to `tests/spec/DOCS_SHA256`, and
`tests/test_conformance_reviewed_pins.py` checks that the checkers' pins match it.
When taking in the spec, review the content of the checks and then raise the pin. Raising only the pin to make it pass is not done.
Unit tests with a hand-written registry confirm the chosen check conditions; they do not prove conformance to any SPEC.

The checks are stacked in this order: the core, the common contracts of all extensions, then the standard-interface-specific ones.
The minimal configuration and the main success, refusal and recovery paths are checked first, and the absence of optional features is treated as not applicable.
The absence or misbehavior of declared required operations is not skipped as missing equipment.

## Reading the SPEC's shared vectors

`tests/test_spec_vectors.py` applies `tests/vectors/*.json` (a copy at `tests/vectors/SPEC_COMMIT`) to this client and the virtual bench.

- cobs / headers / confirm / discovery / sessions / refusals / ops / ops_encoding / core_extensions /
  interface_profiles / logic_layout: all cases. Including events and data in ops.json (capture events and data frames).
- interface_boundaries: all cases of batching / reception / serial_loss / storage / groups / fixtures.
- checks.json: all cases. The dmseq CRC-8 and DATA0 words are applied to the dmseq checker's reference implementation and the virtual bench's dmseq model.
- core_lifecycle: all scenarios. Applied to the virtual bench's per-transport acceptance (`Endpoint.receive` / `process` / `sent` /
  `close_route`) with 0 resend records.
- core_policy: all cases. Applied to the virtual bench's optional acceptance policy (the test hook `Endpoint.policy`). The downstream responses are
  placeholder values in the vectors, so only the fact that the policy does not refuse is compared.

The other vectors are read by the tests of their features: capture_formats by `tests/test_capture_blocks.py` and the virtual bench's capture tests,
hid_reports by `tests/test_hid_stream.py`, settings_file by `tests/test_settings.py`.
