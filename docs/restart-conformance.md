# Partial conformance checks of Probe restart

[日本語](restart-conformance.ja.md)

RestartChecks, based on restart §1/2 and core §9, sends operations to RestartModel, which uses the current Core and GPIO/plan.
The model's restart uses Endpoint.reboot and switches boot/clock/Core/extension resources to a new boot.
Checks is built from the explicit SPEC registry and runs the common InterfaceChecks, RegisteredOpsChecks, then the specific checks.
The public parts do not search for private benches or equipment automatically.

SampleRestartAdapter provides explicit write completion, a logical clock, and raw delivery to 3 transports.
The context checks restart op16 and restart_max_ms50.
restart_delay_ms50 is the actual restart time by which this virtual fixture delays, and it is stated as a control condition.
The SPEC's restart_max_ms is an upper bound and does not mean a general Probe must keep silent for 49 ms.
It is not a hardware checker to be used as is on a Probe that restarts immediately.

- BARRIER: seeds a GPIO high-output plan and checks that boot/ownership/pins/plan do not change while restart's response buffer is prepared and during the 9 ms write wait. After the success response has been written in the old boot, the restart interval begins.
- LOCK: SID0 is session_required, another SID is locked. Refusal does not change the boot/restart/resources/holder.
- TLV: number 0 and truncated TLVs are malformed. Unknown critical 255 is unsupported and echoed as is. Unknown optional 127 is ignored and restart proceeds normally.

During the restart interval, on the 3 logical transports UART bridge/vendor bulk/TCP, confirm and the same restart request get no answer.
The 49/50 ms boundary from write completion is used as a fixture condition; at 50 ms, all transports answer confirm in the new boot.
In the new boot, the clock is 0, and holder/plan/pin driving/cache are empty. The same corr/all bytes of the old restart also get no_session, and the old success is not returned.
The raw control requests/replies and write/quiet/boot events are stored separately from the normal Checks trace.
It does not measure the timing of physical framing, USB enumeration or TCP re-listening.

Run it with `uv run pytest -q tests/test_conformance_restart.py`.
It includes the normal case, common declarations, wire faults, state faults, clock success and boot faults after restart, missing control, and the fixture's delay conditions.
It injects and detects restart before the response, missing restart, boot reuse, leftover plan/pin/cache/holder, a clock not reset,
responses during restart, missing transport return, a rewritten critical tag and pin changes on refusal.
On failure, cleanup of pending writes does not hide the original violation behind a separate cleanup refusal.

The target's halt/reset state, saved/unsaved settings and fallback, port_speed's boot baud, notification suppression and truly concurrent transports,
and conformance of physical re-enumeration are not proven. The report has full_conformance=false.
