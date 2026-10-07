"""The board table: every board the hardware test knows, keyed by its board-identify id (or, for a USB probe no bridge
gives an id, its unit id = its USB serial). OEP_HW_BOARDS is a comma list of these keys.

Each entry says what the board is (kind: how it is flashed), which sketch.yaml profile of oep-probe-arduino's
examples/Firmware/OepProbe builds its firmware, how to reach its OEP port after a flash, and the describe model the
firmware reports. The defaults a test needs on that board (two free GPIO channels, a UART RX / TX pair, the port_speed
candidates of a bridge) are here too; the environment overrides them (OEP_HW_GPIO, OEP_HW_UART, OEP_HW_RATES). A
channel is free when the board wires it to nothing; 0 = none, and the tests that need one skip.

Port forms (see oep_client.link.open_host):
  /run/board-identify/by-id/<id>   a serial port: a USB-UART bridge (classic ESP32) - also where esptool flashes
  usb:<unit_id>                    the probe whose USB serial is the unit id (ESP32-P4: vendor bulk, then HID)
  cdc:<unit_id>                    the CDC serial port of the USB device whose serial is the unit id (RP2040 / RP2350),
                                   found with pyserial's list_ports (the kernel names it /dev/ttyACMn as it pleases);
                                   also the port the 1200-baud touch goes to before a flash
  tcp:<unit_id>                    a probe on the network, found by DNS-SD (_oep._tcp, TXT unit_id; transports §3)
  tcp://<host>:<port>              a probe on the network at that address (where mDNS does not reach: WSL 2's NAT, a
                                   router between; tcp://<host> alone takes the port DNS-SD finds)

A TCP board (kind tcp) is never flashed: put the firmware on through the board's serial / USB entry first (its own
run), and give it its networks with OEP_WIFI_SSID_<n> / OEP_WIFI_PASS_<n> on that run (the wifi test). OEP_HW_BOARDS
may also name a TCP probe directly - `tcp:<unit_id>` or `tcp://<host>:<port>` - as a board of kind tcp with no table
entry (no expected model, no free channels unless OEP_HW_GPIO / OEP_HW_UART / OEP_HW_DISABLE give them).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

BY_ID = "/run/board-identify/by-id"


@dataclass(frozen=True)
class Board:
    id: str                              # board-identify id, or the unit id
    kind: str                            # esp32 (esptool merged.bin) | esp32p4 (USB DFU app.bin) | esp32p4-usj (esptool
                                         # merged.bin over the P4's USB-Serial/JTAG) | rp2 (BOOTSEL uf2) | virtual |
                                         # tcp (on the network: never flashed here)
    profile: str                         # sketch.yaml profile in examples/Firmware/OepProbe
    model: str                           # describe's model ("": not checked)
    port: str                            # the OEP port after flashing (forms above)
    unit_id: str | None = None           # the USB serial (p4 / rp2); the bridge boards learn theirs from describe
    flash_port: str | None = None        # esp32: the bridge (esptool); rp2: the CDC port for the 1200-baud touch (None: = port)
    usbip_busid: str | None = None       # WSL: usbipd busid to attach again after a DFU reboot (p4)
    rates: tuple[int, ...] = ()          # port_speed candidates (UART bridge only)
    gpio: tuple[int, int] = (0, 0)       # two free channels for the gpio set / read test and the logic capture (drive, pull)
    disable: int = 0                     # a third free channel the config test disables, then enables again
    label: int = 0                       # the channel the config test labels (0: gpio[0]); neither it nor disable is driven
    uart: tuple[int, int] = (0, 0)       # (rx, tx) channels for the uart configure test
    virtual_profile: str | None = None   # kind virtual: the oep_client.virtual_bench profile served on a pty
    notes: str = ""

    @property
    def resettable(self) -> bool:
        """A classic ESP32 behind a USB-UART bridge whose auto-reset circuit wires EN / IO0 to RTS / DTR: the host can
        reboot it (flash.hard_reset). A USB probe has no such line from the host; the virtual bench has nothing to reset."""
        return self.kind == "esp32"

    @property
    def usb(self) -> bool:
        """The probe is itself a USB device (its own restart takes it off the bus and back): the P4s and the RP2s."""
        return self.kind in ("esp32p4", "esp32p4-usj", "rp2")

    @property
    def tcp(self) -> bool:
        """Reached over TCP (Wi-Fi): flashed through its other entry, never here."""
        return self.kind == "tcp"

    @property
    def shared(self) -> bool:
        """A jig of the ArduinoCore-CH32RV bench: running on it needs the bench's permission first (README)."""
        return "jig" in self.notes


