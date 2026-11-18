# Overall OEP testing policy

[日本語](testing-policy.ja.md)

This defines what OEP, consuming projects, and measurement / pytest integration guarantee, and their responsibilities. The contracts listed here are units of guarantee; what shows that each implementation meets them is the result of that implementation's tests.

This document is the testing policy common to the OEP project, not a description of only the Python tests in this repository. The document's maintainer is oep-client-python, and changes to what is guaranteed are reviewed by the owners of the implementations concerned. The authoritative source of protocol norms, numbers and shared vectors is oep-spec, and the settings format of the test infrastructure is not added to the protocol's conformance conditions.

Records of real units, wiring, connections, power and equipment operation are managed by each equipment administrator, separately from the public code. Public tests work with their own repository and explicitly given settings, and need no other equipment inventory or generator. Equipment-specific IDs, paths and pass criteria are not embedded in shared documents.

The entry point for consumers is the [test guide for projects that use OEP](testing-consumers.md). The design of the infrastructure is in [Fixing the configuration and wiring](hardware-configuration.md) and the [hardware settings format](hardware-schema.md). The steps of the Python hardware harness are kept separately in [tests/hw](../tests/hw/README.md).

Conformance checks are split into 3 stages: "core", "interface common" and "OEP interface specific". The first 2 stages are also used for custom extensions, and the third has checks for each norm and templates for custom extensions. For the definitions, see the [SPEC's conformance guide](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/conformance.md); for the scope the checkers run, see the [conformance check entry point](conformance-checks.md).

## 1. Units of guarantee

A test unit is "contract × implementation × condition × observation method". A contract has an ID, what is guaranteed, the stimulus, the expected result and tolerance, the needed roles, connections and features, the supported scope and the end state. A contract ID does not include the physical unit, probe model, fixed channel or experiment date. Swapping units is treated as a change of run conditions.

Compilation, success in a virtual environment, a self-report inside the DUT, external measurement and interoperation with an independent implementation are different guarantees. A loopback cannot detect a case where its own TX/RX share the same mistake, and timing measured with the same clock cannot detect that clock's error. Expected values live on the test side, and pass criteria are not changed by equipment settings. Per-unit calibration values record their origin, measurement conditions and valid range.

Contracts include normal, boundary, invalid input, races, disconnect/restart and recovery after failure. Not every test is forced to create all combinations; choose the axes that have grounds in the implementation differences of that contract.

## 2. Test groups and owners

| Test group | What it really guarantees | Main owner and how it is checked |
|---|---|---|
| Spec consistency | consistency of norms, numbers, generated files and shared vectors | oep-spec's existing checks. No implementation or equipment tests are placed there |
| Virtual probe spec conformance | state transitions, refusals, resend, session, resource lifetime and notifications follow the SPEC | oep-client-python. Checked with spec vectors and independent inputs/expected values |
| OEP host | framing, TLV, revision, discovery, pipeline, timeout, resend/recovery, unknown values | each client and ch32rv's OEP implementation. Unit + virtual + independent hardware |
| broker | per-client resources and ownership, sharing, disconnect recovery, separation of multiple targets | ch32rv. Virtual fault injection and multiple clients on hardware |
| Probe protocol implementation | list/describe, declarations matching the implementation, session, refusal, resource conflicts | oep-probe-arduino. C++ unit tests + hardware checks by an independent host |
| Real OEP transports | splitting/joining, boundaries, disconnect, re-enumeration and recognizing the same unit over serial/bulk/HID/TCP | the probe side and each host side. The probe's USB and the DUT's USB are separate tests |
| debug wire and DM/ADI | scan, attach, halt/resume/step/reset, read/write, limits, broken wire, state saving | oep-probe-arduino. Known targets and independent observation/host |
| target-specific flash/debug | RAM loader, erase/program/readback, boundaries, partial updates, interruption, GDB, console coexistence | ch32rv. Virtual + known diagnostic image + hardware. Python's own path is handled by Python |
| Probe fixtures | GPIO, UART, I2C/SPI peer roles, driving/release, resource sharing | oep-probe-arduino. Matched against known signals or an independent peer |
| capture and clocks | rate/layout, trigger, pretrigger, ring, drop, notifications, logic/analog/group, time uncertainty | the probe and each client. Verified layer by layer with known signals, saved data and an independent reference |
| firmware distribution and updates | profile, identity, manifest/hash, update targets, boot/settings/return after update | oep-probe-arduino. The communication/UI of a client-side updater belongs to that client |
| Arduino definitions and build | part/package, pin/route, board/options, startup/link, dependencies, distribution | ArduinoCore-CH32RV. Generation consistency, host, build of all adopted targets, isolated install |
| Arduino runtime/API | GPIO/interrupt, time, Print/Stream, UART, Wire, SPI, ADC, PWM, DAC, timer | ArduinoCore-CH32RV. Self checks + external stimulus/observation + independent peer |
| Connection between Arduino and tools | discovery, upload, reset, monitor, HID bootloader, re-enumeration | ArduinoCore's minimal E2E. ch32rv has deeper verification of the internal processing |
| DUT USB | host/device API, class, transfer, recovery, coexistence with other peripherals | ArduinoCore-CH32RV. DUT and peer by role, interoperation with independent implementations |
| USB PD | CC negotiation, source/sink, fixed/PPS, fault, real VBUS | ArduinoCore's adopted scope. PD source/sink and independent voltage observation |
| Measurement data and analysis | source, time, gaps, file format, decoder, matching and unknown/unchecked | wireskein. Separate tests of synthetic data, real recordings, virtual OEP and hardware capture |
| Browser display | reading the file format, logic/analog, attachments, display of time and values | wireskein-web. Known fixtures and browser checks |
| pytest integration | primary/peer lifecycle, port/profile, end handling, report, not hiding failures | pytest-embedded-arduino-cli and pytest-embedded-wireskein. Each feature verified by its owner |

Conformance to shared vectors alone does not claim conformance of the whole protocol. A pair of the Python client and the same-Python virtual probe alone does not claim independence. Combine multi-language implementations, expected values from the norms and matching against hardware.

## 3. Required test contracts

The IDs in the table below are a proposed contract classification, not current test names. The values, counts and tolerances of individual cases are defined from the norms' values or the target implementation's declarations. The count of reproduction tests is decided separately from the contract's failure modes and run time.

| Class | Checks needed besides the normal path | Main run axes |
|---|---|---|
| HOST-FRAME | splitting/joining, around the maximum length, CRC, invalid role/TLV, delay/loss/duplication | transport, host implementation, frame/window boundaries |
| HOST-RECOVER | resend of the same request, boot/session invalidation, reopening, no wrong re-execution | disconnect point, restart, whether the state changed |
| BROKER-OWNERSHIP | coexistence of monitor/flash/GDB, resource release of ended clients, no crosstalk between targets | client count, target count, same/different probe |
| PROBE-DECLARE | revision/ops/limits matching behavior, refusal of unknown features, unit identification | model/profile, transport, implemented SPEC |
| PROBE-SESSION | end/lease/force, release of pins/streams/plans, refusal on conflict | resource count, multiple transports, midway failure |
| PROBE-SETTINGS | get/set/unset/save, restart, broken saved content and partial application, writing and reading the settings file, settings restore | storage, settings items, application order |
| DEBUG-WIRE | candidate search, ambiguity, keeping existing connections, attach/reset, wire lost, time limits | RVSWD/SWIO/SWD, target family, clock |
| DEBUG-MULTI | separation of state, streams and reset of A/B on the same probe, refusal at the limit | connection limit, wire/slot count, real simultaneity |
| TOOL-FLASH | empty / unaligned / boundary / largest image, partial update, readback, refusal of the wrong unit, midway failure | chip / erase granularity / ABI, probe backend, transport |
| TOOL-DEBUG | registers/RAM, step/breakpoint, reset/run, not breaking the console, running after detach | chip/DM, HW/flash breakpoints, monitor coexistence |
| FIXTURE-IO | GPIO direction/idle, UART baud/format, I2C NACK/stretch/recovery, SPI mode/bit order/CS | channel resources, speed, peer implementation, buffer boundaries |
| CAPTURE-DATA | known signals, trigger/segment, drop detection, layout, multiple rates, saving/reloading | logic/analog, channel count, rate, load, capture method |
| CAPTURE-TIME | an independent time reference, uncertainty, synchronization between probes, different boot_id | same/multiple probes, clock, trigger method |
| UPDATE-PROBE | matching the intended image, update, reconnection, settings check, regression of the supported consumer paths | model/profile, flasher, versions before and after the update |
| CORE-RUNTIME | startup, constructors, heap/stack, small RAM, Print/Stream, boot after reset | ABI, family, clock, package, build options |
| CORE-PERIPHERAL | API values and external signals, abnormal input, timeout, shared timer/IRQ, release and reconfiguration | instance, route, clock, load, implementation differences |
| CORE-PACKAGE | install, FQBN, pinned dependencies, library compile, recipe and the real core selected | package/local source, board/options, OS |
| USB-ENUM | descriptor/configuration, control requests, invalid requests, identifying the peer | DUT host/device, controller, speed, class |
| USB-DATA | known payloads in both directions, sequence/length/CRC, packet/buffer boundaries, short/ZLP | role, class, transfer, payload/load |
| USB-RECOVER | bus reset, stall/clear, USB unplug/replug, peer reset, communicating again, UART/timer coexistence | what is reset, power, role, simultaneous operation |
| PD-POWER | negotiation matching the real voltage, fixed/PPS, unplug/replug, reset, fault and end state | adopted role, source/sink, voltage/current conditions |
| RUN-LIFECYCLE | READY on both sides, start synchronization, timeout, cancellation, cleanup of logs/settings | single/peer, control path, failure point |
| WIRING-RESOLVE | identifying the pin map, multiple candidates, swaps, re-checking, invalidating old results | probe/target count, candidate range, wiring changes |
| EVIDENCE-VERIFY | not treating gaps/unchecked as success, correspondence of log/waveform/verdict | recorder, decoder, report, storage format |

This list does not expand the adopted scope of USB classes or Arduino APIs without limit. For each feature declared as supported, fill in the normal, abnormal and recovery contracts. Unsupported features are out of scope in the support table, told apart from not implemented / not verified.

## 4. Run layers and release decisions

Without hardware, check pure logic, generated files, the protocol, virtual probes/targets, the host core and build/install of distributions. The peer lifecycle over virtual connections is also verified here. Normal collection and runs work even without equipment settings.

On hardware, separate the contracts of a probe alone, a probe with a known target, an Arduino DUT with loopback, a DUT with an independent peer, a DUT with a measuring instrument, and multiple clients/targets. Choose the needed equipment from the tests' requirements, and do not embed physical unit names in the test bodies.

The owning side defines what release checks cover before a change. Representative hardware is chosen by differences in silicon, ABI, clock, controller, transport and implementation path. There is no need to run the product of all values of all hardware every time, but do not hide unrun adopted difference axes behind the total test count.

| What changes | Responsibility before release |
|---|---|
| probe firmware | The probe side runs and judges probe unit tests/build, hardware checks of the candidate image, return after update, and regressions of the affected client/ch32rv/Core paths |
| host/client | The client side checks on virtual and installed hardware. For changes to update UI/transport, add updater contracts and run them explicitly as update work |
| ch32rv | Checks protocol/broker, flash/debug with an independent diagnostic image, regressions of supported backends, and the affected range of Arduino recipe/monitor |
| ArduinoCore | Checks definitions/build/package, APIs and external observation, adopted USB roles/classes, and minimal upload/monitor E2E |
| measurement/plugins | Checks that implementation's unit tests and usage workflow. It does not stand in for quality assurance of probe firmware |

Normal tests of consuming projects check behavior on installed probes and record the versions used. No minimum probe firmware version is defined in test settings. The protocol/interface revision handled and the needed features are verified. Judging known WCH-Link defects and updating it is the tool side's responsibility.

If a candidate fails, the release that requires that contract is incomplete. The decision is not "stop all repositories at once because they share integration tests". Even for a failure whose cause cannot be identified, the fact that a required contract could not be confirmed remains.

## 5. From wiring discovery to tests

Tests do not assume "the wiring is decided from the start". Initial connection candidates, discovery results, the confirmed configuration and this run's role assignment are told apart.

```text
Specify units, power and candidate range
  → Check the probe's declarations and existing resources
  → Search for debug wires and target units
  → Confirm the needed GPIO/UART etc. mapping with diagnostic firmware
  → Confirm the connections and save the configuration
  → Assign roles from the test requirements and show the run plan
  → Re-check units and connections
  → Run, observe, end handling, save evidence
```

Discovery itself may involve writing or reset, so it does not run automatically during normal pytest collection. If the wiring stops holding during a test, it does not search for another target on its own and continue. Discovery is a preparation step with an explicit range, and it also records candidate causes of failure. Details are defined in [Fixing the configuration and wiring](hardware-configuration.md).

## 6. Multiple targets and USB pairs

Even with the same physical board, the roles of DUT, peer and observer change during tests. USB host/device is the role in that test and is not fixed to board names such as CH32X035 or ESP32-S3.

```text
PC ─ OEP probe ─ debug/control ─ target A (DUT this time)
               └ debug/control ─ target B (peer this time)
                                 A ─ USB data ─ B
```

debug/control is writing over RVSWD and the DM console and so on; USB data is the path under test. The peer's UART wiring is not made required just to test USB. A CH32X035 peer with RVSWD only is an official target of this configuration.

### 6.1 Implementation goals for simultaneous connections

The settings model can express multiple targets on one probe, multiple probes, and multiple backends to one target. Target units and debug paths are kept separate.

A configuration that controls A/B at the same time on one probe needs the wire's max_connections, console count and pin/CPU resources to be actually met. The configuration model does not limit probes and targets to 1:1. The probe declares its real limits. A limit of 1 is accepted on small platforms, but such a platform is not assigned to contracts that require simultaneous control.

A configuration that writes in turn and switches connections to check READY/status is expressed as a separate capability. However, it has constraints: missing logs during the switch, waits in the DM mailbox, and state saving. It is not a substitute for contracts that need constant control of both sides and simultaneous observation. A configuration that controls both sides with multiple probes can also be assigned to the same contracts.

The OEP session lock is per probe. A/B on the same probe are not set up so that independent pytest peer fixtures fight over them in separate sessions. A broker or a shared session owned by the test side manages both, and separates per-target operations and streams. The handling of a transport disconnect of the shared session and a client leaving is also tested.

### 6.2 USB test series

| DUT and peer | What is guaranteed |
|---|---|
| DUT host / known device | host enumeration/control/class/data/recovery |
| DUT device / known host | device descriptor/class/data/recovery |
| DUT and a cheap CH32X035 peer with the same stack | regressions of repetition, resource conflicts, load and recovery. A separate row from independent interoperation |
| An independent stack such as ESP32-S3 or a PC | interoperation for the supported role/class/speed |
| USB PD source/sink | contracts of CC and voltage. Not inferred from USB data success |
| HID bootloader upload | contracts of writing and application start. Not used to guarantee the sketch USB API |

Writing, control and logs of both sides go on a path separate from the USB under test. After writing, a re-queryable READY is sent to both sides, and the build ID and role are confirmed before starting USB. The order of getting the start response and the enumeration log is fixed so that waiting for the response does not read and discard earlier logs. At the end, USB, debug and capture are each released.

The peer's VBUS power source, switch, GND and whether back-powering happens are confirmed equipment information. The other end of a USB cable or the power relations are not inferred from debug discovery results alone. FS success does not guarantee HS, and the adopted scope of USB controller/speed/class is defined independently.

## 7. Selection and verdicts

What a test defines is roles and needed conditions. Assignments that meet the conditions are generated from the configured and confirmed connections, and pairs that share the same physical resources are serialized. The whole set of discovery candidates, or every device that looks like USB, is not made a matrix.

The equipment's physical capability, the scope the implementation declares as supported, and this probe's declarations are told apart. A contract is not removed from the targets because a candidate firmware forgot to output required ops. Missing declarations are recorded as conformance test failures.

There is no need to run all pairs every time. Provide explicit selection of connections, targets, probes and contracts, and a release selection that includes representative differences. USB pairs are limited to A/B that are physically connected. On equipment that can swap roles, A=host/B=device and B=host/A=device are planned as separate runs.

| Situation | Record / handling |
|---|---|
| Unsupported silicon/feature | out of scope. Told apart from the support declaration |
| Feature not implemented, test not defined | not implemented / test not ready. Not made out of scope or PASS |
| No optional equipment/wiring | skip with a reason / not run |
| Explicitly given config/unit/connection absent or ambiguous | settings/preparation error. No substitution with the first candidate |
| Mismatch when re-checking the confirmed configuration | stop as an old configuration, and require re-confirmation |
| READY, declarations or operations of a candidate firmware differ from expectations | save the failure. Not classified as missing equipment without grounds |
| A contract that ran differs from expectations | FAIL. Save the facts and evidence even if the cause is not identified |
| A required release contract not run or skipped | release check incomplete |

During development, optional cases may be skipped, but the run plan shows planned, selected, out of scope and missing equipment. Release success is judged from required contracts and difference axes being filled in, not by pytest's exit code alone.

## 8. Evidence and reproducibility

For each run, save the contract ID, the repository's commit/dirty, the supported SPEC version, build profile, core/library/tool/client/probe/peer firmware, image hash, unit, the version/hash of the resolved topology, role assignment, transport, stimulus, expected values, observations, verdict and end state. For values that cannot be obtained, record unknown and the reason.

Tie logs, readback, waveforms, capture layout/rate, time uncertainty, both sides' READY/build ID and failure snapshots to the results. State the difference between capture time and test time, and do not compare clocks of different boots directly. Do not save credentials. When reusing artifacts for regression tests of decoders/display, keep that separate from guaranteeing the capture side's behavior.

Save both operation failures and cleanup failures, and do not let a cleanup exception erase the original cause. State end states such as settings that could not be restored, a diagnostic image left on the DUT, or the peer's USB still enabled.

## 9. Related authoritative sources and implementations

- [OEP conformance](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/conformance.md), [debug wire and connection](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/interfaces/oep-if-debug.md), [settings (core §13)](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/oep-core.md#13-settings-optional). The authoritative norms live in oep-spec, and this policy defines no new communication rules.
- [Python pins discovery](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/src/oep_client/pins.py), [hardware tests](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/tests/hw/README.md), [unit table](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/tests/hw/boards.py).
- [probe DebugPort](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/src/OepTarget.h), [Settings](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/src/OepSettings.h), [implementation limits](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/docs/implementation-limits.ja.md), [C++ unit tests](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/tests/host/run.sh), [CI](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/.github/workflows/tests.yml).
- [ch32rv OEP and broker](https://github.com/ch32-riscv-ug/ch32rv/blob/main/docs/oep-host.ja.md), [virtual OEP](https://github.com/ch32-riscv-ug/ch32rv/blob/main/crates/oep/tests/virtual_bench.rs), [getting the virtual environment](https://github.com/ch32-riscv-ug/ch32rv/blob/main/crates/oep/tests/virtual_bench/uv.rs), [flash E2E](https://github.com/ch32-riscv-ug/ch32rv/blob/main/cli/tests/oep_flash.rs), [monitor E2E](https://github.com/ch32-riscv-ug/ch32rv/blob/main/cli/tests/oep_monitor.rs).
- [ArduinoCore plan](https://github.com/ch32-riscv-ug/ArduinoCore-CH32RV/blob/main/tests/TEST_PLAN.ja.md), [matching against the implementation](https://github.com/ch32-riscv-ug/ArduinoCore-CH32RV/blob/main/docs/test-coverage.ja.md), [configuration loader](https://github.com/ch32-riscv-ug/ArduinoCore-CH32RV/blob/main/tests/harness/bench.py).
- [pytest Arduino basics](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli/blob/main/TESTING_BASICS.ja.md), [details and peer principles](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli/blob/main/TESTING_ADVANCED.ja.md), [peer lifecycle example](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli/blob/main/examples/12_peer_host_core/README.ja.md).
- [WireSkein capture tests](https://github.com/Open-Embedded-Probe/wireskein/blob/main/tests/test_oep_virtual_bench.py), [capture matching](https://github.com/Open-Embedded-Probe/wireskein/blob/main/docs/capture-test-guide.ja.md), [pytest recording integration](https://github.com/Open-Embedded-Probe/pytest-embedded-wireskein/blob/main/tests/test_plugin.py), [JS client pre-publish check](https://github.com/Open-Embedded-Probe/oep-client-js/blob/main/docs/publish.ja.md), [web viewer release check](https://github.com/Open-Embedded-Probe/wireskein-web/blob/main/docs/release.ja.md).
