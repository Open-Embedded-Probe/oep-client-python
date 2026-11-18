# Hardware settings format v1

[日本語](hardware-schema.ja.md)

The settings format that makes the [overall testing policy](testing-policy.md) and the [configuration management policy](hardware-configuration.md) concrete. `oep-hardware` does offline validate / plan, virtual smoke, and standalone hardware probe preflight, and `tests/equipment` is the explicit pytest entry point. It has no wiring discovery or target runner adapter, and the hardware tests of [tests/hw](../tests/hw/README.md) do not read this format. The minimal entry point is the [setup check steps](hardware-quickstart.md). It does not change the OEP wire protocol or the SPEC.

## Ownership and boundaries

The maintainer of the general format and the reference validator is `oep-client-python`, which has the hardware harness and discovery. It publishes the format's documentation, templates and conformance examples; pass/fail of probe firmware is owned by `oep-probe-arduino`, clients by each client, and the Core's pass/fail by the Core. Python runtime dependencies are not forced on clients in other languages. Arduino build/upload/peer lifecycle is left to the pytest plugin, with an adapter that connects equipment role resolution and the shared control session.

This document and the bundled templates are maintained as the authoritative source of the general settings format. The inventory and export of real equipment are held separately by each equipment administrator. It is a required condition that the runner does not refer to that inventory's path, checkout or generator, and that copying these templates alone is enough to write settings by hand.

## Use of the files

| File | Use | Entry point that uses it |
|---|---|---|
| `hardware.local.toml` | units, electrical conditions, known wiring, the range allowed to search | discovery. Not used for normal tests |
| `hardware.resolved.local.toml` | the same nodes, and confirmed connections, grounds and generation | plan, preflight, hardware tests |
| `.env` | the paths of the files above, results and the shared lock | `uv run --env-file .env ...` |

resolved does not include input. One self-contained resolved is passed per run. Several files may be made per use, but no merge, search or implicit default at run time is introduced. To add targets, add nodes and connections to the same file. When the inventory grows, the private generating source may be split, but the export keeps this format.

The templates are [`.env.example`](../tests/hw/.env.example), the [input TOML](../tests/hw/hardware.example.toml) and the [confirmed TOML](../tests/hw/hardware.resolved.example.toml). They illustrate one probe, 2 targets, independent debug control, USB between targets and GPIO observation. **Everything is fictional**: pad names are for replacement, and channels are for illustration. An implementation that operates hardware with `example = true` left as is is forbidden. Nor does it mean the current firmware supports simultaneous connections.

## v1 data

| Key / array | Meaning / validation |
|---|---|
| `schema_version` | integer `1`. An unknown version is an error. Unrelated to firmware versions |
| `kind` | `input` or `resolved`. Tests accept only resolved |
| `example` | required bool. true allows only offline validation and plan |
| `configuration_id` / `generation` | the name of the equipment set and a positive integer generation. The generation is updated on connection/unit changes |
| `confirmed_at` | resolved's confirmation date and time. An RFC3339 string with timezone |
| `probes[]` | a unique ID, identity, host transport. Does not contain targets |
| `targets[]` | a unique ID, chip, board, identity. Does not contain test roles or build profiles |
| `power_domains[]` | affected nodes `members`, how to operate, signal voltage, GND check, evidence |
| `discovery_requests[]` | input's operation range. probe/target/protocol/power, allowed and excluded channels, pads, write permission |
| `debug_links[]` | probe→target control paths. protocol, channel/pad mapping, power, state, evidence |
| `signal_nets[]` | signals that join several endpoints. signal kind, each node's pad or channel, mode, power, state, evidence |
| `usb_links[]` | data connections between targets. host/device endpoints, VBUS power source and voltage, GND, state, evidence |
| `evidence[]` | a unique ID, manual/observation, what was confirmed. For observations, tool/revision and the artifact path |

Node IDs must not repeat across all probes/targets. The connection IDs of each kind and evidence IDs are also unique within their kind. Broken references, wrong types and unknown keys are errors, which prevents settings from being ignored through typos. Relative paths are relative to the TOML's location; relative paths in environment variables are relative to the CWD at run time. No environment variable expansion or shell expansion is done on TOML values.