# The classic ESP32 firmware's channels: 4-5, 13-14, 16-19, 21-23, 25-27, 32-36, 39 (a PICO-D4 loses 16 / 17 to its flash).
# An M5Stack ATOM brings out G19, G21-23, G25, G26, G32, G33 (G27: its LED, G39: its button).
_ESP32_ATOM = dict(gpio=(25, 26), disable=33, uart=(32, 21), rates=(1500000, 921600, 500000))
# The V003 jig (ESP32-D0WD-V3 + CH340; ArduinoCore-CH32RV tests/benches/v003-esp32.toml): SWIO 16, NRST 23, and every
# other output-capable channel (4, 5, 13, 14, 17-19, 21, 22, 25-27, 32, 33) goes to a V003 pad. Left: 34-36 and 39, inputs
# with no pulls - no gpio pair, no UART TX (gpio / capture / capture_group skip; uart runs on the jig's settings plan, or
# skips). config labels 35 and disables 34 (neither driven); the analog capture reads 34.
_ESP32_V003_JIG = dict(label=35, disable=34, rates=(921600, 500000))
# ESP32-P4: GPIO 24 / 25 are the USB-Serial/JTAG. The X035 jig (tests/benches/x035-p4.toml) wires 2 / 54 (RVSWD), 4-6,
# 9-15, 46-50, 52, 53; the third P4 4, 5, 19, 21-23: none of these. 34-38 are the P4 straps, read only at a chip
# reset, when the probe drives no pin: disable 34 is never driven, TX 37 only while planned.
_P4 = dict(gpio=(32, 33), disable=34, uart=(36, 37))
# Pro Micro RP2350: GP19 is the PSRAM CS; GP0 / GP1 carry the bench's L103 (tests/benches/l103-rp2350.toml: nothing
# else wired). The fixture UART is Serial1 = UART0, whose pins arduino-pico accepts are TX 0 / 12 / 16 / 28, RX 1 / 13 /
# 17 / 29 (GP4 / GP5 hung the probe, 2026-10-02).
_RP2350 = dict(gpio=(26, 27), disable=28, uart=(13, 12))

TABLE: dict[str, Board] = {b.id: b for b in (
    Board("esp32-pico-d4-50029191fe34", "esp32", "esp32", "esp32", f"{BY_ID}/esp32-pico-d4-50029191fe34",
          flash_port=f"{BY_ID}/esp32-pico-d4-50029191fe34", notes="M5Stack ATOM (ESP32-PICO-D4), FTDI bridge; free for OEP tests",
          **_ESP32_ATOM),
    Board("esp32-d0wd-v3-0070070d9394", "esp32", "esp32", "esp32", f"{BY_ID}/esp32-d0wd-v3-0070070d9394",
          flash_port=f"{BY_ID}/esp32-d0wd-v3-0070070d9394", notes="V003 jig (classic ESP32 + CH340): the bench's; ask first",
          **_ESP32_V003_JIG),
    Board("esp32-series-30eda0e31108", "esp32p4", "esp32p4", "esp32p4", "usb:30eda0e31108", unit_id="30eda0e31108",
          usbip_busid=os.environ.get("TEST_BENCH_CH32X035_USBIP_BUSID", "12-4"),
          notes="X035 jig (ESP32-P4, HS USB): the bench's; ask first", **_P4),
    Board("esp32-series-30eda0e343c6", "esp32p4", "esp32p4", "esp32p4", "usb:30eda0e343c6", unit_id="30eda0e343c6",
          usbip_busid="11-4", notes="second P4 (ESP32-P4, HS USB), the user's: free for dev-oep, nothing wired but its LED "
                "(an older firmware's USB serial is 30eda0e343c6-hs)",
          **_P4),
    Board("esp32-series-30eda0ea068b", "esp32p4-usj", "esp32p4", "esp32p4", f"{BY_ID}/esp32-series-30eda0ea068b",
          unit_id="30eda0ea068b", flash_port=f"{BY_ID}/esp32-series-30eda0ea068b",
          notes="third P4 (ESP32-P4, FS USB-Serial/JTAG port only), ours: a CH32V003 on SWIO 19, NRST 4, power from 5 "
                "(the idle output-high its settings keep), its UART on 22 / 23, its app's output on 21", **_P4),
    Board("9489dd2ae0953650", "rp2", "promicrorp2350", "rp2350", "cdc:9489dd2ae0953650", unit_id="9489dd2ae0953650",
          notes="SparkFun Pro Micro RP2350 (the firmware enumerates as the project's 1209:4F45); no board-identify id: keyed by unit id",
          **_RP2350),
    # The ATOM over Wi-Fi / TCP: the same board and firmware as above, flashed and given its networks through its bridge
    # entry (OEP_WIFI_SSID_<n> / OEP_WIFI_PASS_<n> on that run); found by DNS-SD unless OEP_HW_ATOM_TCP names it
    # (tcp://HOST:PORT - from WSL 2's NAT mDNS does not reach the board; `oep config state` over the bridge shows its ip)
    Board("esp32-pico-d4-50029191fe34-tcp", "tcp", "esp32", "esp32", os.environ.get("OEP_HW_ATOM_TCP", "tcp:50029191fe34"),
          unit_id="50029191fe34", notes="M5Stack ATOM over Wi-Fi / TCP (reference firmware: port 7450); free for OEP tests",
          **{k: v for k, v in _ESP32_ATOM.items() if k != "rates"}),
    # No hardware: the virtual bench on a pty (oep_client.virtual_bench_serve). A dry run of the test logic, never a release test.
    Board("virtual-esp32-v003", "virtual", "esp32", "esp32", "", virtual_profile="esp32-v003", gpio=(25, 26), disable=33,
          uart=(21, 22), rates=(921600, 500000), notes="oep_client.virtual_bench esp32-v003 on a pty; flashing is a no-op"),
)}


