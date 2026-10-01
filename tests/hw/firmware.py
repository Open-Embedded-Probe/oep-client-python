"""Where the probe firmware comes from (release-testing.ja.md §2), chosen by the environment:

  OEP_PROBE_DIR=/path/to/oep-probe-arduino   build examples/Firmware/OepProbe there with arduino-cli, per profile
  OEP_PROBE_VERSION=0.0.25                   the GitHub release: firmware-<ver>.json names the images and their sha256
  OEP_HW_NOFLASH=1                           neither: test what is on the board already (nothing is flashed)

`obtain(profile)` returns a Firmware with the image paths this profile produced (merged.bin + app .bin for the ESP32s,
.uf2 for the RP2s) and a `source` record for the results (the checkout's commit and dirty flag, or the release
version and URL). Builds and downloads are cached per process, so several boards of one profile share them."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field

RELEASES = "https://github.com/Open-Embedded-Probe/oep-probe-arduino/releases/download"
SKETCH = pathlib.Path("examples") / "Firmware" / "OepProbe"
CACHE = pathlib.Path(os.environ.get("OEP_HW_CACHE", pathlib.Path.home() / ".cache" / "oep-hw"))


class FirmwareError(Exception):
    pass


@dataclass
class Firmware:
    source: dict                           # for the results: kind local / release / on-board, and the details
    version: str | None                    # the firmware string describe should report (None: unknown, on-board)
    merged: pathlib.Path | None = None     # ESP32: the whole flash image (esptool at 0x0)
    app: pathlib.Path | None = None        # ESP32: the app image alone (the P4's DFU)
    uf2: pathlib.Path | None = None        # RP2040 / RP2350
    log: list[str] = field(default_factory=list)

    def image_for(self, kind: str) -> pathlib.Path:
        path = {"esp32": self.merged, "esp32p4": self.app, "rp2": self.uf2}.get(kind)
        if path is None:
            raise FirmwareError(f"no image for a {kind} board in {self.source}")
        return path


def mode() -> str:
    """local, release or on-board, from the environment (OEP_HW_NOFLASH wins; a dir and a version together: an error)."""
    if os.environ.get("OEP_HW_NOFLASH") not in (None, "", "0"):
        return "on-board"
    d, v = os.environ.get("OEP_PROBE_DIR"), os.environ.get("OEP_PROBE_VERSION")
    if d and v:
        raise FirmwareError("set one of OEP_PROBE_DIR and OEP_PROBE_VERSION, not both")
    if d:
        return "local"
    if v:
        return "release"
    raise FirmwareError("set OEP_PROBE_DIR (build a checkout) or OEP_PROBE_VERSION (a GitHub release), "
                        "or OEP_HW_NOFLASH=1 to test the firmware already on the board")


_cache: dict[tuple[str, str], Firmware] = {}


def obtain(profile: str) -> Firmware:
    """The firmware for this profile, built or downloaded once per process."""
    m = mode()
    key = (m, profile)
    if key not in _cache:
        if m == "local":
            _cache[key] = build(pathlib.Path(os.environ["OEP_PROBE_DIR"]), profile)
        elif m == "release":
            _cache[key] = download(os.environ["OEP_PROBE_VERSION"].lstrip("v"), profile)
        else:
            _cache[key] = Firmware({"kind": "on-board", "why": "OEP_HW_NOFLASH"}, None)
    return _cache[key]


# ---- a local checkout ----------------------------------------------------------------------------------------------

def checkout_state(repo: pathlib.Path) -> dict:
    """commit, dirty, version (library.properties) of the checkout, for the record."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()
    state = {"dir": str(repo)}
    try:
        state["commit"] = git("rev-parse", "--short", "HEAD")
        state["dirty"] = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (subprocess.CalledProcessError, FileNotFoundError):
        state["commit"] = None
    props = repo / "library.properties"
    if props.exists():
        for line in props.read_text(encoding="utf-8").splitlines():
            if line.startswith("version="):
                state["version"] = line.split("=", 1)[1].strip()
    return state


