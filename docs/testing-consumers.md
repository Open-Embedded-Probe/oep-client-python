# Test guide for projects that use OEP

[日本語](testing-consumers.ja.md)

This is a guide for projects that use OEP probes to verify their own clients, tools or embedded software. It does not restrict the language, target family, or whether Arduino is used. The tests themselves and their pass criteria are managed by the consuming project, which normally uses installed probes.

The authoritative source for responsibilities and guaranteed scope is the [overall testing policy](testing-policy.md). For the equipment settings format, see the [hardware settings format](hardware-schema.md) and the [procedure for fixing the wiring](hardware-configuration.md). `oep-hardware` does settings checks and planning, virtual smoke, and standalone probe preflight. Running target tests is the job of the consuming project's runner, which applies the following responsibilities and verdicts.

## 1. What your own project guarantees

| Use | What the consumer checks | What the probe provider checks |
|---|---|---|
| Implementing an OEP client | framing, requests/responses, handling of declarations, timeout, resend, session recovery | protocol and declaration conformance, behavior of real transports |
| Using an existing client | the chosen transport, how the client is called, error handling, resource release | the client's internal communication and compatibility decisions |
| Making a flash/debug tool | target identification, target-specific processing, readback, running/debug, console coexistence | the features wire/DM provide, separation of resources and state |
| Making a target driver/API | target behavior, external signals, faults and recovery, communication with an independent peer | the features fixture/capture provide and the measurement conditions |
| Making several targets work together | preparing both sides, roles, communication results, end handling when one side fails | connection/stream limits and separation, shared session |

The consumer also verifies the combination with installed probes. Passing the provider's tests alone does not claim success of your own combination. On the other hand, there is no need to build pass/fail of firmware updates into normal consumer tests. Evaluating an update is treated as a separate verification that names the target image and the changes.

## 2. Moving from the virtual environment to hardware

1. Check inputs, expected values, boundaries and error handling with unit checks that need no board.
2. With a virtual probe, check OEP requests/responses, timeout, disconnect/restart, resource conflicts and cleanup. State the scope that the virtual probe/target can guarantee.
3. Check the equipment, and with installed probes and a single target, check unit identification, writing/readback, running, and the control/log paths.
4. Check the target's API with independent stimulus and observation. GPIO is verified with external input/output, UART with an independent peer, timing accuracy with an independent clock.
5. Add the contracts for the needed peers, load, faults/recovery and multiple targets. The owning side decides the targets and difference axes needed for a release.

Pin the virtual probe's version and dependencies in the consuming project's lock/CI, and state how to get them. Do not search automatically in unspecified sibling checkouts or the home directory. If a required virtual check has no dependency, treat it as a preparation error; do not return from the test function to make it a success.

To verify core requests, sessions, resend and framing, you can use `core-v1` from the [conformance check entry point](conformance-checks.md). It is a minimal profile with only fn 0. Consumer tests that use interfaces such as flash/debug choose a profile with a target that has those interfaces, and record its SPEC version.

Success in the virtual environment does not guarantee voltage, wiring, real-time accuracy or real USB quality. Also, a virtual probe from the same implementation family as your own client alone may miss shared mistakes. Combine expected values from the norms, independent implementations and hardware.

## 3. Isolating equipment settings from tests

Test code declares roles such as `dut` / `peer` / `observer` and the needed features and connections. Unit IDs, serial ports, channels and target pads are not fixed in code. When equipment is replaced, update the settings and the confirmed records.

Copy the public templates to create `.env` and `*.local.toml`, and keep them out of Git. Pass `.env` explicitly, as in `uv run --env-file .env ...`, and put units and wiring together in TOML. The main settings are these 4.

| Setting | Use |
|---|---|
| `OEP_HW_INPUT` | units, electrical conditions, the search range allowed to operate |
| `OEP_HW_CONFIG` | the confirmed configuration used in normal tests |
| `OEP_HW_RESULTS` | where this run's evidence is saved |
| `OEP_HW_LOCK` | the path of the lock shared by equipment users |