identity prefers the probe's `oep-unit-id` and the target's `chip-uid`. `manual-label` requires `reason` and is treated as identification that needs a human check at preflight. A match of physical units is not judged by serial port, chip family or label alone. UIDs that cannot be observed are not generated.

v1 transports handle OEP `serial`, `usb` named by unit ID, explicitly given `tcp`, and `virtual`, which uses no hardware (the virtual bench's profile is specified); debug handles `swio` / `rvswd` / `swd`. serial has a path, tcp a host/port, and usb a complete unit_id (core §7.5: 1 to 32 bytes of `a-z 0-9 -`; those starting with `x-` cannot be used for naming). The validator checks that the usb selector matches identity, and after connecting the unit_id of OEP describe is also matched. The channel mapping has the common keys `data`, `clock` and optional `reset`; the offline validator checks the count per protocol, and does not check profile/pad features or reserved pins. WCH-Link is a separate backend and does not pretend to be an OEP probe. Concrete keys for WCH-Link, measuring instruments and automatic power switches are not in the v1 templates.

A signal endpoint has either a target pad or a non-negative probe channel. mode is `input-only` / `output-only` / `bidirectional` / `open-drain`. For a shared bus's simultaneous drive conditions, pull-ups, analog values and so on, add the details the contract needs before adopting that test. A `digital` connection alone does not guarantee I2C, PWM accuracy or voltage accuracy.

When known wiring is written in input, the same link structure is used, with `state = "candidate"`. resolved has only connections with `state = "confirmed"`. Unconfirmed candidates and failures are all kept in discovery artifacts and are not mixed into the file for normal tests. Unknown channel limits, reserved pins and pad features are not judged by numbers alone; they need matching of the profile and the probe's declarations.

The probe's observed firmware, protocol/interface revision, describe and limits are saved in the evidence artifacts. They are not repurposed as a minimum firmware version or test adoption conditions. At run time they are taken again and saved in the results. The real artifact files and their SHA-256 are recorded in the run result manifest, and the SHA-256 of the settings file itself is also saved in an external manifest. The settings do not embed their own hash.

## Test selection and verdicts

The test owner's contracts have an ID, the adopted implementation scope, roles, the needed connections and capabilities, the build profile mapping, simultaneity, and stimuli, expected values and end states. Test pass/fail and build matrices are not put in the equipment TOML.

For example, a USB contract requires `dut` and `peer`, dut's host implementation, peer's device implementation, a USB link, control of each target, and 2 simultaneous consoles. host/device in the settings is the allowed direction of the physical connection and does not show whether an API implementation exists. The test owner judges the profile suitability of both sides. When reversing the allowed direction, re-check VBUS and the surrounding circuits too.

For run selection, explicit selectors by contract, probe, target and link IDs are provided. Test code does not have fixed lists of names. Assignments that satisfy the conditions are enumerated on the confirmed graph within the given configuration, excluding aliased assignments of the same target and pairs that are not connected. Each assignment records role→unit→control/link and the needed resources.

| Situation | Verdict |
|---|---|
| no config given, optional hardware suite | skip. Suites that use no hardware run as is |
| explicit config absent or invalid, input given for normal tests | settings error |
| explicit selector absent, or ambiguous where a unique choice is required | settings error. The first is not chosen |
| no physical connection suitable for an optional contract | skip with a reason |
| no connection suitable for a gate-required contract | record the shortage; the gate is incomplete. Not treated as success |
| saved units/connections do not match preflight | the configuration is invalid and the run stops. No re-discovery midway |
| a required op / connection count missing on an adopted platform | FAIL of the implementation. Tests are not removed because of missing declarations |
| lacking capability on a platform not adopted | state out of scope. Tell not implemented apart from missing equipment |

A normal run goes once through the chosen assignments in a stable order. The representative matrix and required contracts for release are specified in the test owner's manifest, and plan outputs all assignments and unmet items. More devices do not generate an unlimited product. backend/profile differences of the same role set are expanded only on the axes specified in the manifest.

## Entry points and priority

The entry points `oep-hardware` has are validate / plan / smoke / preflight. Runners with other entry points also follow these processing boundaries.

| Entry point | Input and side effects |
|---|---|
| validate / plan | offline checks of syntax, references and assignments. No ports are opened |
| discover | searches with explicit input and range. Writing is forbidden by default, and result candidates are saved |
| confirm / export | outputs a new resolved from candidate choices and grounds. Does not operate firmware/settings/power |
| preflight | holds the lock and matches units, declarations and needed connections. Operations needed for normal connection are recorded in the results |
| test | preflight→upload→READY→test→cleanup within the same lock-holding interval. Uses installed probes |
| probe update / diagnostic flash / applying settings | independent entry points that make writes and settings changes explicit. Not called implicitly by normal tests or export |

The common settings are `OEP_HW_INPUT`, `OEP_HW_CONFIG`, `OEP_HW_RESULTS` and `OEP_HW_LOCK`. For the same item, the explicit CLI wins over environment variables. `.env` is not loaded automatically by the runner. Neither input nor config is filled in by path search.

The adapter resolves OEP paths from resolved. When primary/peer ports are given directly to the Arduino plugin, **the explicit settings are not silently overwritten; they are matched against the resolved paths, and a mismatch is a settings error**. The plugin's priority order CLI → profile environment variable → common environment variable → sketch is kept. General serial upload/monitor and OEP debug control are treated as separate paths and are not pushed into a port with the same string.

Hardware is serialized with the host-wide lock `OEP_HW_LOCK`. The path is required explicitly, and no separate lock is generated automatically in the checkout. When DUT/peer use the same probe, they share one session and broker.

## Operating the templates and generation

To write by hand, copy the 3 examples, replace the input with real equipment, and create resolved through discovery and confirmation. When known wiring is entered by hand, confirm identity and connections before moving it to resolved too. Merely rewriting the date and time or state does not make it observed.

Generation from a private inventory also goes through the same validator. The output location is required, and normally it writes to a new file and the diff is checked. The whole existing `.env` is not regenerated in a way that erases secrets or hand edits. Only the 4 automatically generated paths are proposed in something like `.env.generated`, and updated explicitly when adopted. Generation does not start automatic loading of `.env` either.

## Guaranteed scope

`oep-hardware validate` checks the keys, types and references of these templates, and `plan` shows assignments based on the connections of PROBE-DECLARE / TOOL-FLASH / USB-DATA. plan does not measure or certify declared capabilities, pad features, electrical conditions or build profiles. USB pairs enumerate only known links, and simultaneous assignments with overlapping debug channels on the same probe are excluded. The needed connection count is shown, and real lack of capability is not converted into an implicit skip.

`smoke` accepts only explicit virtual transports, and checks COBS/CRC and OEP confirm, list/describe, identity and session open/end in-process. A virtual smoke, which uses no hardware, is possible even with `example = true`. The result is success within this limited scope, and does not guarantee full conformance of PROBE-DECLARE, real transports, multiple targets or USB communication.

`--config` wins over environment variables, and `OEP_HW_CONFIG` can be read. validate can also use `OEP_HW_INPUT` when no config is given. `--out` only creates a new JSON and does not overwrite existing files. smoke/preflight can number a new JSON automatically in `OEP_HW_RESULTS`. preflight takes the existing lock that `--lock` or `OEP_HW_LOCK` points to with POSIX flock and holds it until the cleanup of all probes. The lock is not newly created or replaced. `--out` or the results location is prepared before connecting, and if an existing result is given, it is refused before any hardware operation. validate/plan/smoke do not take the real equipment's lock.

`preflight` accepts only a non-example resolved, an observable OEP unit identity and an explicit physical transport. It records confirm, unit ID, firmware/model/chip, list/describe and required declarations, session open/end and transport close. It does not take a session with force or collect other runners' remembered sessions. It does not include target attach/flash, settings/power changes or wiring diagnosis, and is not used as run approval for other contracts. There is no minimum firmware version. `tests/equipment/test_preflight.py` skips only when no settings are given, and does not convert invalid explicit settings or failures into success or skips.
