# Checking port_speed on a UART bridge

[日本語](port-speed-conformance.ja.md)

PortSpeedChecks checks the Probe side's handshake, phase table and return timers of link §3 with explicit response write completion and a logical clock.
PortSpeedModel combines the current Core, GPIO/plan and link, and declares UART bridges 0/2 and TCP 1.
It declares port_speed (0x12) and port_state (0x13), and keeps a phase per port (0 boot, 1 trying, 2 decided, 3 return pending).
Boot is 115200, and the rates the fixture can make are 115200/230400/460800.
It does not open a physical UART, and is kept separate from conformance of a program in which a real Host switches baud.

It runs the common InterfaceChecks, RegisteredOpsChecks, then the specific checker.
It checks registry op18/19, the describe declarations of both ops, idle 3000 ms, a tolerance of 2% and the UART bridge declaration.
RegisteredOpsChecks checks that a fn with port_speed also has port_state.
The public checker selects the SPEC explicitly and does not automatically search for private benches or neighboring checkouts.

```python
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance.interfaces import InterfaceChecks
from oep_client.conformance.port_speed import PortSpeedChecks
from oep_client.conformance.port_speed_sample import SamplePortSpeedAdapter
from oep_client.conformance.port_speed_model import PortSpeedModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = PortSpeedModel(boot_id=17)
checks = Checks(model.handle, registry, "virtual-resource-1")
common = InterfaceChecks(checks).run()
report = PortSpeedChecks(checks, SamplePortSpeedAdapter(model)).run()
assert common["status"] == report["status"] == "passed"
# Save identity, report, checks.trace and the source/adapter versions to an explicit output location.
```

Each case does a logical restart and reads the new boot_id back, then creates a session and a GPIO high-output plan with a long lease.
Before and after the step request, it matches that plan/pin/idle/journal/ownership are unchanged, and saves the requests, responses and snapshots.
PortSpeedModel.begin only prepares the response buffer. finish stands for the point where that response has been written.
The adapter's `receive()` stands for the point where reception of the next frame starts. The following begin (or raw) finishes receiving that frame.
When a return condition holds during reception or writing, it becomes phase 3, and after writing that frame's response at the old baud it returns to the boot baud.
port_state is read with a new corr each time, and a past port_speed success response is not treated as the phase.
The checker reads the buffer's role/corr/resolution/payload with Checks, but this check operation is not taken as the time the Host received the response.
The prepared/sent/discard events and the baud and logical time at that point are recorded separately.

| case | Check |
|---|---|
| TRY-BARRIER | 115200 while the response buffer is prepared and during the 9 ms wait. After writing the response at the old baud, it changes to 230400 and starts timing verify. port_state is phase 1 |
| COMMIT | Decides after the trial; the response's actual baud and phase 2. step1/2 accept verify_ms 65535 and also 0 |
| REVERT-TRIAL / REVERT-COMMITTED | The response's baud is the boot 115200; until it has been written, the original 230400; after sending the response at the old baud it returns, phase 0 |
| STATES | Refusals from the phase table: step1/2 in phase 0, step0 and step1 with a different baud in phase 1, step0/1 in phase 2 are cause 6 with no state change. After each step, port_state reads the phase |
| VERIFY | At the 99/100 ms boundary the trial returns automatically, and port_state is phase 0 |
| TOLERANCE | Against the actual 230400, accepts the integer boundaries 235102/225883 and refuses the neighbors 235103/225882. Even for a request of 232000 the response's 230400 is used, and commit names the tried 232000 again |
| BOOT-RATE | step0 with 115200/113000/117000, which come to the boot speed or below, is unsupported (mark 0) |
| MALFORMED / TLV | Short fixed part, verify0 with step0, undefined step, echo of unknown critical, number 0, truncated TLV, ignoring unknown optional. Critical/optional TLVs of port_state are also checked |
| REPLAY | Resending the same try request 20 ms later does not change the speed, verify deadline or protected state again |
| IDLE | After deciding, it returns at 2999/3000 ms, and port_state is phase 0 |
| IDLE-RENEW | A normal confirm and its response renew the idle deadline, and the 2999/3000 ms boundary after renewal is checked |
| IDLE-INVALID | A short invalid frame gets no response, does not renew the idle deadline, and it returns at the original 3000 ms |
| END-BARRIER | Until the end response has been written, phase 3 at 230400; the response is sent at the old baud, then it returns to 115200 |
| NON-BRIDGE | Opens the session over TCP, and port_speed and port_state (SID0 / held SID) from the same TCP are refused with cause 6 |
| ONE-PORT | While port0 is in phase 1/2/3, step0/1/2 on another bridge are cause 6 without changing the peer, and port_state on another bridge is 115200/phase 0 |
| PENDING-VERIFY | A frame that crosses the verify boundary: port_state is phase 3, step0/1/2 from the owning session are also cause 6, and it returns after the response, including for invalid frames |
| PENDING-IDLE-OWNER | port_state/keepalive of the owning session across the idle boundary return after responding at the old baud, and step0/1/2 are cause 6. Phase 3 is not cancelled |
| PENDING-IDLE-OTHER | port_state or source on SID0, and invalid frames, also return after responding at the old baud while staying in phase 3 |

