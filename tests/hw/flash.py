"""Flashing a probe firmware onto a board, one way per board kind (release-testing.ja.md §3 item 1), and the reset of a
bridge board for the config test:

  esp32    esptool --chip esp32 -p PORT -b 115200 write-flash 0x0 <merged.bin>   (esptool on PATH; 115200: an ATOM's FTDI)
  esp32p4  USB DFU 1.1 of the app image to the running probe (pyusb; what dfu-util -D does), then the probe reboots into
           it. On a WSL bench usbipd drops the device at the reboot: `usbipd.exe attach --wsl --busid <busid>` when the
           board table has one.                                                                        [UNTESTED here]
  rp2      the 1200-baud touch on the CDC port reboots the board into BOOTSEL (the boot ROM's USB device, 2e8a:0003 /
           2e8a:000f); `picotool load -x <uf2>` (OEP_HW_PICOTOOL, default picotool on PATH) writes it over PICOBOOT and
           restarts the board. With OEP_HW_UF2_DRIVE=<mount> the .uf2 is copied to that BOOTSEL drive instead (a host
           that mounts it). Neither possible -> FlashSkipped: the tests go on with what is on the board.

Every flasher returns a small record for the results (seconds, the tool's last lines). FlashError: the image did not
go on. FlashSkipped: nothing was written, the board keeps its firmware (the tests still run, the record says so)."""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import time


class FlashError(Exception):
    pass


class FlashSkipped(Exception):
    pass


def _log(msg: str) -> None:
    print(f"  [hw] {msg}", file=sys.stderr, flush=True)


# ---- classic ESP32: esptool --------------------------------------------------------------------------------------------

def esptool_command() -> list[str]:
    exe = shutil.which("esptool") or shutil.which("esptool.py")
    if exe is None:
        raise FlashError("esptool is not on PATH (pipx install esptool)")
    return [exe]


def esptool_major() -> int:
    """esptool 5 spells its commands with dashes (write-flash) and warns about the old underscores; 4 knows only those."""
    try:
        out = subprocess.run(esptool_command() + ["version"], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"(\d+)\.\d+", out)
        return int(m.group(1)) if m else 5
    except (subprocess.SubprocessError, OSError):
        return 5


def flash_esp32(port: str, merged: pathlib.Path, baud: int = 115200, chip: str = "esp32", log=_log) -> dict:
    """The whole flash image at 0x0 through the board's bridge; esptool resets into the bootloader before and
    hard-resets after (--after hard-reset, its default), so the board boots the new firmware when this returns."""
    dash = esptool_major() >= 5
    cmd = esptool_command() + ["--chip", chip, "-p", port, "-b", str(baud), "--after", "hard-reset" if dash else "hard_reset",
                               "write-flash" if dash else "write_flash", "0x0", str(merged)]
    log(f"esptool: {merged.name} ({merged.stat().st_size} bytes) -> {port} at {baud}")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    dt = time.monotonic() - t0
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
    if proc.returncode != 0:
        raise FlashError(f"esptool failed ({proc.returncode}) after {dt:.0f} s:\n" + "\n".join(tail))
    log(f"esptool: done in {dt:.1f} s")
    return {"tool": "esptool", "command": " ".join(cmd), "seconds": round(dt, 1), "tail": tail}


def hard_reset(stream, settle_s: float = 0.1) -> None:
    """Reset a classic ESP32 behind a USB-UART bridge the way esptool's --after hard-reset does (esptool.reset.HardReset,
    no flow control): DTR off (IO0 high: not the download mode), RTS on (EN low) for 0.1 s, RTS off. Both lines are
    left deasserted, which the auto-reset transistor pair reads as idle. `stream` is the pyserial port the link holds."""
    stream.dtr = False
    stream.rts = True
    time.sleep(settle_s)
    stream.rts = False


# ---- ESP32-P4: USB DFU 1.1 (the minimum of ArduinoCore-CH32RV tests/bench/dfu.py) ----------------------------------------

DFU_DETACH, DFU_DNLOAD, DFU_UPLOAD, DFU_GETSTATUS, DFU_CLRSTATUS, DFU_GETSTATE, DFU_ABORT = range(7)
DFU_STATES = ["appIDLE", "appDETACH", "dfuIDLE", "dfuDNLOAD-SYNC", "dfuDNBUSY", "dfuDNLOAD-IDLE", "dfuMANIFEST-SYNC",
              "dfuMANIFEST", "dfuMANIFEST-WAIT-RESET", "dfuUPLOAD-IDLE", "dfuERROR"]
