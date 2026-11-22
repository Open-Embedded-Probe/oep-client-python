# Documents in docs

[日本語](README.ja.md)

## Using the client

| Document | What is in it |
|---|---|
| [User guide](guide.md) | Installing, connecting, assigning pins, fixtures, capture, debugging and writing, settings |
| [The `oep` commands](cli.md) | The `oep` commands, kept sessions, TCP, finding pins, speeding up a UART bridge |
| [Modules](reference.md) | The public API |
| [Writing and debugging an SWD board](swd.md) | From the Arduino IDE and gdb through an OEP probe |
| [The pytest plugin](pytest.md) | OEP probes from pytest-embedded-arduino-cli |
| [Virtual bench](virtual-bench.md) | A probe and target that run with no hardware |

## Tests and equipment

| Document | What is in it |
|---|---|
| [Overall testing policy](testing-policy.md) | What OEP and the projects that use it guarantee, and who is responsible |
| [Guide for projects that use OEP](testing-consumers.md) | Test steps for using OEP from any language, target or framework |
| [Minimal setup check](hardware-quickstart.md) | validate / plan / virtual smoke and running with `.env` |
| [Fixing the configuration and wiring](hardware-configuration.md) | Units, discovery, checked connections, roles, the design of shared control |
| [Hardware settings format](hardware-schema.md) | The public keys and checks, the input and resolved TOML, the `.env` template |
| [Python hardware harness](../tests/hw/README.md) | Settings and steps |

## Conformance and the spec

| Document | What is in it |
|---|---|
| [Conformance checks from the SPEC](conformance-checks.md) | The public core / interface checkers and their scope |
| [Conformance checks and SPEC versions](conformance-spec-compatibility.md) | Pinning the checkers' text and the shared vectors |
| [Traceability by rule](trace/README.md) | oep-spec's normative units and the tests that check them |
| [Burdens found following the spec](spec-follow-notes.md) | Spec rules that are hard to implement (material for review) |

A document per checker:

