# Conformance checks from the SPEC

[日本語](conformance-checks.ja.md)

The checkers read the given SPEC checkout and do not build expected values from the implementation's registry. The checkers are pinned to the body text they reviewed ([Conformance checks and SPEC versions](conformance-spec-compatibility.md)).

Check results are split into the following 3 stages.

| Stage | Application to all implementations | Contracts checked |
|---|---|---|
| core | all OEP endpoints | transport, request/response, refusal order, resend, session, time, resource lifetime |
| interface | all interfaces including custom extensions | name/revision/instance, describe/ops, TLVs, common rules for resources, subscriptions and paging. Checks what applies to each feature |
| oep-interface | the standard OEP interfaces implemented | each op's behavior, side effects, races and abnormal cases. Reused as a template for tests of custom extensions |

`oep-conformance` runs part of the first 2 stages. A PASS means success of only the items run, not full conformance. In the JSON, `full_conformance` is false, and `levels` and `unchecked` keep the scope. OEP-specific behavior checks are not run.

## Running

Copy `tests/equipment/.env.example` and load it explicitly. The settings are a stable port / named USB/TCP, the expected unit ID, the SPEC checkout, the shared lock and a new JSON output location. The SPEC is not searched for automatically in neighboring checkouts. There is no dependence on a private equipment inventory. Wiring and target specification are not needed for this core check. `OEP_CONFORMANCE_FRAMING=serial` uses the independent COBS/CRC checker and adds wire checks and reconnection checks to the message checks. `tcp` uses an independent length checker on the explicit `tcp://HOST:PORT` and adds TCP checks to the message checks. DNS-SD and neighboring settings are not searched automatically. `client` uses the normal client backend and can run the message checks over USB/TCP too.

```sh
uv run --group dev --env-file tests/equipment/.env pytest tests/equipment/test_core_contract.py
# The CLI entry point of the same checker
uv run --env-file tests/equipment/.env oep-conformance
```

Prepare the parent directory of the output location first. Result files are not overwritten. The shared lock is held from connection to close, and its own session is ended. force checks only handover between sessions the same checker took. If the first open is refused, no force is sent. Another user's session is not taken. No pin operations, settings changes or flashing are done. An implementation that does not match the spec is recorded as FAIL.

## Core-only virtual bench

Giving `--profile core-v1` to `virtual_bench_serve` starts a minimal core-only endpoint. This profile has only fn 0, with no target/fixture/interface/resource/subscription. It is not treated as PASSing the common interface or OEP-specific ops.

```sh
uv run python -m oep_client.virtual_bench_serve --pty --profile core-v1
# Or TCP. Use the shown PORT explicitly.
uv run python -m oep_client.virtual_bench_serve --tcp 0 --profile core-v1
```

Put the shown port and `OEP_CONFORMANCE_UNIT_ID=virtual-core-1` in the explicit settings. The required `tests/test_virtual_core.py` uses the same public checker over raw messages, a real PTY and loopback TCP, and manages the child process until it ends. Normal tests do not open hardware.

## Checks with an explicit TCP peer

On equipment that accepts 2 connections at once, add `OEP_CONFORMANCE_TCP_PEER=tcp://HOST:PORT` (`--tcp-peer` on the CLI). The address of the same listener is fine. When another listener is given, the same unit, boot and core/interface declarations are matched too. DNS-SD is not used to search for candidates. Without the setting, multiple connections remain unchecked. If it is set but cannot connect, or the unit differs, it is FAIL, not a skip or a fallback to a single connection. A simultaneous connection count of 2 is not required of all TCP implementations.

The added checks are peer identification, the common lock/owner and refusal of other sessions, the destination of responses, separation of partial frames per connection, the lock kept after one side closes, and primary kept when peer is cut off for an oversized length. The no-response observation for leaked responses is 100 ms, and the wait for a partial header 300 ms. The JSON keeps the raw bytes and observations of primary/peer.

After close, the lock is observed from peer on session 0, and primary is reopened and matched again. Requests of the original session S are not moved to the new connection; only when its own owner is confirmed does it force with another session T, and T is ended on the same new connection. If identification, boot, owner or takeover is uncertain, later items stop, and cleanup of the closed connection's session is left to the lease. Moving the same session's TCP transport and resending are checked by CORE-RECONNECT-* (session, replay, ended, lease, unread), which reopens primary's connection.