DFU_STATUS = ["OK", "errTARGET", "errFILE", "errWRITE", "errERASE", "errCHECK_ERASED", "errPROG", "errVERIFY",
              "errADDRESS", "errNOTDONE", "errFIRMWARE", "errVENDOR", "errUSBR", "errPOR", "errUNKNOWN", "errSTALLEDPKT"]
DFU_IDLE, DFU_DNLOAD_IDLE, DFU_MANIFEST_WAIT_RESET, DFU_ERROR = 2, 5, 8, 10


def usb_device(serial: str, vid: int | None = None, pid: int | None = None):
    """The USB device whose iSerialNumber starts with `serial` (the unit id; an older P4 firmware appends "-hs"),
    whatever VID:PID it carries - or, with vid / pid and no serial, any device of that VID:PID. None when absent."""
    import usb.core
    import usb.util
    for d in usb.core.find(find_all=True):
        if vid is not None and (d.idVendor != vid or d.idProduct != pid):
            continue
        if not serial:
            return d
        if not d.iSerialNumber:
            continue
        try:
            sn = usb.util.get_string(d, d.iSerialNumber) or ""
        except Exception:      # noqa: BLE001 - a device we cannot read is not ours
            continue
        if sn.lower().startswith(serial.lower()):
            return d
    return None


def dfu_interface(dev):
    """(interface number, wTransferSize) from the DFU functional descriptor (class FE / subclass 01)."""
    for intf in dev.get_active_configuration():
        if intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 1:
            extra = bytes(intf.extra_descriptors)
            i = 0
            while i + 1 < len(extra):
                ln, ty = extra[i], extra[i + 1]
                if ty == 0x21 and ln >= 9:
                    _attrs, _detach, xfer, _ver = struct.unpack_from("<BHHH", extra, i + 2)
                    return intf.bInterfaceNumber, xfer
                i += max(ln, 1)
            return intf.bInterfaceNumber, 4096
    raise FlashError("the probe has no DFU interface (class FE/01): its firmware predates oep-probe-arduino 0.0.16")


def dfu_download(serial: str, image: bytes, log=_log) -> dict:
    """Send `image` (the app .bin alone: no bootloader, no partition table) to the running probe's DFU interface; the
    firmware writes the other app slot, verifies it and reboots into it (errVERIFY: not this probe's image, the running
    firmware stays)."""
    import usb.core
    import usb.util
    dev = usb_device(serial)
    if dev is None:
        raise FlashError(f"no USB device with serial {serial} (is the probe attached? usbipd on WSL)")
    n, xfer = dfu_interface(dev)
    try:
        usb.util.claim_interface(dev, n)
    except usb.core.USBError:
        pass                                    # a kernel driver that holds it still lets EP0 through

    def status():
        r = bytes(dev.ctrl_transfer(0xA1, DFU_GETSTATUS, 0, n, 6, 5000))
        return r[0], r[1] | (r[2] << 8) | (r[3] << 16), r[4]

    st, _, state = status()
    if state == DFU_ERROR:
        dev.ctrl_transfer(0x21, DFU_CLRSTATUS, 0, n, b"", 5000)
    log(f"dfu: interface {n}, {xfer}-byte blocks, {len(image)} bytes -> serial {serial}")
    t0 = time.monotonic()
    block = 0
    for off in range(0, len(image), xfer):
        dev.ctrl_transfer(0x21, DFU_DNLOAD, block, n, image[off:off + xfer], 5000)
        while True:
            st, poll, state = status()
            if st:
                raise FlashError(f"dfu block {block}: {DFU_STATUS[st]} ({DFU_STATES[state]})")
            if state == DFU_DNLOAD_IDLE:
                break
            time.sleep(poll / 1000)
        block += 1
    dev.ctrl_transfer(0x21, DFU_DNLOAD, block, n, b"", 5000)          # zero-length: manifest
    for _ in range(100):
        try:
            st, poll, state = status()
        except usb.core.USBError:
            break                               # rebooting into the new image
        if st:
            raise FlashError(f"dfu manifest: {DFU_STATUS[st]} - the running firmware stays")
        if state in (DFU_IDLE, DFU_MANIFEST_WAIT_RESET):
            break
        time.sleep(max(poll, 50) / 1000)
    dt = time.monotonic() - t0
    log(f"dfu: {len(image)} bytes in {block} blocks, {dt:.1f} s")
    return {"tool": "dfu (pyusb)", "interface": n, "transfer_size": xfer, "blocks": block, "seconds": round(dt, 1)}


