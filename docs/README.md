# Documents in docs

[日本語](README.ja.md)

| Document | Subject and status |
|---|---|
| [Minimal setup check](hardware-quickstart.md) | Steps to run validate/plan/virtual smoke and `.env` |
| [Conformance checks from the SPEC](conformance-checks.md) | The public core / interface checkers and their scope |
| [Outline checks of lease and force](session-smoke-conformance.md) | Checks lease expiry, resend and owner change with explicit time/resource adapters |
| [Notification output with bounded retention](notification-output-sample.md) | Checks subscription refusal, seq gaps, partial frames and response priority with a limited raw fixture |
| [Host TLV reader checks](host-tlv-conformance.md) | Checks unknown tags, critical, duplicates, corruption and length boundaries with an explicit reader adapter |
| [Host notifications and deadlines](host-notification-conformance.md) | Checks the current SPEC's role dispatch, seq of unknown kinds, and response waiting/pump while notifications continue |
| [Host classification of unknown values](host-value-conformance.md) | Checks unknown values that are failures, and cause/scan kind that keep their values |
| [Context and width of TLV requests](tlv-request-conformance.md) | Checks 4 fn/op contexts, equivalence of known critical, no change before refusal, and order |
| [Core clock checks](clock-conformance.md) | Checks reading, SID 0, the resend exception, lease and the u64 boundary with an exact logical ns clock |
| [Core discovery checks](discovery-conformance.md) | Checks list/fn 0 describe at any cursor, channel names and that declarations do not change |
| [Resource number limit](allocator-conformance.md) | Checks the u32 limit with an explicit seed, no reuse after release, resend and restart |
| [Notification seq and resubscription](notification-sequence-conformance.md) | Checks seq wrap, drop, subscription replacement and resend on a logical transport |
| [Raw USB checks](usb-conformance.md) | Independent bulk/HID checkers and a minimal software model |
| [Overall testing policy](testing-policy.md) | What OEP and consuming projects guarantee, and their responsibilities |
| [Guide for consuming projects](testing-consumers.md) | Test steps for the side that uses OEP from any language, target or framework |
| [Fixing the configuration and wiring](hardware-configuration.md) | Design of units, discovery, confirmed connections, roles and shared control |
| [Hardware settings format](hardware-schema.md) | Public keys and checks, input and confirmed TOML, `.env` template |
| [Python hardware harness](../tests/hw/README.md) | Settings and steps |
| [pytest plugin](pytest.md) | Using OEP probes from pytest-embedded-arduino-cli |
| [Writing and debugging an SWD board](swd.md) | Through an OEP probe from the Arduino IDE and gdb |
| [Conformance checks and SPEC versions](conformance-spec-compatibility.md) | Pinning the checkers' body texts, and the shared vectors |
| [Map from rules to tests](trace/README.md) | The normative units of oep-spec and the tests that check them |
| [Burdens found while following the spec](spec-follow-notes.md) | Spec rules that are inconvenient to implement (material for review) |

The maintainer of the documents here is oep-client-python. Ownership of guarantees and verdicts is split among the implementations, and the protocol norms live in oep-spec. The overall policy is common to the project, not a description of the Python tests only. The inventory of real equipment is managed outside and passed to the runner through explicit settings.