## Raw USB and the software model

For bulk/HID there is an independent raw adapter API, a minimal USB model, and `usb_hw`, which opens hardware (including descriptor checks and checks of the end of IN transfers) ([USB check guide](usb-conformance.md)). USB `framing=client` of the normal CLI is message checks only, and success of the model is not applied to hardware.

## Current granularity and limits

| Item | Check |
|---|---|
| confirm | magic/revision, fixed part, frame/window/inflight, transport, boot ID |
| discovery | unit matching, list count/boundaries/duplicates, describe paging/end/invariance, the shape of ops, transport support, max_op_ms, the frame limit of describe TLVs, discovery while locked and history after confirm |
| headers/TLV | result role/corr/resolution/detail/length, unknown response TLVs after the fixed part, refusal priority, short confirm / magic / revision range, short / over-length / ID 0 (including critical) / unknown critical and non-critical request TLVs |
| session | keepalive, lock refusal of other sessions, lease lower/upper bound / default, duplicate owner and open with the same ID, natural expiry, the effect of header refusal and payload refusal on the lease, force only between sessions it took itself |
| replay | a corr that is not 1 to 32767 ahead of the last number is result_lost even if not executed, keeping refusal results, refusal of the same corr with a fixed payload, priority of header checks and resend checks, an exactly matching result or result_lost, refusal of a changed request with the same corr, history after open with the same ID, resend after end, no revival by open resend, no lease extension by resend |
| u16 | advancing by less than half a cycle, independence of session 0, wrap from 65535 to 1 |
| serial wire | splitting at every byte boundary, joining unknown-role frames with requests, CRC/COBS corruption, no response to short requests / unknown role, recovery at the delimiter after a midway cut, no response to oversized frames and recovery after the gap. Raw sent/received data, decode results and observation time are also saved |
| TCP wire | byte splitting and joining of length, length 0, no response to short requests / unknown role, keeping the frame even across a midway wait beyond the frame gap, disconnect on oversized length. Raw bytes and close/reset are saved |
| TCP peer (when set explicitly) | same-unit matching before connecting, common lock/owner, separation of responses by connection, separation of partial headers, lock kept after primary disconnects, takeover only of its own session, isolation of the peer disconnect fault from primary |
| reconnect (serial) | Closes the OS port, opens the same port, and matches confirm/unit/boot again. Checks the session and lock, resend history, history of ended sessions and lease expiry while closed. A restart or unit mismatch is FAIL and stops later items, and end is not sent to a different endpoint |
| interface common | describe/ops/invariance/instance checks of each fn independently. The same checks for custom interfaces |

Each item saves the clause, PASS/FAIL/BLOCKED, request and response hex, and elapsed time. Even if decoding/reading the wire itself fails, that run's raw sent/received data and the error are saved. Past wire records are not copied into that failure. After a timeout / transport disconnect or cleanup failure, recovery is not hidden; it is BLOCKED, and the whole run is FAIL. Only the optional pytest hardware entry point with no settings at all is SKIP. When TCP's max_frame is 65535, a larger length cannot be sent in u16, so only the oversized length item is not_applicable, and the reason is kept in the pytest skip. This is told apart from skips for missing equipment. Incomplete settings, missing access rights, a different unit and spec violations are FAIL.

`result_lost` is accepted with the retention size limit the spec allows taken into account. When an old clock in the history is resent, returning completed with a new clock value fails as re-execution. The cache capacity is not assumed to be an implementation-specific fixed value.