def usbip_reattach(busid: str | None, log=_log, delay_s: float = 4.0) -> bool:
    """WSL: the device re-enumerates after the reboot and usbipd lets go of it; attach it again. Nothing to do
    without a busid or without usbipd.exe (a Linux host)."""
    if not busid or not shutil.which("usbipd.exe"):
        return False
    time.sleep(delay_s)
    log(f"usbipd: attach --wsl --busid {busid}")
    subprocess.run(["usbipd.exe", "attach", "--wsl", "--busid", busid], text=True, capture_output=True)
    return True


def wait_usb(serial: str, gone_s: float = 10.0, back_s: float = 30.0, log=_log, busid: str | None = None) -> float:
    """Wait for the device to disappear (the reboot) and come back; -> seconds until it was back. Raises FlashError
    when it never reappears."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < gone_s and usb_device(serial) is not None:
        time.sleep(0.2)
    if busid:
        usbip_reattach(busid, log)
    t1 = time.monotonic()
    while time.monotonic() - t1 < back_s:
        if usb_device(serial) is not None:
            log("usb: the probe is back")
            return round(time.monotonic() - t0, 1)
        time.sleep(0.5)
    raise FlashError(f"the probe {serial} did not re-enumerate within {back_s:.0f} s "
                     f"(a WSL bench: usbipd.exe attach --wsl --busid <busid>)")


def flash_p4(serial: str, app: pathlib.Path, busid: str | None = None, log=_log) -> dict:
    rec = dfu_download(serial, app.read_bytes(), log)
    rec["back_after_s"] = wait_usb(serial, log=log, busid=busid)
    return rec


# ---- RP2040 / RP2350: BOOTSEL, then picotool (PICOBOOT) or a mounted drive ---------------------------------------------

BOOT_DEVICES = ((0x2E8A, 0x0003), (0x2E8A, 0x000F))        # the boot ROM's USB device: RP2040, RP2350
PICOTOOL_URL = "https://github.com/raspberrypi/pico-sdk-tools/releases/download/v2.3.1-0/picotool-2.3.1-x86_64-lin.tar.gz"


def bootsel_touch(port: str, log=_log) -> None:
    """Open the CDC port at 1200 baud with DTR low and close it: the Pico SDK USB stack reboots into the boot ROM's
    BOOTSEL mode (the same touch picotool -f and the Arduino IDE use)."""
    import serial
    log(f"bootsel: 1200-baud touch on {port}")
    try:
        s = serial.Serial(port, 1200, timeout=0.1)
        s.dtr = False
        time.sleep(0.1)
        s.close()
    except serial.SerialException as e:
        raise FlashError(f"the 1200-baud touch on {port} failed: {e}") from e


def boot_devices(serial: str | None = None) -> list[dict]:
    """The boot ROM USB devices present (BOOT_DEVICES with an iProduct that says Boot: "RP2 Boot" / "RP2350 Boot" - a
    board's own firmware may carry the same VID:PID, a Waveshare RP2040 Zero does), each as {vid, pid, bus, address,
    serial}; with `serial`, only the one whose serial starts with it (the boot ROM spells the unit id in upper case)."""
    import usb.core
    import usb.util
    out = []
    for vid, pid in BOOT_DEVICES:
        for d in usb.core.find(find_all=True, idVendor=vid, idProduct=pid):
            try:
                product = usb.util.get_string(d, d.iProduct) or "" if d.iProduct else ""
                sn = usb.util.get_string(d, d.iSerialNumber) or "" if d.iSerialNumber else ""
            except Exception:   # noqa: BLE001 - not readable: not usable by picotool either
                continue
            if "boot" not in product.lower():
                continue
            if serial and not sn.lower().startswith(serial.lower()):
                continue
            out.append({"vid": vid, "pid": pid, "bus": d.bus, "address": d.address, "serial": sn, "product": product})
    return out


def wait_bootsel(serial: str | None = None, timeout_s: float = 30.0, log=_log) -> dict | None:
    """Wait for the board's boot ROM USB device to enumerate (on WSL, for usbipd to attach it)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            found = boot_devices(serial)
            if not found and serial:
                # An RP2040's boot ROM carries a fixed serial (not the unit id): the one boot device there is, if alone.
                alone = boot_devices()
                found = alone if len(alone) == 1 else []
        except Exception:   # noqa: BLE001 - no libusb access right now
            found = []
        if found:
            d = found[0]
            log(f"bootsel: {d['product']} {d['vid']:04x}:{d['pid']:04x} serial {d['serial']} at bus {d['bus']} address {d['address']}")
            return d
        time.sleep(0.5)
    return None


