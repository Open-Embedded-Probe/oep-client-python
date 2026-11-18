# Writing and debugging an SWD board

[日本語](swd.ja.md)

An Arm board connected to an OEP probe's SWD is written, run, stopped and inspected. There are two ways.

- **From the Arduino IDE**: the "Upload" button and the "Debug" button. Neither the core nor the IDE's settings are changed. Start one IDE server, and for debugging put `debug_custom.json` in the sketch folder.
- **Directly from the OEP host**: commands, gdb, pytest, Python.

The targets are Arm core 0 of RP2040 and RP2350 boards (Raspberry Pi Pico, Pico 2 and so on, arduino-pico). Which chip it is is told by the DPIDR at connection (RP2040 selects core 0 with TARGETSEL; RP2350 uses ADIv6 MEM-AP 0x2000).

## 1. Connecting

- Three wires: SWDIO, SWCLK and GND. The board's power is provided on the board side.
- Give the probe's 2 channels the labels `<port>.swdio` and `<port>.swclk` with the port's name. From then on, the board is addressed as `oep://<unit id>/<port>`.

  ```
  oep settings set oep://<unit id> core channel.GPIO28.label=pico.swdio channel.GPIO27.label=pico.swclk --save
  ```

  If you do not know which channels they are connected to, you can find them with `oep pins` (README, "Finding pins").
- To use a UART for the console, connect the board's TX to a free channel of the probe (3.4).

## 2. From the Arduino IDE

### 2.1 Starting the IDE server

```
pip install oep-client-python
python -m oep_client.ide oep://<unit id>/pico=rpipico --gdb-port 3333
```

- `=rpipico` is the board name shown to the IDE (`rpipico` when omitted). Any number of ports can be listed. Per port, one network port and
  one gdb port (counting up from `--gdb-port`) are created.
- The IDE server uses the probe only when an upload comes and while gdb is connected. At other times, other tools can use that probe.
- The startup line shows each port's name, UDP port and gdb port.

### 2.2 Uploading

1. Choose the board (Raspberry Pi Pico and so on) in the IDE.
2. In "Tools > Port", choose the network port `oep-<unit id>-<port> at <address>`. After starting the IDE server, it can take a few seconds
   to appear in the list.
3. Press "Upload". If asked for a password, leave it empty and press OK.

The IDE server checks the `.bin` the IDE sends with MD5, writes it over SWD, reads it back to verify, and restarts the board.

How it works: the IDE server announces mDNS `_arduino._tcp` per port (TXT is `board`, `auth_upload=no`, `tcp_check=no`, `ssh_upload=no`),
and acts as the receiver of the core's network upload (arduino-pico's `tools/espota.py`). It announces one address, the one on the LAN (chosen with
`--interface`).

### 2.3 Debugging

Put `debug_custom.json` in the sketch folder, and point the IDE's debugging at the IDE server's gdb server.

```json
[
  {
    "configId": "rp2040:rp2040:rpipico",
    "servertype": "external",
    "gdbTarget": "localhost:3333"
  }
]
```

- The file is an array. `configId` is the same string as the `configId` of the debug configuration the IDE builds. It is the board's FQBN, and if the board menus are changed from the default
  their values are attached too. Elements that do not match are silently ignored, so the reliable way is to copy the value the IDE writes to
  `.theia/launch.json` in the sketch folder after it has started debugging once.
- Pressing "Debug" makes the IDE upload the sketch (gdb's `load`), reset and stop the core, and stop at `main`. Breakpoints, stepping,
  pausing, and display of variables and registers work.
- The gdb server listens only on 127.0.0.1. Run the IDE on the same PC as the IDE server.
- Pressing "Upload" during debugging does not disconnect gdb. The IDE server writes within the gdb session, resets only the core and runs it.

### 2.4 Console

Put in a setting that passes the board's UART straight through to the probe's USB serial (CDC), and open the probe's port in the IDE's serial monitor.

```
oep settings set oep://<unit id> oep.fixture.uart/0 passthrough.cdc0.rx=GPIO12 passthrough.cdc0.baud=115200 --save
```

- `GPIO12` is the channel the board's TX is connected to, and `cdc0` is the probe's first CDC (transport names are in oep-spec core §13.3).
- The setting works regardless of sessions, and with `--save` it survives a restart.

### 2.5 What it cannot do

- Core 1 is not handled (core 0 only).
- Hardware breakpoints are limited to the number of FPB comparators (4 on RP2040). gdb automatically makes breakpoints in flash hardware ones. On RP2040 they cannot be placed at 0x20000000 and above (RAM).
- The RISC-V cores of RP2350 are not handled.
- There are no watchpoints.

## 3. Directly from the OEP host

### 3.1 Writing

```
python -m oep_client.rp2040_swd flash oep://<unit id>/pico sketch.ino.elf
```

It writes an ELF (laying out the loadable parts in flash) or a `.bin` (flash content from 0x10000000), reads it back to verify, and restarts the board.

### 3.2 Console (SEGGER RTT)

Even without a UART wire, the RTT ring in the board's RAM can be read over SWD (when the sketch uses RTT).

```
python -m oep_client.rp2040_swd console oep://<unit id>/pico 10       # show the output for 10 seconds
```

From pyserial, registering `oep_client.pyserial` lets you open `oep://`.

```python
import serial
serial.protocol_handler_packages.append("oep_client.pyserial")
port = serial.serial_for_url("oep://<unit id>/pico")
```

### 3.3 With gdb

Connect to the IDE server's gdb server with the gdb that comes with the core (`arm-none-eabi-gdb` for arduino-pico). If you do not need the upload receiver,
starting the IDE server for this use only works the same.

```
arm-none-eabi-gdb sketch.ino.elf
(gdb) target extended-remote 127.0.0.1:3333
(gdb) monitor reset halt
(gdb) load
(gdb) monitor reset halt
(gdb) break loop
(gdb) continue
```

| monitor command | Action |
|---|---|
| `reset halt` (`reset init`) | Resets the core and stops at the first instruction |
| `reset` (`reset run`) | Resets the core and runs it |
| `halt` | Stops |
| `resume` (`go`) | Runs |

While gdb is connected, the IDE server holds the probe's lock and sends a keepalive every 5 seconds.

### 3.4 pytest (pytest-embedded-arduino-cli)

The test's DUT and peer Pico are written over SWD by the plugin `oep_client.pytest_plugin`, which makes RTT the console. The port is
`TEST_SERIAL_PORT_*=oep://<unit id>/<port>` in `.env`. Details are in [pytest.md](pytest.md).

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

- `oep_client.arm`: `SwdWire` (`oep.wire.swd`), `ArmAdi` (`oep.target.arm-adi`), `MemAp`, `CortexM` (halt, registers, function calls).
- `oep_client.rp2040`, `oep_client.rp2350`: flash writing through the boot ROM (`program_and_verify`) and restart.
- RP2350 attaches without TARGETSEL, and uses `arm.ArmAdi(hst, conn, adiv6=True)`, `arm.MemAp(adi, 0x2000)`, `rp2350.core_of(mem)`.
  `rp2040_swd.Target` tells the chips apart and opens them (`.chip` is the chip it identified).

### 3.6 Sharing the probe with other tools

When a broker (OEP host guide §7) handles the same probe, `rp2040_swd` and the IDE server get a share of its lock
(`oep_client.brokers`: the environment variable `OEP_BROKER`, the user's `oep/brokers/<unit_id>.json`, the sharing extension `oep.probe.share`). Without a
broker, they open the probe directly.