Multiple targets and the behavior of each op are not checked (pin release is checked electrically on hardware by `python -m oep_client.conformance.release_hw`, with the target's GPIO inputs). For serial, the client's encoder/decoder are not used; the CRC is checked with the standard library, and COBS and reception with separate code. The client is used only for OS port exclusivity and opening/closing the port. Reconnection is OS port close/open and does not simulate physical USB unplugging, power loss or USB reset. If DTR/RTS or the bridge wiring causes a restart, it is recorded as a boot-change FAIL, and whether the session was kept is not settled. Duplicate response checks are limited to an observation window of 30 ms after the result, and no response to oversized frames to an observation window of the declared frame gap + 100 ms. TCP uses independent length reception and sending, and after a broken response it stops rather than hiding it by reframing. With framing=client the independent wire checks are not run. Until these are filled in, do not claim full core compliance or full interface compliance.

`oep-hardware preflight` is a separate contract limited to unit, declarations and open/end. Success of portable C++, shared vectors and virtual tests is also reported separately from hardware conformance.

The registry/vectors are pinned by `tests/vectors/SPEC_COMMIT` and `SPEC_SHA256`, and normal tests check that snapshot. With an explicit `OEP_SPEC_DIR`, the upstream content of the same commit is also compared. When the SPEC's HEAD moves ahead, it is not copied automatically. Update with `OEP_SPEC_DIR=/explicit/path OEP_SPEC_REF=<commit> tools/sync_registry.sh`.

Release of resources by force is checked on hardware by `python -m oep_client.conformance.force_hw`, with a debug connection to the target (the taken session's connection disappears, its number becomes no_resource, and the next connection of the taking side gets a new, larger number). For resource lifetime there are [supplementary checks with an explicit adapter](resource-conformance.md). They are not added to the normal CLI automatically, and subscriptions, route disconnect, dependency order and electrical idle remain unchecked.

Subscriptions and notifications are checked on hardware by `python -m oep_client.conformance.notify_hw`, with streaming logic capture on one named wire (lock, seq and position, response priority, replacement, max_delay_ms, transport, stopping on end / force). For the lifetime of subscriptions and events there are [supplementary checks with an external stimulus adapter](subscription-conformance.md). Raw notifications and observation windows are saved. data, response priority, route and queue limits are not checked, and they are not added to the normal CLI automatically.

[Supplementary checks of data/event and batching](data-conformance.md). With external byte positions and loss-free stimulus conditions stated explicitly, it checks thresholds, first-byte delay, the shared seq, splitting, and keeping unsent data on resend / same-session open. The seq wrap is verified in unit tests by actually generating a full cycle, kept separate from real-clock and hardware results.

[Supplementary checks of transports and writers](route-conformance.md). It checks the transport of responses/notifications, priority, the queue limit of unsent suffixes, the seq of drops, and keeping session/resources/subscriptions/history after a disconnect. Real framing/OS buffers, per-transport window/max_inflight, concurrent execution and hardware disconnects are not checked.

[Supplementary checks of per-transport acceptance and recovery](pipeline-conformance.md). It separates count/byte boundaries and checks the independence of separate connections and recovery after the response is sent. Over-limit refusal is optional in the SPEC and is not added as a required condition for general hardware.

[Session resend and refusal cache under congestion](replay-pressure-conformance.md). It applies the resend/header/session decisions before congestion, and checks that resends of refusal results and peer's session 0 queries do not break the shared history. S is fixed to primary, and it also checks that force with another ID T replaces the shared cache.

[Response sending and lease](lease-conformance.md). Renewed at response generation (core §6.1), not renewed on waiting to send / partial send / send completion / resend / earlier-stage refusal. It checks expiry while waiting to send, and revival/wrong renewal by old responses after end/force. Physical send completion is not checked.

[Retention limits of the resend cache](retention-conformance.md). It checks the boundaries for each of request/response, missing markers, eviction and old unused corr, and retention across refusal/end/same-session open. If the request is kept, identity is compared first, and malformed for a changed request is told apart from result_lost for a missing response.

[Missing retention and session/boot lifetime](retention-lifecycle-conformance.md). Expiry keeps the history, a different session replaces it, and a new boot invalidates history/resources/pending sends. Only an explicit logical-model reset is allowed, and physical equipment is not restarted.

[Boundaries of corr u16](corr-conformance.md). It checks advancing of the wrapping number, refusal at exactly half a cycle and beyond, the wrap from 65535 to 1, refusal of numbers half a cycle or more behind, independence of SID 0, and end after a full cycle and history replacement by another session.

[Re-reading the state after result_lost](recovery-conformance.md). It checks that a sample host confirms boot/session and the current resources from the wire only, and does not re-run a state-changing request with an unknown result with a new corr. The original outcome stays unknown.

[force during recovery and per-transport unknown results](recovery-races-conformance.md). It detects session replacement before / during / at the end of reads, and under the 3 conditions of all responses lost, only the first received and none executed, makes only that transport's unresolved requests unknown.

corr wraps (core §4.1, §5.1). Wrapping, half cycles and dropping records of corr are checked by WRAP/HALF-GAPS/WRAP-END of the [corr checks](corr-conformance.md).

[Recovery from lost results of end/open](session-loss-conformance.md). It resends the same request and also checks the current session state. Even if a saved open success is obtained, use does not resume if it has expired. Success or the lease value is not inferred from result_lost.

[Abandoning several pendings when numbers run out, and recovery on a new transport](exhausted-conformance.md) runs under 3 conditions. The old S is not moved; after the release is confirmed on SID 0, it opens with another ID. The unknown of old requests and the received results are kept.

[Declaration checks of all interfaces](interface-conformance.md). It reports separately name labels, ops, fixed widths of common tags, channels, paging at each position, invariance before and after a session, undeclared ops and instance. Adoption of standard ops or common parts is not inferred from names.

[Positioned streams with explicit adoption](stream-conformance.md). It checks read's non-consumption, u64 positions and gap, and marks' inclusive paging, eviction, u32 wrap and time. It is limited to persistent-fn conditions and not applied automatically to resource-type streams or capture.

[Write acceptance and debug completion results with explicit adoption](outcome-conformance.md). It checks accepted/done and success/partial/failed, the failure classification of unknown status, and no duplicate side effects on resend. The custom sample's capacity and payloads are stated explicitly, kept apart from physical delivery and conformance of OEP-specific debug ops.

[Lifetime of resource-addressed streams and debug connections](dependency-conformance.md). It checks the dependency order of release, resource addressing, buffer/queue retention, session end and historical replay after release. It is kept apart from the mechanism constraints of the standard console and physical release.

[Specific declarations of standard wire / console](oep-declaration-conformance.md). It reports standard required ops, specific describe and SWIO clock in the third stage `oep-interface`, kept apart from the results of common compliance and op behavior.

[Standard wire attach and console open](attach-conformance.md). It checks critical TLVs, refusal before side effects, mechanism/resource constraints and resend in the third stage. Logical target access is kept apart from electrical bring-up/delivery.

[Finite scan of standard wires](scan-conformance.md). It checks searching from the start of explicit candidates, pre-validation of all candidates, pin release, the basis of identifiers and resend in the third stage. Limited to a logical fixture that explicitly provides the optional scan.

[SDI/DMDATA mailbox of the standard console](mailbox-conformance.md). It checks frame boundaries, DATA0/DATA1 order, the sender queue, zero-length SDI write and stopping during halt/debug in the third stage. dmseq, arbitration with real debug requests and physical delivery are not checked.

[Probe-side dmseq framing](dmseq-conformance.md). It checks CRC, sequence numbers, SYN/TO, pending resend and ack, detected target reset, independence between connections and close in the third stage. Target-side timeout and physical delivery are not checked.

[Target-side dmseq](dmseq-target-conformance.md). It checks resend, duplicate suppression of input H / lack of space, timeout and late acks with a logical clock. It also checks a mailbox round trip with an independent probe, kept apart from real-time lower bounds and firmware integration.

[riscv-dm dmi and console arbitration](debug-conformance.md). It checks dmi syntax, progress and poll, optional halt/resume, and mailbox stop/return on the same connection in the third stage. The common declarations of all 5 fns including the added fn are checked separately in the second stage.

[Optional block and state restoration](block-conformance.md). It checks length, alignment, hart preconditions, bus values/progress, restoration of GPR/DATA/abstractauto and console retention in the third stage. Physical abstract commands and restore failures are not checked.

[Optional step and re-halt](step-conformance.md). It checks PC/moved on success and failure, keeping prv and DATA, re-halt and step_left, console arbitration and resend in the third stage. Physical instructions, CSR operations and waiting times are not checked.

[Optional reset and DM return](reset-conformance.md). It checks the success conditions per mode, waiting for the DM response, havereset, reset/restart marks and dmseq resynchronization in the third stage. Physical ndmreset and the procedure for confirming execution are not checked.

[Optional run and the loader state](run-conformance.md). It checks stopped/status/PC/values and elapsed time, keeping host changes, scratch restoration, up-front refusal and bus values after the program in the third stage. Physical CPU execution and real time are not checked.

[Wire attach options](wire-options-conformance.md). It checks reset channel/hold/conflicts and DM return, idle_clock, SWD targetsel identity and pin release on detach/end in the third stage. scan options and electrical conformance are not checked.

[scan idle_clock/targetsel](scan-options-conformance.md). It checks a single explicit targetsel, pre-checking all candidates, idle levels during the search and per-candidate pin release in the third stage. Physical waveforms/timing are not checked.

The [logical checks](retry-conformance.md) of wire retries check separately no resend after ambiguous delivery of host DMI,
busy, probe-internal reads, and releasing the drive after failure and returning to normal idle.
Release on a broken wire after the response and physical waveforms are not checked.

The [logical checks](cut-conformance.md) of broken wires check release of dependent resources after the request's response is written,
console link-lost/closed, history clearing after success and keeping other connections.
Real transport delivery and writer concurrency are not checked.

[Writer lease checks of standard resources](writer-lease-conformance.md).
It checks stopping during execution, restart at send, expiry while waiting for the writer and prevention of ownership revival by old tokens
with an explicit logical clock. This fixture's common declarations and cut checks are split into the second/third stages.

[Checks of registered standard ops](registered-ops-conformance.md) checks the required ops and optional op pairs of the explicit standard interfaces
in the third stage. It does not call real behavior, and a declaration-only fixture is not treated as a complete probe.
Required describe metadata and specific payloads/side effects need additional checks.

[Selected describe metadata](selected-metadata-conformance.md) selects the restart/RISC-V/ARM-ADI fns.
It checks required tags, fixed widths, block's max_length, and that ARM-ADI's features have no bits.
Other metadata and real behavior of advertised ops need separate checks.

[Fixture-family describe metadata](fixture-metadata-conformance.md) checks GPIO/UART/I2C/SPI given explicitly.
It checks the GPIO input mode, UART formats and TX role, I2C/SPI fixed shapes and frame limits, SPI modes, and the conditions of optional stretch/cs_setup.
The behavior of advertised ops and pin/plan lifetime need separate checks.

[capture describe](capture-metadata-conformance.md) states the logic/analog/capture-group fns explicitly and checks the shape of repeated metadata,
the shape of features, and the shapes of mode, rate and trigger. The real behavior of ops is needed separately.

[GPIO and plan behavior](gpio-conformance.md) checks, with an explicit fixture, up-front refusal, per-fn atomic replacement, driving order,
set/read, resend and idle restoration in the third stage. Physical pins / other fixtures / persistent config are not checked.

[Core lifetime with a GPIO plan](gpio-lifecycle-conformance.md).
With an explicit clock, it checks expiry, force, open with the same ID, resend history, lease renewal / no renewal and pin/plan release.

[UART-specific behavior](uart-conformance.md).
It checks configure, receive/send, TX idle, resend, plan re-creation and end/expiry/force.
[Applying the common stream to UART](uart-stream-conformance.md) uses the stream checks' asserts fitted to the plan-owned lifetime.

It also detects resync/TO/CRC marks per console §2 and false notices of invalid flags. The number of marks for repeated observations is not fixed.

[I2C target specific checks](i2c-conformance.md).
It checks RX/TX FIFO, ACK, overflow, stretch/configure, no duplicates on resend and plan lifetime with 4 configurations.

[SPI target specific checks](spi-conformance.md).
It checks mode/bit order/CS, partial bytes, arm/overflow/FIFO, resend and plan lifetime including while CS is active with 2 configurations.

[Logic capture specific checks](logic-conformance.md).
It checks query/configure, one-shot/repeat, generations, read/segments/release, short/error and plan lifetime with an explicit sample.

[Checks of analog success payloads](analog-payload-conformance.md).
It checks layout / actual values / scale / skew / frontend / reference, calibration and known raw samples independently.
Conformance of execution, refusal, generations, resource lifetime, notifications and all analog operations is not checked.
