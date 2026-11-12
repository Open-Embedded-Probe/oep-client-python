# Writing and debugging an SWD board

[日本語](swd.ja.md)

An Arm board on an OEP probe's SWD is written, run, stopped and inspected. Two ways:

- **From the Arduino IDE**: its Upload and Debug buttons. Neither the core nor the IDE's settings change; one broker is
  started beforehand.
- **From an OEP host directly**: commands, gdb, pytest, Python.

The target is an RP2040 board's core 0 (a Raspberry Pi Pico and the like, arduino-pico).

## 1. Wiring

- Three wires: SWDIO, SWCLK, GND. The board powers itself.
- Label the probe's two channels with a port name, `<port>.swdio` and `<port>.swclk`. The board is then
  `oep://<unit id>/<port>`.

  ```
  oep settings set oep://<unit id> core channel.GPIO28.label=pico.swdio channel.GPIO27.label=pico.swclk --save
  ```

  `oep pins` finds the channels when you do not know them (README, "Finding the pins").
- For a UART console, wire the board's TX to a free channel of the probe (3.4).

## 2. From the Arduino IDE

### 2.1 Start the broker

```
pip install oep-client-python
python -m oep_client.ota_broker oep://<unit id>/pico=rpipico --gdb-port 3333
```

- `=rpipico` is the board name the IDE is shown (`rpipico` when left out). Give as many ports as you like: each gets a
  network port and a GDB port (from `--gdb-port` on).
- The broker uses the probe only while an upload comes in and while gdb is connected; other tools have it otherwise.
- Its first lines give each port's name, UDP port and GDB port.

### 2.2 Upload

1. Pick the board (Raspberry Pi Pico or another) in the IDE.
2. In Tools > Port pick the network port `oep-<unit id>-<port> at <address>`. It may take a few seconds after the
   broker starts to be listed.
3. Press Upload. Leave the password empty when asked.

The broker checks the `.bin` the IDE sends by its MD5, writes it over SWD, reads it back and restarts the board.

How: for each port the broker announces `_arduino._tcp` over mDNS (TXT `board`, `auth_upload=no`, `tcp_check=no`,
`ssh_upload=no`) and answers the core's network upload (arduino-pico's `tools/espota.py`). It announces one address, a
LAN one (`--interface` picks it).

### 2.3 Debug

Put `debug_custom.json` in the sketch folder to point the IDE's debugger at the broker's GDB server:

```json
[
  {
    "configId": "rp2040:rp2040:rpipico",
    "servertype": "external",
    "gdbTarget": "localhost:3333"
  }
]
```

- The file is an array. `configId` is the string of the IDE's own debug configuration: the board's FQBN, with the board
  menu's values when they were changed from the defaults. An element that does not match is silently ignored; the sure
  way is to copy the value the IDE writes to the sketch folder's `.theia/launch.json` once a debug session has started.
- Debug writes the sketch (gdb's `load`), resets the core halted and stops at `main`. Breakpoints, stepping, pausing,
  variables and registers work.
- The GDB server listens on 127.0.0.1 only: run the IDE on the broker's PC.
- Upload while debugging keeps gdb connected: the broker writes within gdb's session and resets the core alone to run.

### 2.4 Console

Pass the board's UART through to the probe's USB serial port (CDC) and open the probe's port in the IDE's Serial
Monitor:

```
oep settings set oep://<unit id> oep.fixture.uart/0 passthrough.cdc0.rx=GPIO12 passthrough.cdc0.baud=115200 --save
```

- `GPIO12` is the channel the board's TX is wired to, `cdc0` the probe's first CDC port (route names: oep-spec core
  §13.3).
- The setting works outside any session; `--save` keeps it across restarts.

### 2.5 Limits

- Core 1 is not handled (core 0 only).
- Four hardware breakpoints (the RP2040's FPB). gdb makes breakpoints in flash hardware ones by itself.
- No watchpoints.

## 3. From an OEP host directly

### 3.1 Write

```
python -m oep_client.rp2040_peer flash oep://<unit id>/pico sketch.ino.elf
```

An ELF (its loadable parts in flash, laid out) or a `.bin` (the flash from 0x10000000) is written, read back and the
board restarted.

### 3.2 Console (SEGGER RTT)

Without a UART wire, the board's RTT rings in RAM are read over SWD (when the sketch uses RTT):

```
python -m oep_client.rp2040_peer console oep://<unit id>/pico 10       # print the output for 10 s
```

From pyserial, `oep://` opens once `oep_client.pyserial` is registered:

```python
import serial
serial.protocol_handler_packages.append("oep_client.pyserial")
port = serial.serial_for_url("oep://<unit id>/pico")
```

### 3.3 With gdb

Connect the core's own gdb (arduino-pico's `arm-none-eabi-gdb`) to the broker's GDB server. A broker started for this
alone works the same when no upload is needed.

```
arm-none-eabi-gdb sketch.ino.elf
(gdb) target extended-remote 127.0.0.1:3333
(gdb) monitor reset halt
(gdb) load
(gdb) monitor reset halt
(gdb) break loop
(gdb) continue
```

| monitor command | what it does |
|---|---|
| `reset halt` (`reset init`) | reset the core and stop at its first instruction |
| `reset` (`reset run`) | reset the core and run |
| `halt` | stop |
| `resume` (`go`) | run |

While gdb is connected the broker holds the probe's lock, a keepalive every 5 s.

### 3.4 pytest (a peer board of pytest-embedded-arduino-cli)

A test's peer Pico is written over SWD and its RTT is the console. Add one line to conftest.py:

```python
pytest_plugins = ["oep_client.pytest_rp2040_peer"]
```

- The peer's address comes in the environment variable `TEST_SERIAL_PORT_PEER_<NAME>=oep://<unit id>/<port>`.
- The peer's profile uses arduino-pico's upload method `uploadmethod=picoprobe_cmsis_dap`. The plugin writes OpenOCD
  files into the peer's folder (`interface/cmsis-dap.cfg`, `target/rp2040.cfg`) that hand the upload to
  `rp2040_peer flash`. Keep them out of git.
- The peer's device lock is its port's own (`rp2040-swd:<address>`).

### 3.5 From Python

```python
from oep_client import arm, link, rp2040

hst = link.open_host("oep://<unit id>")
hst.open(60000, owner="my-tool")
wire = arm.SwdWire(hst)
conn, dpidr, _ = wire.attach(targetsel=rp2040.TARGETSEL_CORE0, pins=(28, 27))   # the SWDIO, SWCLK channels
adi = arm.ArmAdi(hst, conn)
adi.power_up()
mem = arm.MemAp(adi, 0)
core = rp2040.core_of(mem)
core.halt()
print(hex(core.reg(15)))      # PC
core.resume()
wire.detach(conn)
hst.end()
hst.link.close()
```

- `oep_client.arm`: `SwdWire` (`oep.wire.swd`), `ArmAdi` (`oep.target.arm-adi`), `MemAp`, `CortexM` (halt, registers,
  calling a function).
- `oep_client.rp2040`: writing the flash through the boot ROM (`program_and_verify`) and restarting.

### 3.6 Sharing the probe with other tools

When ch32rv's broker has the probe, `rp2040_peer` shares its lock (ch32rv's named extension
`io.github.ch32-riscv-ug.share`). Without ch32rv running it opens the probe directly.