ONE-PORT is an **invalid Host stimulus to the Probe** that puts requests with the same owner ID into another bridge.
During phase 3, port0's reception is kept incomplete while frames from the other bridge are processed, and finally ended with an invalid frame.
A normal Host sends a session's requests on one transport. This is not a sample of Host transport switching.
In normal cases, commit/revert wait 20 ms. Since raw stimuli such as state/refusal are also included, conformance of all Hosts' wait/switch is not claimed.
step2 is tried only with valid bauds, and no new priority between a meaningless baud in step2 and the tolerance is defined.
The priority of requests where a phase-table refusal overlaps with tolerance / unsupported at or below the boot speed is not in the norm and is not checked (refusal stimuli use only valid bauds).
The stimulus that creates phase 3 is the reception of a single frame only; continuing simultaneous sending and receiving is not handled.
Concurrency that runs another request during begin is outside the adopted scope of this sequential fixture.

The unit tests are `uv run pytest -q tests/test_conformance_port_speed.py`.
They include the normal case, common declarations, wire faults, semantic faults, missing control and absence of port_state.
They inject and detect switching before the write, actual baud/state/send baud, early/missing timers, deadline renewal by resend, cause/tag, critical,
tolerance and boundaries, pin changes, acceptance by non-bridge / second port and peer changes on refusal, and wrong idle renewal.
They also inject and detect old phase/baud of port_state, acceptance from non-bridge, ignoring critical, accepting the boot speed, wrong baud on revert, refusing verify0 on step1,
refusing commit at the tried baud, acceptance on a second port during phase 2/3, missing phase 3 during end, immediate return without creating the pending state,
accepting steps while pending, cancelling a verify pending, switching midway through a frame, missing return at a boundary, cancellation by another session,
not cancelling in the owning session, and accepting step1 / refusing step2 after cancellation.
Fault injection replaces not only return values but also the actual send buffer.
Even when the check fails, the prepared write is completed before cleanup, and an error from an unfinished write does not hide the original violation.

A discard that sends no response does not renew idle (a model that renews idle on an invalid frame FAILs).
A count of successes alone is not taken as complete (full_conformance=false).
Long write waits / simultaneous requests / recovery from wrong responses, frames sent and received continuing at the same time, other resources and transport conditions, the Host's switch/wait, physical UART/production are not checked.

## Session release and late write completion

PortSpeedLifecycleChecks adds a context and partial checks of the following cases.
After the common declarations and PortSpeedChecks, it builds an independent model/Checks and runs with the same explicit SPEC registry.
Call `PortSpeedLifecycleChecks(checks, SamplePortSpeedAdapter(model)).run()` and save the trace and report.

