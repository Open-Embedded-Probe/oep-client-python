# Discovering and fixing the hardware configuration and wiring

[日本語](hardware-configuration.ja.md)

Configuration management derived from the [overall testing policy](testing-policy.md). The keys, selection and verdicts, and the processing entry points are in the [hardware settings format](hardware-schema.md).

Physical units, connections, discovery candidates, confirmed information and roles during tests are kept apart. One target is not bundled with one probe. Test bodies have no fixed channels or unit names; they request logical signals and the needed capabilities. Merely loading settings does not start discovery, writing or a firmware update.

## 1. Information managed

| Information | Content and authoritative source |
|---|---|
| probe unit | ID, identification method, transport from the host, firmware update path. A replaceable physical node |
| target unit | chip/board/package, unit identification such as UID. Independent of the probe's channels and ports |
| observers, power, switches | how they are used, measurement conditions, resource IDs, the group of units affected by operations |
| connections | debug, GPIO/UART, USB data, power paths. Different kinds of connections are expressed separately |
| discovery range | probe channels and target pads allowed to be operated, exclusions, voltage/power, operations allowed for discovery |
| discovery results | the full list of candidates, observed values, unit information, diagnostic image, discovery tool/version, date and time |
| confirmed configuration | confirmed connections, the mapping between logical signals and real pads/channels, grounds and generation |
| test requirements | roles of DUT/peer/observer/control, features, connections, simultaneity, stimuli and expected values. Maintained by the test owner |
| run plan | what results from resolving, from the confirmed configuration, the roles and paths used this time, resource occupation, build/profile and contract pairs |

The probe's own USB transport and USB data between targets are different connections. RVSWD is a control path for writing, debugging and the DM console, and does not mean there is wiring for GPIO observation or USB data.

When the same target is used from separate paths, OEP and WCH-Link, make it one target node with two paths. To prevent double writing to the same unit, exclusivity is applied to the target unit as well as the port name.

Signal wires are not necessarily 1:1. An I2C bus, SPI's shared clock/data, and input-only capture branches are expressed as connections with multiple endpoints. Each wire's input/output/open-drain, shareability and simultaneous-use conditions are matched with the contracts.

## 2. Information needed to start discovery

Discovering the pad/channel mapping after wiring loosely is allowed. However, power, GND, signal voltage, the source of USB VBUS power, pins that must not be operated and power switches are assumed to be known in advance. These electrical conditions cannot be determined from searching data wires.

The initial settings have the identification of the probe to operate, the channels that may be searched, the target's candidates or expected kind, and the known power/connection range. If the target's UID is not yet known, it is obtained by discovery and associated at confirmation. If it cannot be obtained, an explicit manual association is recorded, and several units of the same type are not considered unique by chip family alone.

The probe's describe is used as information on features and allowed channels. The intersection of the candidate range and the declarations is taken, but the declarations are not treated as proof of real electrical wiring. Reserved pins, existing connections, plans, captures and resources in use by other operations are matched.

## 3. Discovery and confirmation procedure

1. Read the explicitly given candidate settings, and validate types, references, identification and the operation range. Take the locks on the related probes, targets, power and shared resources.
2. Record the probe's identity, firmware, protocol/interface revision, channels/ops/limits and the current resource state.
3. Classify the debug candidates, and search RVSWD/SWIO/SWD within the allowed range. List all candidates and save pins, identifiers and trial conditions. Do not implicitly choose the first of several answers.
4. For each candidate, make the needed connection and check chip/UID and so on. If the association is ambiguous because of several units of the same type or an unknown UID, require an explicit choice or an additional identification step.
5. When writing a known diagnostic image to a target whose debug/control has been confirmed, make the write explicit, separate from discovery. Record the image's origin and hash, the handling of the original image, and the end state.
6. Map the needed GPIO/UART and so on to logical signals. Match an identification pattern per target pad with the probe channels on the independent observing side. When needed, also stimulate in the reverse direction to tell apart candidates, branches, shorts and direction.
7. Confirm connections that cannot be discovered, such as USB cables and power, with input information and independent checks. Do not assign a USB peer just because debug is connected.
8. Excluding ambiguous mappings, save the confirmed configuration as a new generation. If needed, generate the probe's settings (such as `channel.<pin>.label` of core §13) as derived settings, apply them explicitly, then match them with the probe's values.
9. Show the configuration after the change and the state of the diagnostic image, and release the locks and outputs. Discovery failures are also kept as artifacts.