def build(repo: pathlib.Path, profile: str, out: pathlib.Path | None = None) -> Firmware:
    """arduino-cli compile --profile <profile> --output-dir <out> examples/Firmware/OepProbe, in the checkout. The
    checkout is only read (the build goes to arduino-cli's own build cache and to `out`)."""
    sketch = repo / SKETCH
    if not (sketch / "sketch.yaml").exists():
        raise FirmwareError(f"{sketch} has no sketch.yaml: OEP_PROBE_DIR must be an oep-probe-arduino checkout")
    if shutil.which("arduino-cli") is None:
        raise FirmwareError("arduino-cli is not on PATH (needed to build OEP_PROBE_DIR)")
    state = checkout_state(repo)
    out = out or CACHE / "build" / f"{state.get('commit') or 'nocommit'}{'-dirty' if state.get('dirty') else ''}" / profile
    out.mkdir(parents=True, exist_ok=True)
    # One build directory per profile: arduino-cli keys its sketch cache by sketch path, so two profiles (an xtensa and a
    # RISC-V ESP32, say) built in turn or at once would link each other's objects ("relocations in generic ELF").
    build_path = CACHE / "build-path" / profile
    build_path.mkdir(parents=True, exist_ok=True)
    cmd = ["arduino-cli", "compile", "--profile", profile, "--build-path", str(build_path), "--output-dir", str(out),
           str(SKETCH)]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
    fw = Firmware({"kind": "local", **state, "profile": profile, "command": " ".join(cmd),
                   "build_seconds": round(time.monotonic() - t0, 1)}, state.get("version"),
                  log=(proc.stdout + proc.stderr).splitlines()[-30:])
    if proc.returncode != 0:
        raise FirmwareError(f"arduino-cli compile failed ({proc.returncode}):\n" + "\n".join(fw.log))
    merged, app, uf2 = out / "OepProbe.ino.merged.bin", out / "OepProbe.ino.bin", out / "OepProbe.ino.uf2"
    fw.merged = merged if merged.exists() else None
    fw.app = app if app.exists() and fw.merged else None
    fw.uf2 = uf2 if uf2.exists() else None
    if not (fw.merged or fw.uf2):
        raise FirmwareError(f"the build left no merged.bin or uf2 in {out}: {sorted(p.name for p in out.iterdir())}")
    fw.source["images"] = {k: str(p) for k, p in (("merged", fw.merged), ("app", fw.app), ("uf2", fw.uf2)) if p}
    return fw


# ---- a GitHub release ----------------------------------------------------------------------------------------------

def fetch(url: str, into: pathlib.Path) -> pathlib.Path:
    """Download `url` to `into` unless it is there (no gh CLI: urllib)."""
    into.parent.mkdir(parents=True, exist_ok=True)
    if not into.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "oep-client-python tests/hw"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        tmp = into.with_suffix(into.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(into)
    return into


def download(version: str, profile: str) -> Firmware:
    """firmware-<version>.json from the release v<version>, then the images this profile needs, sha256-checked."""
    base = f"{RELEASES}/v{version}"
    into = CACHE / "release" / version
    try:
        manifest = json.loads(fetch(f"{base}/firmware-{version}.json", into / f"firmware-{version}.json").read_text("utf-8"))
    except Exception as e:      # noqa: BLE001 - urllib's errors are several kinds
        raise FirmwareError(f"could not fetch firmware-{version}.json from {base}: {e}") from e
    if manifest.get("schema") != 1:
        raise FirmwareError(f"firmware-{version}.json: schema {manifest.get('schema')}, expected 1")
    entries = [e for e in manifest.get("firmware", []) if e.get("example") == "Firmware/OepProbe" and e.get("profile") == profile]
    if not entries:
        raise FirmwareError(f"release {version} has no Firmware/OepProbe build for profile {profile}")
    fw = Firmware({"kind": "release", "version": version, "manifest": f"{base}/firmware-{version}.json",
                   "profile": profile, "images": {}}, manifest.get("version", version))
    for e in entries:
        path = fetch(f"{base}/{e['file']}", into / e["file"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != e["sha256"]:
            path.unlink(missing_ok=True)
            raise FirmwareError(f"{e['file']}: sha256 {digest} is not the release's {e['sha256']}")
        fw.source["images"][e["kind"]] = {"file": e["file"], "sha256": digest, "model": e.get("model")}
        if e["kind"] == "merged":
            fw.merged = path
        elif e["kind"] == "app":
            fw.app = path
        elif e["kind"] == "uf2":
            fw.uf2 = path
    return fw


if __name__ == "__main__":       # python -m tests.hw.firmware <profile>: fetch or build by hand, print the record
    fw = obtain(sys.argv[1] if len(sys.argv) > 1 else "esp32")
    print(json.dumps({"version": fw.version, "source": fw.source}, indent=2))