- FORCE-TRIAL / FORCE-COMMITTED: force changes to a new holder and releases the plan. Until the response has been written, phase 3 at 230400; the response is sent at the old baud, then it returns to the boot baud.
- EXPIRY-TRIAL / EXPIRY-COMMITTED: at the 999/1000 ms boundary of lease 1000 ms, the holder, plan and speed-up are released. It uses conditions where the lease ends before trial verify 5000 ms and committed idle 3000 ms.
- EXPIRED-TRY-WRITE / EXPIRED-COMMIT-WRITE: after the response buffer is prepared, the lease ends while waiting for the write. Completion handling of the old response does not revive the speed, ownership or plan.
  While the commit response is being written, it keeps 230400 in phase 3, and returns after it has been written.
- EXPIRY-PENDING / -OLD / -OPEN: when the lease ends while receiving the next frame, phase 3. port_state on SID0 returns phase 3,
  port_speed from the old session gets no_session rather than cause 6 (the core decision comes first), and open of a new session does not cancel it either. All return to phase 0 after the response.

After each case returns, port_state on SID0 reads back 115200/phase 0.

In this fixture, request execution is instant, and write waits advance as idle time. Simultaneous inflight of force,
the clock stopping during execution, and physical send success when the lease ends during a pending write are not proven.
The normal force example is also a request that opens a new session, and the holder's existing session is not moved to another transport.
Cleanup on check failure uses the observed holder, and does not hide the original boundary violation behind a separate no_session refusal.

`uv run pytest -q tests/test_conformance_port_speed_lifecycle.py`.
It includes the normal case, response faults and state faults. It detects too-early return / missing return on force, early lease / missing release,
faults that revive trial/commit/holder with an old write, switching midway through the commit response,
immediate return during reception, cause 6 to the old session, cancelling the session pending / missing return, and missing phase 3 on force.

## Clocks during request execution and write waits

PortSpeedExecutionChecks and SamplePortSpeedExecutionAdapter run with PortSpeedExecutionModel.
Checks is built with the registry loaded from the explicit SPEC and run after InterfaceChecks.
A declaration of max_op_ms 5000 and an explicit execution control are needed. The parts do not search private settings.

It handles the context, the combinations of source/sink × execution 1500/3500 ms × lease 1000/60000 ms, VERIFY and OTHER-ROUTE.
After 400 ms it receives a normal frame, and during execution the committed idle deadline and lease deadline do not advance.
The write wait after execution advances 599 ms of the remaining 600 ms for the short lease, or up to idle 2999 ms for the long lease.
It checks that the deadlines do not shift during the wait, that idle 3000 ms is counted again from response completion, and that the lease counts from response generation and does not change on send completion (core §6.1),
and the following 999/1000 or 2999/3000 boundaries.
Fixed known source data and sink responses, and live GPIO/plan/ownership are also matched.

VERIFY runs a 3500 ms source on SID0 during the trial, and checks that the verify deadline is extended by the execution time and not counted again on the response.
OTHER-ROUTE runs a 3500 ms source on SID0 on another bridge 2 after deciding, and checks that port0's idle deadline is extended and not renewed by that response.
Both check the return at 1 ms before the remaining time and at the boundary.

`uv run pytest -q tests/test_conformance_port_speed_execution.py`.
It includes the normal case (overall and each schedule), response faults, clock/state faults, stop faults and the fixture guard.
Stop faults are verify consumption, verify renewal by the response, idle consumption/renewal by execution on another transport, and early return of verify.
3500 ms is longer than idle 3000 ms and also beyond the short lease of 1000 ms, so it is not a stimulus that passes by chance within the deadline.
Simultaneous execution on several transports, execution/refusal paths other than source/sink, physical time and Host switch/wait are not checked.