| Document |
|---|
| [Checks of the resource number limit and restart](allocator-conformance.md) |
| [Logical operation checks of analog capture](analog-conformance.md) |
| [Checks of analog success payloads and raw data](analog-payload-conformance.md) |
| [Independent ARM-ADI checks and the logical fixture](arm-adi-conformance.md) |
| [Pre-checks of standard wire attach and console open](attach-conformance.md) |
| [Checks of optional block operations and state restoration](block-conformance.md) |
| [Logical operation checks of capture-group](capture-group-conformance.md) |
| [Checks of immediate group start and timing](capture-group-geometry-conformance.md) |
| [Describe checks of capture](capture-metadata-conformance.md) |
| [Wrap and release of capture segment serials](capture-serial-conformance.md) |
| [Checks of group capture with different rates](capture-timeline-conformance.md) |
| [Checks of stop, resend and lifetime for a mixed-rate group](capture-timeline-lifecycle-conformance.md) |
| [Core clock checks and the logical fixture](clock-conformance.md) |
| [Core outline checks](core-smoke-conformance.md) |
| [Boundary checks of corr u16](corr-conformance.md) |
| [Logical checks of the response and release on a broken wire](cut-conformance.md) |
| [Checks of data notifications and batching](data-conformance.md) |
| [Logical checks of riscv-dm dmi, halt/resume and console arbitration](debug-conformance.md) |
| [Lifetime checks of resource-addressed streams and debug connections](dependency-conformance.md) |
| [Cursor and channel name checks of core discovery](discovery-conformance.md) |
| [Checks of probe-side dmseq framing](dmseq-conformance.md) |
| [Logical checks of target-side dmseq](dmseq-target-conformance.md) |
| [Pipeline execution order checked from resource side effects](execution-order-conformance.md) |
| [Abandoning several pendings when corr is exhausted, and recovery on a new transport](exhausted-conformance.md) |
| [Describe metadata checks for the fixture family](fixture-metadata-conformance.md) |
| [GPIO and plan logical behavior checks](gpio-conformance.md) |
| [Core lifetime checks with a GPIO plan](gpio-lifecycle-conformance.md) |
| [Read checks of the core responses' fixed shapes and known TLVs](host-core-shapes-conformance.md) |
| [Deadline start points of the Host's receive loop](host-deadline-conformance.md) |
| [Checks of the Host's notification dispatch and deadlines](host-notification-conformance.md) |
| [Checking critical marking in Host requests](host-request-conformance.md) |
| [Resend of the same request and return after an unknown result](host-resend-conformance.md) |
| [Checks of the Host's TLV reader parts](host-tlv-conformance.md) |
| [Host classification of unknown values](host-value-conformance.md) |
| [Partial checks of the Host's save wait time and transfer time](host-wait-conformance.md) |
| [Logical checks of the I2C target](i2c-conformance.md) |
| [Compliance checks of declarations used for all interfaces](interface-conformance.md) |
| [Interface outline checks used for all extensions](interface-smoke-conformance.md) |
| [Supplementary checks of response sending and lease](lease-conformance.md) |
| [Checking link over several transports of the same Probe](link-routes-conformance.md) |
| [Checking the required link source/sink](link-transfer-conformance.md) |
| [Logical checks of logic capture](logic-conformance.md) |
| [SDI/DMDATA mailbox checks of the standard console](mailbox-conformance.md) |
| [Retention range of notification seq](notification-retention-conformance.md) |
| [Checks of notification seq wrap, resubscription and resend](notification-sequence-conformance.md) |
| [Specific declaration checks of standard wire / console](oep-declaration-conformance.md) |
| [Checks of representative OEP-specific scenarios](oep-example-smoke-conformance.md) |
| [Checks of write acceptance and debug completion results with explicit adoption](outcome-conformance.md) |
| [Checking combinations of pin declarations](pin-constraints-conformance.md) |
| [Supplementary checks of per-transport request acceptance and recovery](pipeline-conformance.md) |
| [Checking port_speed on a UART bridge](port-speed-conformance.md) |
| [Probe roles and request headers](probe-input-conformance.md) |
| [Supplementary checks of re-reading the state after result_lost](recovery-conformance.md) |
| [force during recovery and per-transport unknown results](recovery-races-conformance.md) |
| [Refusal order for requests with several invalid conditions at once](refusal-order-conformance.md) |
| [Op declaration checks of registered OEP interfaces](registered-ops-conformance.md) |
| [Supplementary checks of session resend and refusal cache under congestion](replay-pressure-conformance.md) |
| [Checks of optional reset and DM return](reset-conformance.md) |
| [Conformance checks of resource lifetime](resource-conformance.md) |
| [Races between pending responses and force](response-race-conformance.md) |
| [Partial conformance checks of Probe restart](restart-conformance.md) |
| [Supplementary checks of the resend cache retention limit and result_lost](retention-conformance.md) |
| [Supplementary checks of missing retention, history eviction and session/boot lifetime](retention-lifecycle-conformance.md) |
| [Logical checks of wire retries](retry-conformance.md) |
| [Checks of notification transports, response priority and partly sent queues](route-conformance.md) |
| [Checks of optional run and the loader state](run-conformance.md) |
| [Finite scan checks of standard wires](scan-conformance.md) |
| [Checks of scan idle_clock and targetsel](scan-options-conformance.md) |
| [Checks of selected standard describe metadata](selected-metadata-conformance.md) |
| [Checking recovery from lost results of end/open](session-loss-conformance.md) |
| [Outline checks of lease and force](session-smoke-conformance.md) |
| [Logical checks of the SPI target](spi-conformance.md) |
| [Checks of optional step and re-halt](step-conformance.md) |
| [read/marks checks of positioned streams with explicit adoption](stream-conformance.md) |
| [Lifetime checks of subscriptions and events](subscription-conformance.md) |
| [Pauses inside TCP frames and reconnection](tcp-recovery-conformance.md) |
| [Context, width and critical checks of TLV requests](tlv-request-conformance.md) |
| [UART logical behavior and plan lifetime](uart-conformance.md) |
| [Common stream checks applied to a plan-owned UART](uart-stream-conformance.md) |
| [Raw USB conformance checks](usb-conformance.md) |
| [Checks of the wire attach options](wire-options-conformance.md) |
| [Lease checks for standard resources and the response writer](writer-lease-conformance.md) |
| [Notification output with bounded retention: a representative fixture](notification-output-sample.md) |
| [Review of the core's fixed fields and omission](core-fixed-fields-review.md) |

The maintainer of the documents here is oep-client-python. Ownership of guarantees and pass/fail is split per implementation, and the
communication norms live in oep-spec. The overall policy is the project's, not a description of the Python tests alone. The real
equipment ledger is kept outside and given to runners as explicit settings.
