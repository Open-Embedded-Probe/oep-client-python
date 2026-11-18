# Op declaration checks of registered OEP interfaces

[日本語](registered-ops-conformance.ja.md)

`RegisteredOpsChecks(checks, selection).run()` checks, in the third stage, the required ops of the explicit fn/names.
`selection` is a dictionary the caller passes. It does not search for standard names and run features; all requests are discovery and declaration reads on SID 0.
It uses the registry's op codes, and the required/optional distinction comes from each interface's normative text.
It is not guessed from whether a lock is needed or from gaps in numbers. It supports the revision 1 standard interfaces (plan, restart, link, wire, riscv-dm, arm-adi, console, fixture, capture).

`RegisteredOpsModel` is an independent declaration-only fixture and has no implementation of the advertised ops.
It is not used as a conforming probe. It declares only the minimal required ops, and the unit tests break the declarations to check the checker.
The common declarations of all fns run separately in the second stage.
The specific metadata of wire/console is handled by the [standard wire / console specific declaration](oep-declaration-conformance.md) checks.

- The ops in the plan/restart, GPIO and SPI tables are required. ARM-ADI requires transfer; read_block/write_block are an optional pair.
- console/UART marks/clear/mark/write are optional. A fn with mark also has marks.
- wire scan and I2C stretch are optional. link port_speed and port_state are optional. A fn with port_speed also has port_state.
- RISC-V halt/resume/reset/block/run/step are optional. read_block/write_block come as a pair.
- logic/analog configure/start/stop/status/read/segments are required. force/query/triggers are optional (capture §3.2).
  release is required only when mode 2 is declared in describe's mode (0x40); otherwise optional (capture §1/§3.2).
  subscribe is required only when mode 3 is declared; otherwise optional (capture §3.2/§3.4). subscribe/unsubscribe come as a pair.
  analog calibration is optional and is not declared for logic (checked by the number of analog's calibration).
- capture-group requires bind/start/stop/status; force is optional, subscribe/unsubscribe an optional pair (capture §4.1/§4.3).
- Only the first byte of a mode row is read. The shape, duplicates and count of modes are handled by the [capture describe checks](capture-metadata-conformance.md).
- An op added to the selected registry is checked as required unless it is a known optional op name; it is not silently ignored.
  If the optional policy and the registry disagree, it stops at the preconditions. It is not applied automatically to new standard names/revisions.

Not having an optional op is not a failure. Real op behavior, required describe tags, notifications, resource lifetime and physical behavior are not checked.
`full_conformance` is false. The public code does not depend on private benches or equipment settings.

```sh
uv run pytest -q tests/test_conformance_registered_ops.py
```

From the Arduino uv/pytest environment, use this.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_registered_ops.py
```
