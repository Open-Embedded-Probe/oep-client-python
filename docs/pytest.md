# OEP in pytest (pytest-embedded-arduino-cli)

[日本語](pytest.ja.md)

OEP probes and the boards behind them are used in [pytest-embedded-arduino-cli](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli) tests.
When oep-client-python is installed with its `pytest` extra, the plugin `oep_client.pytest_plugin` loads automatically. No conftest.py is needed.

```
pip install "oep-client-python[pytest]"
```

The plugin acts only when a test's port is an OEP probe, and when a test uses the plugin's fixtures.

## 1. Ports

Ports go in `.env`, as in pytest-embedded-arduino-cli's guide.

```sh
# tests/.env
TEST_SERIAL_PORT_DUT=oep://0123456789ab/dut
TEST_SERIAL_PORT_PEER_PICO=oep://0123456789ab/pico
```

The address is `oep://<unit_id>/<port>` (the port is the first half of the probe's label `<port>.<line>`; OEP host guide §5.3).

## 2. Lock

A probe has one lock per unit (OEP core §6.1). The plugin names pytest-embedded-arduino-cli's lock
`probe:<unit_id>`. The ports and routes (`?via=...`) of the same probe all get the same name. So does the probe's own USB serial port.

- Within one test, even when the DUT and its peer are on the same probe, they share one lock.
- Other tests wait until that probe is free.

## 3. Port records

What is connected behind the port (parts, the kind of peer, board defects and so on) is read with the test argument `port_memo`. The record's shape is decided by the record's owner
(the test project). The owner's name goes in the ini.

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

Exactly one of the following is used, in this order.

1. `OEP_PORT_MEMO_<unit_id>_<port>` in `.env` (`OEP_PORT_MEMO_<unit_id>` for a probe without ports; upper case, with `-` as `_`). Write one JSON object
   enclosed in single quotes. It is used whole, not mixed item by item with the memo area.
2. `<owner>/port/<port>` in the probe's memo area (oep-if-memo §4).
3. If neither exists, `{}`.

If both exist and differ, a warning is issued (fix the memo area, or delete the `.env` variable). The peer port's record is `peer_port_memo("<name>")`.
A probe without a memo area (such as a WCH-Link for which another plugin returns the name `probe:<id>`) uses `.env` only.

```sh
# tests/.env
OEP_PORT_MEMO_0123456789AB_DUT='{"parts": {"eth": {"to": "lan"}}}'
```

Writing to the memo area is done by tools outside pytest (such as a tool that uses `Memo.write` of `oep_client.memo`).

## 4. Console

pyserial can open `oep://...` (`oep_client.pyserial`). On RP2040 / RP2350 boards, SEGGER RTT read over SWD is the console
([swd.md](swd.md) §3.2). The sketch uses RTT.

## 5. Writing

When the port is `oep://<unit_id>/<port>` and the profile is an RP2040 / RP2350 board (fqbn `rp2040:`), the plugin writes the build's ELF over the probe's SWD
(pytest-embedded-arduino-cli's write hook). arduino-pico's upload method does not matter. Otherwise, arduino-cli's writing is used as is.

## 6. Session and UART

The test argument `oep_host` is the OEP session with the probe behind the DUT's port (`oep_host.host` is the Host, `oep_host.touch()` keeps the lease).
Silence longer than 60 seconds loses the session.

`oep_uart.open(baud)` assigns `oep.fixture.uart` to the wires with the port labels `<port>.rx` / `<port>.tx` (OEP host guide §5.3), and returns a channel with `expect`,
`expect_exact` and `write` (the same pexpect shape as `dut`. The default wait is 30 seconds).

```python
def test_uart(dut, oep_uart):
    ch = oep_uart.open(115200)
    ch.write("ping\n")
    ch.expect("pong")
```

## 7. Sharing with other tools

When a broker (OEP host guide §7) handles the same probe, the plugin gets a share of its lock (`oep_client.brokers`).
When the DUT's console goes through the platform's own monitor (when pytest-embedded-arduino-cli wraps the port), that monitor holds the probe and
publishes the broker a little later. `oep_host` waits for it up to `oep_broker_wait` seconds in the ini (default 10).
