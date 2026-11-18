# Checks of selected standard describe metadata

[日本語](selected-metadata-conformance.ja.md)

`SelectedMetadataChecks(checks, selection).run()` runs the third-stage checks for the explicit fn/name.
It handles revision 1 of restart, riscv-dm and arm-adi, and also runs the registered-op checks. All requests are reads on SID 0.

- restart: `restart_max_ms` is a required u32. No positive lower bound that the spec does not state is imposed.
- RISC-V: when block is provided, `max_length` is a required u16, a multiple of 4 in bytes.
- ARM-ADI: when block is provided, the same limit conditions apply. `features` sets no bits (debug §6 defines no bits. A value of 0 is allowed).

`max_length` is the fn's limit, not the transport's limit. The host uses, per transport, the smaller of max_length and the length that max_frame allows (debug §4.5), so it is not matched against the transport's frame.
`max_length=0` is not refused either, because the spec has no positive lower bound.

`SelectedMetadataModel` is an independent fixture that holds only the declarations of the standard interfaces.
The common declarations and registered ops of all fns are checked separately.
It has no real behavior for the advertised ops and is not treated as a conforming probe. It includes unit tests that break missing items, types, sizes and forbidden tags.
Other required metadata, restart duration, bus behavior and physical delivery are not checked.
`full_conformance` is false. It does not depend on an implicit search for private benches or equipment.

```sh
uv run pytest -q tests/test_conformance_selected_metadata.py
```

The explicit public code can also be used from the Arduino uv/pytest environment.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_selected_metadata.py
```