def picotool_command() -> list[str] | None:
    exe = os.environ.get("OEP_HW_PICOTOOL") or shutil.which("picotool")
    return [exe] if exe and (os.path.sep not in exe or pathlib.Path(exe).exists()) else None


def picotool_load(uf2: pathlib.Path, device: dict | None = None, log=_log) -> dict:
    """picotool load -x <uf2>: write over PICOBOOT and restart the board (needs it in BOOTSEL). `device` (from
    boot_devices) picks it by bus / address, so another RP2 on the host is never written."""
    cmd = picotool_command()
    if cmd is None:
        raise FlashSkipped(f"flash skipped: no picotool (OEP_HW_PICOTOOL or on PATH; {PICOTOOL_URL})")
    cmd = cmd + ["load", "-x", str(uf2)]
    if device:
        cmd += ["--bus", str(device["bus"]), "--address", str(device["address"])]
    log(f"picotool: {uf2.name} ({uf2.stat().st_size} bytes)")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    dt = time.monotonic() - t0
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
    if proc.returncode != 0:
        raise FlashError(f"picotool failed ({proc.returncode}):\n" + "\n".join(tail))
    log(f"picotool: done in {dt:.1f} s")
    return {"tool": "picotool", "command": " ".join(cmd), "seconds": round(dt, 1), "tail": tail}


def uf2_drive(timeout_s: float = 15.0) -> pathlib.Path | None:
    """OEP_HW_UF2_DRIVE, once INFO_UF2.TXT is there (a host that mounts the BOOTSEL drive); None without the variable
    or when nothing appears in time (WSL mounts none by itself)."""
    root = os.environ.get("OEP_HW_UF2_DRIVE")
    if not root:
        return None
    deadline = time.monotonic() + timeout_s
    while True:
        if (pathlib.Path(root) / "INFO_UF2.TXT").exists():
            return pathlib.Path(root)
        if time.monotonic() > deadline:
            return None
        time.sleep(0.5)


def uf2_copy(uf2: pathlib.Path, drive: pathlib.Path, log=_log) -> dict:
    dst = drive / uf2.name
    log(f"uf2: {uf2.name} ({uf2.stat().st_size} bytes) -> {drive}")
    t0 = time.monotonic()
    try:
        with open(uf2, "rb") as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 64 * 1024)
            out.flush()
            os.fsync(out.fileno())
    except OSError as e:
        raise FlashSkipped(f"flash skipped: the BOOTSEL drive {drive} is not writable from here ({e})") from e
    return {"tool": "uf2 copy", "drive": str(drive), "seconds": round(time.monotonic() - t0, 1)}


def flash_rp2(port: str | None, uf2: pathlib.Path, serial_number: str | None, log=_log, back_s: float = 45.0) -> dict:
    """BOOTSEL (when a CDC port is there to touch; a board already in BOOTSEL has none), then picotool, or the drive
    copy when OEP_HW_UF2_DRIVE names one; then wait for the probe's CDC port to come back (15-20 s on a WSL bench).
    FlashSkipped when neither way is possible from here: the tests go on with the firmware on the board."""
    t0 = time.monotonic()
    if port:
        bootsel_touch(port, log)
    boot = wait_bootsel(serial_number, log=log)
    if boot is None:
        raise FlashSkipped("flash skipped: no RP2 boot ROM USB device appeared after the 1200-baud touch "
                           "(a WSL bench: usbipd must attach 2e8a:0003 / 2e8a:000f)")
    drive = uf2_drive()
    rec = uf2_copy(uf2, drive, log) if drive is not None else picotool_load(uf2, boot, log)
    rec["boot_device"] = f"{boot['product']} {boot['vid']:04x}:{boot['pid']:04x} serial {boot['serial']}"
    if serial_number:
        from .boards import cdc_port
        deadline = time.monotonic() + back_s
        while True:
            try:
                rec["port"] = cdc_port(serial_number)
                break
            except FileNotFoundError:
                if time.monotonic() > deadline:
                    raise FlashError(f"the RP2 probe {serial_number} did not come back as a CDC port within {back_s:.0f} s") from None
                time.sleep(0.5)
    rec["seconds_total"] = round(time.monotonic() - t0, 1)
    return rec
