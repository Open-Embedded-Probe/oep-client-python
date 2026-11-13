# OEP in pytest (pytest-embedded-arduino-cli)

[日本語](pytest.ja.md)

OEP probes and the boards behind them in [pytest-embedded-arduino-cli](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli)
tests. Installed with its `pytest` extra, oep-client-python's plugin `oep_client.pytest_plugin` loads by itself; no
conftest.py is needed.

```
pip install "oep-client-python[pytest]"
```

The plugin acts only when a test's port is an OEP probe, or when a test asks for its fixtures.

## 1. Ports

Ports go in `.env`, as pytest-embedded-arduino-cli's guide has it.

```sh
# tests/.env
TEST_SERIAL_PORT_X035C8T6_S3=oep://30eda0e31108/x035c8t6-s3
TEST_SERIAL_PORT_PEER_PICO=oep://80f1b2d835d6/rp2040-x035
```

The address is `oep://<unit_id>/<port>` (the port is the first half of the probe's labels `<port>.<line>`, OEP host
guide §5.3).

## 2. The lock

A probe has one lock (OEP core §6.1). The plugin names pytest-embedded-arduino-cli's lock `probe:<unit_id>`: every
port and route (`?via=...`) of one probe has that name, and so has the probe's own USB serial port.

- In one test, a DUT and a peer on the same probe share the one lock.
- Another test waits until the probe is free.

## 3. The port's record

What is wired to a port (parts, the kind of its peer, the board's defects) is read with the test argument `port_memo`.
The record's shape is its owner's (the test's project); the owner's name goes in the ini.

```toml
# pyproject.toml
[tool.pytest.ini_options]
oep_memo_owner = "io.github.example.mylib"
```

```python
def test_eth(dut, port_memo):
    if port_memo.get("parts", {}).get("eth", {}).get("to") != "lan":
        pytest.skip("eth is not wired on this port")
```

The first of these is used:

1. `.env`'s `OEP_PORT_MEMO_<unit_id>_<PORT>` (`OEP_PORT_MEMO_<unit_id>` for a probe without ports; upper case, `-` as
   `_`): one JSON object in single quotes, used whole, never mixed with the memo member by member.
2. The probe's memo object `<owner>/port/<port>` (oep-if-memo §4).
3. `{}`.

When both are there and differ, a warning (correct the memo or drop the variable). A peer's record is
`peer_port_memo("<name>")`. A probe without a memo area (a WCH-Link another plugin names `probe:<id>`) has `.env` only.

```sh
# tests/.env
OEP_PORT_MEMO_BC5F4B9807F7='{"parts": {"eth": {"to": "lan"}}}'
```

The memo is written by tools outside pytest (ones using `oep_client.memo`'s `Memo.write`).

## 4. The console

pyserial opens `oep://...` (`oep_client.pyserial`). For an RP2040 board it is the SEGGER RTT read over SWD
([swd.md](swd.md) §3.2); the sketch uses RTT.

## 5. The upload

For a port `oep://<unit_id>/<port>` whose profile is an RP2040 board (fqbn `rp2040:`), the plugin writes the build's
ELF over the probe's SWD (pytest-embedded-arduino-cli's upload hook), whatever arduino-pico's upload method is.
Anything else is arduino-cli's upload.

## 6. The session and the UART

The test argument `oep_host` is an OEP session on the probe behind the DUT's port (`oep_host.host` is the Host,
`oep_host.touch()` keeps the lease); quiet for longer than 60 s, it loses the session.

`oep_uart.open(baud)` plans an `oep.fixture.uart` onto the port's lines labelled `<port>.rx` / `<port>.tx` (OEP host
guide §5.3) and returns a channel with `expect`, `expect_exact` and `write` (pexpect, like `dut`; 30 s by default).

```python
def test_uart(dut, oep_uart):
    ch = oep_uart.open(115200)
    ch.write("ping\n")
    ch.expect("pong")
```

## 7. Sharing with other tools

When a broker (OEP host guide §7) serves the probe, the plugin shares its lock (`oep_client.brokers`). When the DUT's
console runs through the platform's own monitor (pytest-embedded-arduino-cli wraps the port), that monitor holds the
probe and publishes its broker a moment later; `oep_host` waits for it up to the ini value `oep_broker_wait` seconds
(default 10).