Equipment information may be filled in by hand or generated by any management tool. What the consumer depends on is the settings format, not a generator or an equipment inventory. A generated `.env` does not overwrite existing secrets or hand edits wholesale.

When a runner uses its own TOML or environment variables, use those corresponding settings. Do not implicitly mix this format with your own settings.

## 4. Running after checking the wiring

Check power, GND, signal voltage, forbidden pins and the search range first. Searching for debug candidates and diagnosing GPIO/UART pad mapping are separate steps. If the target's UID cannot be obtained, keep the basis for manual identification, and do not treat the chip family as unit identification.

Save all candidates, and do not pick the first of several candidates. Processing that writes a diagnostic image is stated separately. When wiring, probe, target or power changes, re-check the affected connections and update the configuration's generation.

Normal test collection and offline plan do not open ports. Hardware runs hold the common lock and make one occupied interval from the preflight of unit, declarations and needed connections to cleanup. On a mismatch, stop; do not look for another target during the test and continue.

## 5. Using several targets and peers

Separate the communication under test from the paths for writing, control and logs. For example, when testing USB host/device, use a configuration where both sides' states can be obtained from independent control even if USB data fails. If RVSWD and the DM console give the needed control, do not make the peer's UART wiring uniformly required.

A DUT/peer on the same probe share one session and broker, and separate their per-target connections/streams. Do not use a configuration where independent fixtures contend in separate sessions. Do not assign a platform that does not meet the needed simultaneity to that contract.

After both sides' uploads finish, match a re-queryable READY, build ID and role, then start communication. Use a run ID or challenge so that an earlier boot log is not mistaken for this run's success. Even when one side fails, save both sides' logs and end states.

## 6. Recording versions used and evidence

For each run, record the project's commit/dirty, contract ID, build profile, the versions of tool/client/virtual probe/hardware probe/peer used, the SPEC version implemented, protocol/interface revision, describe, image hash, unit, configuration generation/hash, roles, observations, verdict and end state. For information that cannot be obtained, record unknown and the reason. Do not guess a source commit from the firmware string.

Normal consumer tests define no numeric minimum probe firmware version. Record the version used, and the client/tool checks compatibility of the needed features and communication formats. Do not use the same version number string alone as grounds for compatibility. When a required feature of a candidate firmware is missing, treat it as a conformance failure in that adopted scope.

Tie DUT logs, readback, captures, both sides' READY and failure snapshots to the verdict. Keep both the original failure and the cleanup failure. If test images or settings remain, state it, and do not save credentials.

## 7. Telling skips from failures

| Situation | Handling |
|---|---|
| No equipment settings passed to an optional hardware suite | skip with a reason |
| Explicit settings / unit / connection absent, invalid or ambiguous | preparation/settings error |
| No equipment for an optional contract | skip with a reason. It stays unverified |
| Declarations / operations / READY of an adopted feature differ from expectations | FAIL. Not classified as missing equipment without grounds |
| A required release contract not run or skipped | release check incomplete |
| The contract itself not ready, implementation not complete | not ready / not implemented. Not read as PASS or out of scope |

The release decision is owned by your own project. If a needed contract does not hold, that release's check is incomplete. A failure of a shared integration test is not a reason to tie the releases of all related projects together uniformly.

## 8. Isolating defects and handing them to the provider

Isolate causes using the preflight of unit, wiring, power and declarations, an independent known image, and the results of running the same contract on another backend or another probe. Replacement need not be required for every defect. Hand the minimal reproducible conditions, expected values, versions used, logs and end states to the owning side concerned: client, probe or target implementation.

When asked to run regressions after a probe update, provide the test entry points and verdict criteria you own. The side doing the update handles the update work, its results, and checking the affected consumer paths. Normal consumer tests do not update probes automatically.
