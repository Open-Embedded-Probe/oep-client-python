# Describe metadata checks for the fixture family

[日本語](fixture-metadata-conformance.ja.md)

`FixtureMetadataChecks(checks, selection).run()` checks, in the third stage, the GPIO/UART/I2C/SPI fn/names the caller states explicitly.
It is limited to revision 1 and also runs the registered-op checks. All requests are reads on SID 0; standard names not selected are not searched for or operated.

- GPIO: required modes u32 and input mode 0.
- UART: the element count and fixed shape of formats, the presence of 8N1, defined data length/parity/stop bits.
  A fn without write does not declare a TX role with role_channels/channel_group.
- I2C/SPI: checks max_length u16, max_clock_hz u32, queue_depth u8. features may be omitted (no bits); when present it is u32.
  It checks that requests/responses using max_length fit in the frame of every transport.
- When I2C stretch is provided, max_stretch_us is required and is a positive u32.
- SPI: modes u8 is required: bit k = mode k, bits 4 to 7 are 0, at least one set. cs_setup_ns may be omitted or 0; it is checked as u32 only when present.

The largest I2C data request is 12 + max_length bytes, the response 8 + max_length bytes.
SPI is request 14 + max_length bytes, response 12 + max_length bytes.
They are compared with the negotiated frame; for the all-transport guarantee, the 64-byte minimum frame or the confirm of the only advertised transport is used.
With several transports, a size above the minimum frame does not PASS while the limits of the other transports are unconfirmed.
No positive lower bounds for queue_depth/max_length/max_clock_hz that the spec does not state are imposed.

The independent `FixtureMetadataModel` is a fixture that holds only the declarations of the standard interfaces.
The third stage for a selected fn checks the preconditions, the registered ops and the metadata.
The common declarations and registered ops are also checked separately.
The behavior of the advertised ops and pin assignment are not implemented, and it is not used as a fully conforming probe.
configure/set/transfer/plan lifetime/electrical conformance are not checked, and `full_conformance` is false.
The public side does not depend on private benches or equipment settings.

```sh
uv run pytest -q tests/test_conformance_fixture_metadata.py
```

From the Arduino uv/pytest environment, the path of the public code is also given explicitly.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_fixture_metadata.py
```