def selected() -> list[Board]:
    """The boards OEP_HW_BOARDS names, in that order (an unknown id raises). Environment overrides apply to all."""
    ids = [s.strip() for s in os.environ.get("OEP_HW_BOARDS", "").split(",") if s.strip()]
    out = []
    for i in ids:
        if i not in TABLE and i.startswith("tcp:"):
            out.append(_override(tcp_board(i)))
            continue
        if i not in TABLE:
            raise KeyError(f"OEP_HW_BOARDS: {i!r} is not in tests/hw/boards.py ({', '.join(TABLE)}), nor tcp:<unit_id> / "
                           "tcp://<host>:<port>")
        out.append(_override(TABLE[i]))
    return out


def tcp_board(target: str) -> Board:
    """A probe on the network named in OEP_HW_BOARDS (tcp:<unit_id> or tcp://<host>[:<port>]): kind tcp, nothing
    expected of its model, no free channels but the environment's."""
    unit = target[4:] if not target.startswith("tcp://") else None
    return Board(target, "tcp", "", "", target, unit_id=unit, notes="a TCP probe named in OEP_HW_BOARDS")


def _pair(text: str) -> tuple[int, int]:
    a, b = (int(v) for v in text.split(","))
    return a, b


def _override(b: Board) -> Board:
    env = os.environ
    kw: dict = {}
    if env.get("OEP_HW_GPIO"):
        kw["gpio"] = _pair(env["OEP_HW_GPIO"])
    if env.get("OEP_HW_DISABLE"):
        kw["disable"] = int(env["OEP_HW_DISABLE"])
    if env.get("OEP_HW_UART"):
        kw["uart"] = _pair(env["OEP_HW_UART"])
    if env.get("OEP_HW_RATES"):
        kw["rates"] = tuple(int(v) for v in env["OEP_HW_RATES"].split(",") if v.strip())
    if env.get("OEP_HW_USBIP_BUSID"):
        kw["usbip_busid"] = env["OEP_HW_USBIP_BUSID"]
    return replace(b, **kw) if kw else b


def resolve_port(b: Board) -> str:
    """The target string for oep_client.link.open_host: a cdc:<unit> form is looked up now (the device path moves)."""
    if b.port.startswith("cdc:"):
        return cdc_port(b.port[4:])
    return b.port


def cdc_port(unit_id: str) -> str:
    """The serial port of the USB device whose serial number is `unit_id` (pyserial list_ports)."""
    from serial.tools import list_ports
    want = unit_id.lower()
    for p in list_ports.comports():
        if (p.serial_number or "").lower() == want:
            return p.device
    raise FileNotFoundError(f"no serial port of a USB device with serial {unit_id}")