The GPIO identification pattern is not "drive any pads all at once". It follows the conditions of the features/pads included in the candidates, in an order that does not conflict with other outputs. Signals that are not observed are left as not connected / not confirmed, and wiring that is not required is not demanded.

The current Python pins discovery centers on debug/reset, and does not confirm a general mapping between GPIO target pads and channels. Therefore, debug discovery and diagnosis of feature wiring are separate contracts and implementation steps. The physical characteristics of USB or analog are not inferred from GPIO discovery results.

Avoid circular dependencies in choosing diagnostic firmware. The basis for confirming wiring is built from a known diagnostic image and independent observation, and does not assume only that the Arduino API under verification works correctly. When doing additional checks with the firmware under verification, its failures are not classified as missing equipment without grounds.

## 4. Validity of confirmed results

The confirmed configuration has the probe and target identities, the discovery tool, the diagnostic image, the probe's declarations, connections and signal map, date and time, the confirmation method and the configuration generation/hash. What was declared, what a person entered and what was observed are told apart.

Replacing a probe, changing wiring, replacing a target, or changing power/USB connections returns the related connections to unconfirmed. When the probe firmware/profile changes, channels/ops/limits and saved settings are matched again, and the affected feature wiring is re-checked. Neither is everything re-discovered unconditionally, nor is it considered valid by the firmware string alone.

Software alone cannot fully detect arbitrary manual wiring changes. After an equipment change, the generation is updated explicitly, and before a run a light check of identity and the needed connections is done. The connection check is not a new search; it checks that the confirmed peer and mapping still hold.

If the check fails, runs with that configuration stop. Do not search another candidate during a normal test and join the results. Only contracts that verify dynamic reconnection/scan itself have the test side specify the target set and expected behavior.

## 5. Resolving roles and the matrix

The unit of selection is not a single unit but an assignment of roles that can run a contract.

| Example contract | Assignment needed |
|---|---|
| probe saved settings | probe, and a channel for settings tests if needed. No target needed |
| ch32rv flash | target, debug path, probe/backend, path for readback and checking execution |
| Arduino PWM | DUT, a PWM-capable pad, capture/measuring instrument, confirmed observation wiring |
| USB host | DUT host, peer device, independent control of both sides, physical USB link, VBUS |
| USB device | DUT device, peer host, independent control of both sides, physical USB link, VBUS |
| separation of multiple targets | target A/B, the same probe, the needed simultaneous capability for connections and console |

Tests define logical roles such as `dut` and `peer` and requirements, and do not fix arbitrary names on the equipment side. Pairs that satisfy the conditions are enumerated from the set of confirmed units and connections in the TOML. The product of multiple probes/targets and USB pairs that are not connected are not generated.

An assignment where one probe controls multiple targets and an assignment where multiple probes control both sides can be used for the same contract. However, they must meet the simultaneity and observation capability the test requires. The host/device of each test is decided at run time, including the build profile.

During development, units, connections and contracts can be narrowed; at release, the target differences are chosen. An entry point equivalent to `plan` shows, without operating anything, assignments, unconfirmed items, lacking capabilities, not implemented items, resource sharing and run order. A preflight that reads whether units exist and their capabilities is stated separately.

