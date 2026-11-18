# Checks of representative OEP-specific scenarios

[日本語](oep-example-smoke-conformance.ja.md)

`OepExampleSmokeChecks`, using GPIO, a receive-only UART and raw ARM as subjects, provides examples of "prepare → request → response → effect → release" that can also be used to test custom extensions.
These specific operations are not mixed into the core checks or the checks common to all extensions as required.

```python
from oep_client.conformance import spec_identity
from oep_client.conformance.core_smoke import CoreSmokeChecks
from oep_client.conformance.oep_smoke import OepExampleSmokeChecks

registry, spec = spec_identity(spec_checkout)
core = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec)
report = OepExampleSmokeChecks(
    core,
    pins=selected_pins,  # gpio: 2 channels, uart_rx: 1 channel, swd: 2 channels
    observe_resources=observe_selected_resources,
    resources_released=selected_resources_are_released,
    feed_uart=send_selected_uart_data,
    functions=selected_functions,  # optional: name -> fn for multiple instances
).run()
```

The calling project states the pins and each callback explicitly. Private bench files and fixed wiring are not referred to.
If there are several instances with the same name, specify the fn chosen from the current list in functions.
This is a check that, when run, sets the given GPIO, creates a UART plan and does an SWD attach. Choose only explicit equipment; the caller provides exclusivity and framing.
It does not search for targets not selected, and does not take other sessions with force.

It checks the reviewed SHAs of the SPEC's core and of the fixture/plan/debug/common body texts before sending.
It checks that the selected OEP's required operations are in ops. If absent, the failed preparation is recorded as a failure and not confused with optional features.

GPIO does plan/set/read, and checks that a batch with an unsupported mode later in it is refused before execution and the original state is kept.
If output low/high are declared, their change is used; even a minimal GPIO with only mode0 can be checked with the same structure.
To check things like reapplying the same setting, the number of setting effects and so on must be observed independently, not only the level.

UART handles an RX-only plan, configure, an explicit AB stimulus and two non-consuming reads.
Reception starts at configure, so the stimulus is given after configure. It also checks that status before configure is baud 0, format 0 (the mark of unconfigured),
and that status after configure is the actual baud configure returned and 8N1.
Without marks, read from 3 (read from a mark) is unsupported; without write, write is unknown_operation; and it checks the effects do not change. The absence of optional operations is not a violation.
Without a stimulus callback, the UART is not_run and the whole is incomplete; it is not a PASS or a missing-equipment skip.

ARM passes the connection obtained by SWD attach to the transfer of another target fn and checks the DP identity and ACK/progress.
attach is sent with the fixed part `method(0), swdio, swclk, max_speed`, the connection is read as u32 and placed as u32 at the start of transfer.
A read batch whose fixed response does not fit in max_frame is refused without touching the target.
After the session ends, the previous session's connection is used with a new SID, and no_resource and unchanged effects are checked.
read_block/write_block are not required, and no new generation numbers or large result buffers are required.

observe_resources returns a JSON-compatible snapshot of the selected plan / pin settings / buffers / connections / target effects and so on.
The core's corr/lease state, which may change even on refusal, and the next resource ID, which may be skipped, are not mixed into the unchanged-effects snapshot.
resources_released independently checks the release of the selected resources. It does not mean resetting values kept within a boot, such as the UART position, or completed effects on an external target, to zero.

```sh
uv run pytest -q tests/test_conformance_oep_smoke.py
```

After a failure, further scenarios stop, and unfinished SIDs remain in the result. Even when communication breaks, no unconditional cleanup requests are sent; the caller checks the state and transport and recovers.
For an interface not provided at all, that example is not applicable. This check does not prove all operations, all optional features, the whole resource graph, electrical conditions or full OEP-specific compliance.
The report's scope/unchecked and full_conformance=false are handled together with the results.