Contracts of an implementation declared as supported are not removed from the matrix because a candidate firmware forgot to output ops. The equipment's physical capability, the implementation's adopted scope and the declarations observed this time are kept apart, and missing required declarations are checked as that implementation's failure.

## 6. Exclusivity and run order

Exclusivity covers probes, targets, shared power, USB switches and measuring instruments. Another backend to the same target, another transport of the same probe, and the group of devices affected by the same hub power conflict even under different names.

Use the host-wide lock passed in the settings, and serialize all hardware operations. The meaning of the lock is a general public contract, not computed from the location of a private repository.

Targets A/B on the same probe share the probe's OEP session. The lifecycle per pytest DUT/peer and ownership of the probe's transport/session are separate layers, and the same probe is not opened exclusively twice. Writing is serialized if needed, and communication starts after both sides are READY. One side ending does not discard the other's stream/session.

A power operation does not necessarily affect only the DUT on that port. Include the affected targets of an operation in the configuration, and match the reset the contract intends with the actual effect. Disconnecting USB data, disconnecting the probe's transport and cutting the DUT's power are told apart.

## 7. Files and environment variables

The side that owns hardware tests has the general input schema, loading and validation, role resolution, tests and templates. An inventory or generator held by the equipment administrator only passes settings following this schema; it is not a dependency that public tests import, start as a subprocess, or search for as a checkout. Another person writing by hand can make the same configuration.

```text
tests/hw/
  .env.example                   public variable names and descriptions
  hardware.example.toml          public template of equipment input
  hardware.resolved.example.toml public template of the confirmed configuration
  .env                           local run settings
  hardware.local.toml             initial units, discovery range, known wiring
  hardware.resolved.local.toml    confirmed connections. Generated or filled in explicitly
```

`.env` and `*.local.toml` are kept out of Git. The real file names and output locations can change, but the distinction between templates and local settings is kept. The confirmed configuration is self-contained and does not require including the initial input file. An environment where all wiring is filled in by hand is also validated and output in the same confirmed format.

Environment variables:

```dotenv
# Run from tests/hw/.
OEP_HW_CONFIG=./hardware.resolved.local.toml
OEP_HW_RESULTS=./.pytest-results
```

`.env` is passed explicitly with `uv run --env-file .env ...`. Automatic loading is not implemented. Config reads only the file given by this variable or the explicit CLI, and does not search sibling directories or the home directory. When the CLI has the same item, the CLI wins. The priority order of the Arduino plugin's primary/peer port/profile stays as it is, and the resolved paths are passed from one place.

A relative `OEP_HW_CONFIG` is relative to the working directory at run time, and this repository's templates assume running from `tests/hw/`. Relative paths inside the TOML are relative to the TOML's location. The resolved absolute paths are kept in the log. In CI, no `.env` is placed, and the same values can be passed through environment variables or the CLI.

Large per-device channel overrides are not listed in `.env`. Units and wiring are held in TOML, and build conditions and expected values by the test owner. When a secret is needed, pass it separately from the environment, and do not include it in generated TOML or artifacts.

Choose one self-contained TOML per host or use. There is no include of multiple files, implicit search or merging of arrays. When things grow, splitting can be done on the private inventory side, and the export passed to the public side can stay one file.

## 8. Generation and updates

Normally, copy `.env.example` and the TOML templates and fill them in. In an environment with an equipment inventory, generate the initial settings, go through discovery and confirmation, and output the confirmed settings and `.env` for the runner. Track the schema version, the generating source's revision and the confirmed generation/hash.

An existing `.env` is not overwritten unconditionally. Generation names the output location explicitly, and checking the diff and replacing are separate steps. Ownership of the run targets the user specifies and of the signal map the machine confirmed is kept apart. Updating derived settings does not erase hand-edited secrets or run options.

Regeneration only creates files; it does no firmware update, settings save or power operation. Those are handled as explicit separate processing. Normal client/Core tests use installed probes. After a probe update, re-check the affected confirmed information and make a new export.
