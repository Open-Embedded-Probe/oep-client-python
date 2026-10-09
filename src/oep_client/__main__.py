"""The oep command: what a probe offers (dump) and its settings (config).

  oep dump --port /run/board-identify/by-id/<probe>        oep dump --virtual p4-x035 --prefix oep.target --json
  oep config show <probe>                 (the settings and what the probe declares; lock-free)
  oep config state <probe>                (the live slot / bind / storage state; lock-free)
  oep config slot <probe> --name x035 --wire rvswd --pins 2,54 --attach at-boot --retry 1 --mechanism dmseq
  oep config bind <probe> --port 1 --stream slot:x035      (the one stream a serial port carries)
  oep config plan <probe> oep.fixture.uart#1 rx=48 tx=49       (the fn's whole plan; fn number or name#instance)
  oep config uart <probe> oep.fixture.uart#1 115200 --format 8N1
  oep config disable <probe> 3 4           (channels the probe never uses or touches; remove disable CH re-enables)
  oep config remove <probe> bind 1        oep config save <probe>        oep config erase <probe>
  oep config wifi <probe> --index 0 --ssid LAB --pass-prompt [--save]   (a network the probe joins, probe.config §1.4;
                                           the passphrase is asked without echo or read from --pass-env VAR, never
                                           printed; without either the entry keeps its passphrase)
  oep config wifi <probe> --from-env [--save]   (OEP_WIFI_SSID_<n> / OEP_WIFI_PASS_<n>, n = the index)
  oep config wifi-unset <probe> --index 0 [--save]
  oep find [--timeout 2] [--json] [--no-verify]   (probes announcing _oep._tcp by DNS-SD over mDNS: unit_id, host,
                                           port, IP; each verified first by confirm + describe's unit_id, host guide
                                           §4.1 - one that fails is dropped with a warning)
  oep speed <probe> [--candidates 921600,500000] [--verify [--flows in:2,out:2]] (port_speed, oep-if-link §3 and the host
                                           guide §17: try the candidates in order on a UART bridge, report; default
                                           500000 - a faster rate only when named, after its 1 s verify each way)
  oep linktest <probe> --rates now,921600 --patterns in,out,duplex --inflight 1,2 --sizes 128,496 --frames 300
  oep pins <probe> --power 5 --wire swio  (find the target's debug pins and reset line: classify the channels, scan,
                                           identify, hold-low + attach under reset; prints a slot, --save writes it)
  oep clock <probe> [-n 8] [--json]       (fn 0's clock, core §7.7: the probe's uptime_ns and boot_id against this
                                           host's time - the reading with the shortest round trip of n)
  oep restart <probe> [--reopen-s S]      (oep.probe.restart: the probe restarts; waits until it is back, its new boot_id)

<probe>: a serial port, tcp://HOST[:PORT] (no port: the one DNS-SD finds), tcp:UNIT_ID (found by DNS-SD) or
usb[:VID:PID[:SERIAL]]. A change takes the lock (owner "oep config") and
ends the session after it; it takes effect at once, and stays over a restart only after `save` (or --save).
"""

from __future__ import annotations

import argparse
import sys

import json
import struct

from . import catalog, config, core, dump, virtual_bench, host, link

USB_HINT = ("install the usb / hid extras (pip install 'oep-client-python[usb-async,hid]') so every way in to the USB "
            "device can be tried, or name the probe's port (a serial port such as /dev/ttyACM0 or COM3, tcp://HOST:PORT)")


def _open_host(target: str, **kw):
    """link.open_host for a command: a probe that cannot be opened (no device, no port, not an OEP probe, a value the
    host does not use) ends the command with one line on stderr instead of a traceback; for a usb target the line
    says how to get another way in."""
    try:
        return link.open_host(target, **kw)
    except (OSError, ValueError, ImportError, host.OepError) as e:
        why = " ".join(str(e).split()).rstrip(".") or type(e).__name__
        hint = f" Hint: {USB_HINT}." if isinstance(e, FileNotFoundError) and (target == "usb" or target.startswith("usb:")) \
            else ""
        raise SystemExit(f"oep: cannot open {target}: {why}.{hint}") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="oep", description="Open Embedded Probe: what a probe offers, its settings")
    sub = parser.add_subparsers(dest="command", required=True)
    _config_parser(sub)
    sp = sub.add_parser("speed", help="port_speed (oep-if-link §3, host guide §17): try faster rates on a UART bridge, "
                        "print the report")
    sp.add_argument("probe", help="the probe's serial port (a UART bridge this host opens)")
    sp.add_argument("rates", nargs="?", default="", help="the candidates, comma-separated, in order of preference "
                    "(the first that passes is kept; default 500000). Same as --candidates")
    sp.add_argument("--candidates", default="", help="the candidates, comma-separated (default 500000, the default "
                    "ceiling of oep-if-link §3 obligation 7). A rate above 500000 is tried only when named here, and "
                    "is committed only after full frames ran 1 s each way (in, out; duplex too with --verify's "
                    "default flows) at it (host guide §17.3.3)")
    sp.add_argument("--flows", default="", help="the flows to verify, comma-separated FLOW[:N] (in, out, duplex; N in "
                    "flight, 0 = the most the link keeps; default: all three at that N). Selects --verify")
    form = sp.add_mutually_exclusive_group()
    form.add_argument("--verify", action="store_true", help="the full form (guide §17.3): baseline, the flows measured "
                      "at every candidate, a failed flow run again one at a time")
    form.add_argument("--minimal", action="store_true", help="the minimal form (guide §17.2, the default): try, confirm, "
                      "commit, no measurement")
    sp.add_argument("--frames", type=int, default=link.FLOW_FRAMES, help="frames per flow when verifying (default 16)")
    sp.add_argument("--no-record", action="store_true", help="do not read or write the record of passed / failed rates "
                    "(~/.cache/oep-client/link-speed.json: a pass 30 days, a failure 1 day; on by default here, off "
                    "in the library)")
    sp.add_argument("--max-tries", type=int, default=None, help="the most candidates tried after the record ordered "
                    "them (default: all)")
    sp.add_argument("--baud", type=int, default=link.BASE_BAUD, help="the boot speed (default 115200)")
    sp.add_argument("--json", action="store_true")
    lt = sub.add_parser("linktest", help="measure the link: traffic patterns at rates the host asks for, what breaks")
    lt.add_argument("probe", help="a probe: a serial port, tcp://HOST:PORT or usb[:VID:PID]")
    lt.add_argument("--rates", default="", help="comma-separated rates to switch to with port_speed and measure at "
                    "(each on its own; 0 or 'now' = the speed in force). Default: only the speed in force")
    lt.add_argument("--patterns", default="in,out,duplex", help="in (probe->host), out (host->probe), duplex")
    lt.add_argument("--inflight", default="1", help="comma-separated in-flight counts (above the probe's max: skipped)")
    lt.add_argument("--sizes", default="", help="comma-separated frame payload sizes (default: a whole frame)")
    lt.add_argument("--frames", type=int, default=300, help="frames per cell (default 300)")
    lt.add_argument("--seconds", type=float, default=None, help="per cell for this long instead of --frames")
    lt.add_argument("--timeout", type=float, default=None, help="seconds to wait for one answer (default 0.3; 3 over "
                    "TCP, where Wi-Fi retransmissions stall answers for 1-4 s)")
    lt.add_argument("--baud", type=int, default=link.BASE_BAUD, help="the boot speed to open at (default 115200)")
    lt.add_argument("--low-latency", choices=("on", "off"), default="on", help="the serial driver's low-latency mode")
    lt.add_argument("--json", action="store_true", help="one JSON object per cell")
    pn = sub.add_parser("pins", help="find where a target is wired: classify the channels, scan the wire, find the "
                        "reset line, suggest a slot (writes nothing without --save)")
    pn.add_argument("probe", help="a probe: a serial port, tcp://HOST:PORT or usb[:VID:PID[:SERIAL]]")
    pn.add_argument("--power", type=int, help="the gpio channel that powers the target: switched off, then on, to "
                    "see which channels follow it (never touched without this)")
    pn.add_argument("--exclude", default="", help="channels never to read, scan or hold, comma-separated")
    pn.add_argument("--wire", choices=("swio", "rvswd", "swd"), default="swio")
    pn.add_argument("--steps", default="classify,hold,scan,identify,reset,slot",
                    help="the steps to run, comma-separated (default all: classify, hold, scan, identify, reset, slot)")
    pn.add_argument("--save", action="store_true", help="write the suggested slot (config set, then save)")
    pn.add_argument("--slot", type=int, default=0, help="--save: the slot number (default 0)")
    pn.add_argument("--name", help="--save: the slot's name (default: the target family, else 'target')")
    pn.add_argument("--json", action="store_true", help="the report as JSON (the steps' lines go to stderr)")
    ck = sub.add_parser("clock", help="the probe's clock (fn 0 clock, core §7.7) against this host's: lock-free")
    ck.add_argument("probe", help="a probe: a serial port, tcp://HOST:PORT or usb[:VID:PID[:SERIAL]]")
    ck.add_argument("-n", type=int, default=8, help="readings; the one with the shortest round trip is kept (default 8)")
    ck.add_argument("--json", action="store_true")
    rs = sub.add_parser("restart", help="restart the probe (oep.probe.restart, optional) and wait until it is back")
    rs.add_argument("probe", help="a probe: a serial port, tcp://HOST:PORT or usb[:VID:PID[:SERIAL]]")
    rs.add_argument("--force", action="store_true", help="take the lock from its holder")
    rs.add_argument("--reopen-s", type=float, default=0.0, metavar="S",
                    help="when the probe is not back within its restart_max_ms, open it again for up to S more seconds "
                         "(your reopen: a host where the device returns late, e.g. WSL re-attaching it through usbipd)")
    fd = sub.add_parser("find", help="probes on the local network that announce _oep._tcp (DNS-SD over mDNS)")
    fd.add_argument("--timeout", type=float, default=2.0, help="seconds to listen for answers (default 2)")
    fd.add_argument("--engine", choices=("auto", "zeroconf", "minimal"), default="auto",
                    help="python-zeroconf (the mdns extra) or this package's one-shot query; auto: zeroconf if installed")
    fd.add_argument("--json", action="store_true")
    fd.add_argument("--no-verify", action="store_true",
                    help="list every announced instance as it is (default: only those that answer confirm and whose "
                         "describe unit_id is their TXT unit_id, host guide §4.1)")
    fd.add_argument("--verify-timeout", type=float, default=1.0, metavar="S",
                    help="seconds for each answer of the verify (default 1)")
    d = sub.add_parser("dump", help="list and describe every interface a probe offers")
    src = d.add_mutually_exclusive_group(required=True)
    src.add_argument("--virtual", choices=sorted(virtual_bench.PROFILES), help="an in-process virtual bench (no hardware)")
    src.add_argument("--port", help="a probe: a serial port, tcp://HOST:PORT or usb[:VID:PID] (lock-free reads only)")
    d.add_argument("--prefix", default="", help="only names under this namespace (label boundaries)")
    d.add_argument("--exact", action="store_true", help="the prefix is a whole name")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.command == "config":
        return _config(args)
    if args.command == "speed":
        return _speed(args)
    if args.command == "linktest":
        return _linktest(args)
    if args.command == "pins":
        return _pins_cmd(args)
    if args.command == "clock":
        return _clock_cmd(args)
    if args.command == "restart":
        return _restart_cmd(args)
    if args.command == "find":
        return _find_cmd(args)

    confirm = (0, 1)
    if args.virtual:
        call = virtual_bench.PROFILES[args.virtual]().call
    else:
        hst = _open_host(args.port)
        call = lambda fn, op, payload: hst.request(fn, op, payload, locked=False).payload   # noqa: E731
        confirm = hst.confirm_range()                    # confirmed already: the revision in use (core §7.1)
    caps = dump.collect(call, args.prefix, args.exact, confirm)
    sys.stdout.write(dump.to_json(caps) + "\n" if args.json else dump.to_text(caps))
    return 0


# ---- oep find ------------------------------------------------------------------------------------------------------

def _find_cmd(args) -> int:
    """DNS-SD browse for _oep._tcp (transports §3, host guide §4.1): what each instance says - its TXT unit_id, the SRV
    host and port, the addresses. Each is verified first (discovery.verify: confirm and describe's unit_id, all at
    once; no session opened) and one that fails is dropped with a warning on stderr; --no-verify lists them raw."""
    from . import discovery
    try:
        found = discovery.browse(args.timeout, args.engine)
    except ImportError:
        raise SystemExit("oep find --engine zeroconf: install the mdns extra (pip install 'oep-client-python[mdns]')") \
            from None
    dropped = []
    if not args.no_verify:
        found, dropped = discovery.verify(found, args.verify_timeout)
        for f, why in dropped:
            print(f"warning: dropped {f.target or f.host or '?'} ({f.instance}, TXT unit_id {f.unit_id or 'none'}): not "
                  f"verified as an OEP probe - {why}", file=sys.stderr)
    if args.json:
        print(json.dumps([{"unit_id": f.unit_id, "instance": f.instance, "host": f.host, "port": f.port,
                           "addresses": f.addresses, "target": f.target} for f in found], indent=2))
        return 0
    if not found and dropped:
        print(f"no verified probe: {len(dropped)} instance(s) of _oep._tcp dropped (above)", file=sys.stderr)
        return 1
    if not found:
        print(f"no probe announces _oep._tcp within {args.timeout:g} s (mDNS stays on the local link: behind a NAT or "
              "a router name the probe as tcp://HOST:PORT)", file=sys.stderr)
        return 1
    for f in found:
        print(f"{f.unit_id or '(no unit_id)'}  {f.host or '?'}  port {f.port or '?'}  "
              f"{', '.join(f.addresses) or 'no address'}  {f.target}  ({f.instance})")
    return 0


# ---- oep clock / oep restart ---------------------------------------------------------------------------------------

def _clock_cmd(args) -> int:
    """The shortest round trip of n clock readings (host guide §12): the probe's uptime_ns stands for the midpoint of
    this host's send and receive times (time.monotonic_ns), within half the round trip."""
    hst = _open_host(args.probe)
    r = hst.clock_best(max(1, args.n))
    out = {"boot_id": r.boot_id, "uptime_ns": r.uptime_ns, "host_before_ns": r.before_ns, "host_after_ns": r.after_ns,
           "round_trip_ns": r.round_trip_ns, "host_ns": r.host_ns, "uncertainty_ns": r.uncertainty_ns}
    if args.json:
        print(json.dumps(out))
    else:
        print(f"boot_id 0x{r.boot_id:08X}, uptime {r.uptime_ns / 1e9:.6f} s at host monotonic {r.host_ns / 1e9:.6f} s "
              f"+/- {r.uncertainty_ns / 1e3:.1f} us (round trip {r.round_trip_ns / 1e3:.1f} us, best of {args.n})")
    return 0


def _restart_cmd(args) -> int:
    hst = _open_host(args.probe)
    try:
        core.restart_fn(hst)
    except LookupError:
        raise SystemExit("oep: the probe offers no oep.probe.restart (an optional interface)") from None
    core.take(hst, owner="oep restart", force=args.force)
    before = hst.limits["boot_id"] if hst.limits else None
    after = hst.restart_probe(reopen_s=args.reopen_s)
    if hst.restart_reopened:
        print(f"not back within restart_max_ms; reopened within --reopen-s {args.reopen_s:g}")
    print(f"restarted: boot_id 0x{before:08X} -> 0x{after:08X}" if before is not None else f"restarted: boot_id 0x{after:08X}")
    return 0


# ---- oep pins -----------------------------------------------------------------------------------------------------

def _pins_cmd(args) -> int:
    from . import pins
    exclude = [int(x, 0) for x in args.exclude.replace(" ", "").split(",") if x]
    say = (lambda t: print(t, file=sys.stderr, flush=True)) if args.json else (lambda t: print(t, flush=True))  # noqa: E731
    hst = _open_host(args.probe)
    try:
        core.take(hst, 10000, owner="oep pins")
        finder = pins.PinFinder(hst, wire=args.wire, power=args.power, exclude=exclude, say=say, probe=args.probe,
                                save=args.save, slot=args.slot, name=args.name)
        report = finder.run(steps=tuple(s for s in args.steps.split(",") if s))
        say(f"done in {report.elapsed_s:.1f} s")
        if args.json:
            print(json.dumps(report.as_dict(), indent=2))
        try:
            hst.end()
        except host.OepError:
            pass
    except host.Rejected as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        hst.link.close()
    return 0 if report.resolution == 'unique-candidate' else 1


# ---- oep linktest -------------------------------------------------------------------------------------------------

def _linktest(args) -> int:
    """Every parameter on the command line: no fixed numbers in the measurement."""
    import dataclasses
    import json
    from . import linktest
    ints = lambda text: [int(v) for v in text.split(",") if v.strip()]   # noqa: E731
    rates = [None if v.strip() in ("0", "now") else int(v) for v in args.rates.split(",") if v.strip()] or [None]
    hst = _open_host(args.probe, baud=args.baud) if args.probe.startswith("/") or ":" not in args.probe \
        else _open_host(args.probe)
    stream = getattr(hst.link, "stream", None)
    if hasattr(stream, "set_low_latency_mode"):
        try:
            stream.set_low_latency_mode(args.low_latency == "on")
        except (OSError, ValueError, NotImplementedError):
            pass
    try:
        core.take(hst, 30000, owner="oep linktest")
        for result in linktest.matrix(hst, rates=rates, patterns=[p for p in args.patterns.split(",") if p],
                                      inflight=ints(args.inflight), sizes=ints(args.sizes) or None,
                                      frames=args.frames, seconds=args.seconds, timeout=args.timeout):
            if args.json:
                for c in result.cells:
                    print(json.dumps({**dataclasses.asdict(c), "actual": result.actual, "low_latency": args.low_latency}), flush=True)
                if result.why:
                    print(json.dumps({"rate": result.rate, "why": result.why}), flush=True)
            else:
                print(result.text(), flush=True)
        try:
            hst.end()
        except Exception:
            pass
    finally:
        hst.link.close()
    return 0


# ---- oep speed ----------------------------------------------------------------------------------------------------

def _flows(text: str) -> list[tuple[str, int]] | None:
    """--flows "in:2,out,duplex:1" -> [("in", 2), ("out", 0), ("duplex", 1)]; "" -> None (the default flows)."""
    out = []
    for item in text.replace(" ", "").split(","):
        if not item:
            continue
        name, _, n = item.partition(":")
        out.append((name, int(n) if n else 0))
    return out or None


def _speed(args) -> int:
    """Take the lock, raise_speed by the host guide's procedure, print the report; the session's end puts the port
    back at its boot speed. The record of passed / failed rates is on unless --no-record."""
    import dataclasses
    text = args.candidates or args.rates
    candidates = [int(r) for r in text.replace(" ", "").split(",") if r] or list(link.DEFAULT_CANDIDATES)
    flows = _flows(args.flows)
    verify = True if args.verify or flows else (False if args.minimal else None)
    hst = _open_host(args.probe, baud=args.baud)
    try:
        core.take(hst, max(5000, link.lease_for(candidates, flows, verify)), owner="oep speed")
        report = link.raise_speed(hst, candidates, flows=flows, verify=verify, frames=args.frames,
                                   record=not args.no_record, max_tries=args.max_tries)
        if args.json:
            sys.stdout.write(json.dumps(dataclasses.asdict(report), indent=2) + "\n")
        else:
            sys.stdout.write(report.to_text())
        hst.end()
    finally:
        hst.link.close()
    return 0 if report.supported else 2


# ---- oep config ----------------------------------------------------------------------------------------------------

def _config_parser(sub) -> None:
    c = sub.add_parser("config", help="the probe's settings (oep.probe.config): slots, binds, save")
    cs = c.add_subparsers(dest="action", required=True)
    show = cs.add_parser("show", help="the settings, what the probe declares, and the live slot / bind state")
    show.add_argument("probe")
    show.add_argument("--json", action="store_true")
    state = cs.add_parser("state", help="the live slot / bind / storage state (op state, lock-free)")
    state.add_argument("probe")
    state.add_argument("--json", action="store_true")
    slot = cs.add_parser("slot", help="register a slot (a place a target is wired to)")
    slot.add_argument("probe")
    slot.add_argument("--slot", type=int, default=0, help="the slot number (default 0)")
    slot.add_argument("--name", required=True, help="1-32 of a-z 0-9 - _ (the oep://<probe>/<name> address)")
    slot.add_argument("--wire", help="rvswd or swio (or an fn); default: the probe's only wire")
    slot.add_argument("--pins", help="swdio,swclk (one pin on swio); default: the wire's only pin set")
    slot.add_argument("--attach", choices=sorted(config.ATTACH), default="host")
    slot.add_argument("--retry", type=float, default=0, help="at-boot: try again every N s while absent (0: never)")
    slot.add_argument("--mechanism", choices=sorted(config.MECHANISM), default="dmseq",
                      help="the console's mechanism, or none (no console on this slot)")
    slot.add_argument("--max-speed", type=int, default=0,
                      help="the line's ceiling in Hz for the probe's own attach (0: none); the target's, e.g. 1000000")
    slot.add_argument("--idle-clock", choices=sorted(config.IDLE_CLOCK), default="high",
                      help="rvswd: SWCLK while the line rests (the target's: low on CH32L103 / V203)")
    slot.add_argument("--save", action="store_true", help="save after the change")
    bind = cs.add_parser("bind", help="the one stream a serial port carries (set it again to change it)")
    bind.add_argument("probe")
    bind.add_argument("--port", type=int, required=True, help="the serial port (its transport index, see show)")
    bind.add_argument("--stream", required=True, help="slot:NAME, slot:N or uart:FN")
    bind.add_argument("--save", action="store_true")
    plan = cs.add_parser("plan", help="the pins an interface keeps (its whole plan, kept as a setting)")
    plan.add_argument("probe")
    plan.add_argument("fn", help="an fn, or name#instance (the list instance, 0 = the first; #0 may be left out, core §7.2)")
    plan.add_argument("roles", nargs="+", help="ROLE=CHANNEL, ROLE a number or the interface's role name (rx, tx, line...)")
    plan.add_argument("--save", action="store_true")
    label = cs.add_parser("label", help="name a channel (a setting, read with config get)")
    label.add_argument("probe")
    label.add_argument("channel", type=int)
    label.add_argument("text")
    label.add_argument("--save", action="store_true")
    idle = cs.add_parser("idle", help="the state of a free pin")
    idle.add_argument("probe")
    idle.add_argument("channel", type=int)
    idle.add_argument("mode", choices=sorted(config.IDLE))
    drive = idle.add_mutually_exclusive_group()
    drive.add_argument("--drive-ma", type=int, help="output modes: the strongest drive level of about this many mA or "
                                                    "less, picked here from the gpio's describe drive_levels")
    drive.add_argument("--drive-level", type=int, help="output modes: a drive level number of this probe")
    idle.add_argument("--save", action="store_true")
    dis = cs.add_parser("disable", help="channels the probe never uses or touches (not on this board)")
    dis.add_argument("probe")
    dis.add_argument("channels", type=int, nargs="+")
    dis.add_argument("--save", action="store_true")
    uart = cs.add_parser("uart", help="a fixture UART's baud / format, applied whenever its plan has RX or TX")
    uart.add_argument("probe")
    uart.add_argument("fn", help="an fn, or name#instance of the oep.fixture.uart")
    uart.add_argument("baud", type=int)
    uart.add_argument("--format", default="8N1", help="data bits, parity, stop bits: 8N1 (default), 8E1, 7O2 ...")
    uart.add_argument("--save", action="store_true")
    rm = cs.add_parser("remove", help="remove one item (unset): slot N, bind PORT, plan FN, label CH, idle CH, uart FN, disable CH")
    rm.add_argument("probe")
    rm.add_argument("kind", choices=sorted(config.ITEM))
    rm.add_argument("key", type=int)
    rm.add_argument("--save", action="store_true")
    wifi = cs.add_parser("wifi", help="a Wi-Fi network the probe joins (the wifi item; the passphrase is never printed)")
    wifi.add_argument("probe")
    wifi.add_argument("--index", type=int, help="the entry (tried in index order; below the probe's wifi_max)")
    wifi.add_argument("--ssid", help="the network's name (1-32 bytes)")
    pw = wifi.add_mutually_exclusive_group()
    pw.add_argument("--pass-prompt", action="store_true", help="ask for the passphrase (no echo)")
    pw.add_argument("--pass-env", metavar="VAR", help="read the passphrase from environment variable VAR")
    pw.add_argument("--open", action="store_true", help="no passphrase: an open network")
    pw.add_argument("--from-env", action="store_true", help="every entry from OEP_WIFI_SSID_<n> / OEP_WIFI_PASS_<n> "
                    "(n = the index; no PASS: open); entries whose SSID and passphrase presence already match are not "
                    "sent again (a passphrase cannot be compared: --force sends them)")
    wifi.add_argument("--force", action="store_true", help="--from-env: send every entry, passphrases included")
    wifi.add_argument("--save", action="store_true")
    wun = cs.add_parser("wifi-unset", help="remove Wi-Fi entries (the wifi item of these indexes)")
    wun.add_argument("probe")
    wun.add_argument("--index", type=int, action="append", required=True, help="an entry's index (repeatable)")
    wun.add_argument("--save", action="store_true")
    for name in ("save", "erase"):
        cs.add_parser(name, help=f"{name} the stored settings").add_argument("probe")


def _pins(hst, fn: int, text: str | None, wire: str) -> tuple[int, int]:
    if text:
        parts = [int(x, 0) for x in text.split(",")]
        return (parts[0], parts[1] if len(parts) > 1 else 0xFFFF)
    groups = []
    for tag, v in core.describe(hst, fn):
        if tag & 0x7F == catalog.CHANNEL_GROUP:
            roles = dict(catalog.unpack_channel_group(v)[1])
            groups.append((roles.get(1, 0xFFFF), roles.get(2, 0xFFFF)))
    if len(groups) != 1:
        raise SystemExit(f"{wire}: {len(groups)} pin sets on this probe - name one with --pins")
    return groups[0]


def _plan_fn(hst, spec: str) -> tuple[int, dict[str, int]]:
    """fn and its role names (from the registry) for `spec`: an fn number, or name[#instance] (the list's instance,
    0-based, core §7.2)."""
    from . import registry as reg
    if spec.isdigit():
        fn = int(spec)
        name = next((e.name for e in core.list_entries(hst) if e.fn == fn), "")
    else:
        name, _, k = spec.partition("#")
        entries = [e for e in core.list_entries(hst, name, exact=True)]
        if not entries:
            raise SystemExit(f"the probe offers no {name}")
        instance = int(k) if k else 0
        fn = next((e.fn for e in entries if e.instance == instance), None)
        if fn is None:
            raise SystemExit(f"{name}: instances {sorted(e.instance for e in entries)}, not {instance}")
    iface = reg.INTERFACES.get(name)
    roles = dict(iface.enum.get("role", {})) if iface else {}
    return fn, roles


def _wire_fn(hst, wire: str | None) -> int:
    if wire is None:
        wires = [e for e in core.list_entries(hst, "oep.wire") if e.name in ("oep.wire.rvswd", "oep.wire.swio")]
        if len(wires) != 1:
            raise SystemExit("this probe has " + (", ".join(f"{e.name} (fn {e.fn})" for e in wires) or "no RISC-V wire")
                             + ": name one with --wire")
        return wires[0].fn
    name = wire if wire.startswith("oep.") else f"oep.wire.{wire}"
    if wire.isdigit():
        return int(wire)
    try:
        return core.find(hst, name)
    except LookupError:
        raise SystemExit(f"this probe does not offer {name}") from None


def _stream(spec: str, slots: dict[str, int]) -> tuple[str, int]:
    kind, _, key = spec.partition(":")
    if kind not in config.STREAM or not key:
        raise SystemExit(f"--stream {spec}: want slot:NAME, slot:N or uart:FN")
    if kind == "slot" and not key.isdigit():
        if key not in slots:
            raise SystemExit(f"--stream {spec}: no slot named {key} (see oep config show)")
        return kind, slots[key]
    return kind, int(key)


def _change(hst, cfg, items, save: bool) -> None:
    core.take(hst, 3000, owner="oep config")
    try:
        h = cfg.set(items)
        print(f"set: hash 0x{h:08x}")
        if save:
            if cfg.needs_save():
                print(f"saved: hash 0x{cfg.save():08x}")
            else:
                print("saved already (storage_hash is the settings' hash): not written")
    finally:
        hst.end()


def _config(args) -> int:
    hst = _open_host(args.probe)
    try:
        cfg = config.ProbeConfig(hst)
        if args.action == "show":
            return _show(hst, cfg, args.json)
        if args.action == "state":
            return _state(cfg, args.json)
        if args.action == "slot":
            fn = _wire_fn(hst, args.wire)
            args.wire = args.wire or str(fn)
            it = config.Slot(slot=args.slot, wire_fn=fn, pins=_pins(hst, fn, args.pins, args.wire), name=args.name,
                             attach=args.attach, retry_s=args.retry, mechanism=args.mechanism,
                             max_speed=args.max_speed, idle_clock=args.idle_clock)
            _change(hst, cfg, [it], args.save)
        elif args.action == "bind":
            slots = {it.name: it.slot for it in cfg.items() if isinstance(it, config.Slot)}
            _change(hst, cfg, [config.Bind(port=args.port, stream=_stream(args.stream, slots))], args.save)
        elif args.action == "plan":
            fn, roles = _plan_fn(hst, args.fn)
            items = []
            for spec in args.roles:
                role, _, ch = spec.partition("=")
                if not ch:
                    raise SystemExit(f"{spec}: want ROLE=CHANNEL")
                number = int(role) if role.isdigit() else roles.get(role.lower())
                if number is None:
                    raise SystemExit(f"{role}: not a role of fn {fn} (roles: {', '.join(sorted(roles)) or 'numbers only'})")
                items.append(config.Plan(fn=fn, role=number, channel=int(ch, 0)))
            _change(hst, cfg, items, args.save)
        elif args.action == "uart":
            fn, _ = _plan_fn(hst, args.fn)
            from .fixture import FixtureUart
            fmt = args.format.upper()
            try:
                byte = FixtureUart.format_byte(int(fmt[0]), fmt[1], int(fmt[2]))
            except (KeyError, ValueError, IndexError):
                raise SystemExit(f"--format {args.format}: want <data bits><parity><stop bits>, e.g. 8N1, 8E2, 7O1") from None
            _change(hst, cfg, [config.Uart(fn=fn, baud=args.baud, format=byte)], args.save)
        elif args.action == "label":
            _change(hst, cfg, [config.Label(channel=args.channel, text=args.text)], args.save)
        elif args.action == "idle":
            from .fixture import Drive, Gpio
            drive = Drive.level(args.drive_level) if args.drive_level is not None else None
            if args.drive_ma is not None:
                levels = Gpio(hst).drive_levels()
                if levels is None:
                    raise SystemExit("--drive-ma: this probe declares no drive_levels (its strength cannot be chosen)")
                drive = levels.at_most(args.drive_ma)
            if drive is not None and args.mode not in ("output-low", "output-high"):
                raise SystemExit(f"--drive-ma / --drive-level go with output-low / output-high, not {args.mode}")
            _change(hst, cfg, [config.Idle(channel=args.channel, mode=args.mode, drive=drive)], args.save)
        elif args.action == "disable":
            _change(hst, cfg, [config.Disable(channel=ch) for ch in args.channels], args.save)
        elif args.action == "remove":
            _change(hst, cfg, [config.remove(args.kind, args.key)], args.save)
        elif args.action == "wifi":
            _wifi_set(hst, cfg, args)
        elif args.action == "wifi-unset":
            _change(hst, cfg, [config.remove("wifi", i) for i in args.index], args.save)
        else:
            core.take(hst, 3000, owner="oep config")
            try:
                if args.action == "save":
                    print(f"saved: hash 0x{cfg.save():08x}")
                else:
                    cfg.erase()
                    print("erased (the settings now in effect stay until the probe restarts)")
            finally:
                hst.end()
        return 0
    except host.Rejected as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        hst.link.close()


def _wifi_set(hst, cfg, args) -> None:
    """oep config wifi (host guide §15.1): the passphrase from a prompt without echo or an environment variable, never
    from the command line, never printed; without one the entry keeps its passphrase (pass_len 0xFF) - an entry that
    does not exist yet needs --pass-prompt, --pass-env or --open."""
    import os
    decl = cfg.describe()
    if config.ITEM["wifi"] not in decl.items:
        raise SystemExit("this probe has no wifi item (describe's items)")
    if args.from_env:
        wanted = config.wifi_from_env(count=decl.wifi_max)
        if not wanted:
            raise SystemExit("--from-env: no OEP_WIFI_SSID_<n> set (n = 0 .. wifi_max - 1)")
        have = {it.index: it for it in cfg.items() if isinstance(it, config.Wifi)}
        send = [w for w in wanted if args.force or not config.same_items([have[w.index]] if w.index in have else [], [w])]
        for w in wanted:
            print(f"wifi {w.index}: " + ("sent" if w in send else "unchanged (ssid and passphrase presence match)"))
        if send:
            _change(hst, cfg, send, args.save)
        elif args.save and cfg.needs_save():
            core.take(hst, 3000, owner="oep config")
            try:
                print(f"saved: hash 0x{cfg.save():08x}")
            finally:
                hst.end()
        return
    if args.index is None or args.ssid is None:
        raise SystemExit("oep config wifi: --index and --ssid (or --from-env)")
    if not 0 <= args.index < max(decl.wifi_max, 1):
        raise SystemExit(f"--index {args.index}: 0 to {decl.wifi_max - 1} (the probe's wifi_max is {decl.wifi_max})")
    if args.pass_prompt:
        import getpass
        passphrase = getpass.getpass(f"passphrase for {args.ssid!r} (not shown): ")
    elif args.pass_env:
        passphrase = os.environ.get(args.pass_env)
        if passphrase is None:
            raise SystemExit(f"--pass-env {args.pass_env}: not set")
    elif args.open:
        passphrase = None
    else:
        if not any(isinstance(it, config.Wifi) and it.index == args.index for it in cfg.items()):
            raise SystemExit(f"wifi entry {args.index} does not exist yet: give --pass-prompt, --pass-env VAR or --open")
        passphrase = config.KEEP
    it = config.Wifi(index=args.index, ssid=args.ssid, passphrase=passphrase)
    try:
        it.value()
    except ValueError as e:
        raise SystemExit(str(e)) from None
    _change(hst, cfg, [it], args.save)
    print(f"wifi {args.index}: {it.shown()['ssid']!r}, passphrase {it.shown()['passphrase']}")


def _plain(o):
    if hasattr(o, "shown"):
        return o.shown()                                           # a wifi item: never its passphrase
    return {k: (v.hex() if isinstance(v, bytes) else v) for k, v in vars(o).items()} if hasattr(o, "__dict__") \
        else list(o)


def _state_dict(st) -> dict:
    return {**{k: v for k, v in vars(st).items() if k not in ("slots", "binds", "wifi")},
            "slots": [_plain(s) for s in st.slots], "binds": [_plain(b) for b in st.binds],
            **({"wifi": vars(st.wifi)} if st.wifi is not None else {})}


def _state(cfg, as_json: bool) -> int:
    """The live state alone (op state, lock-free): what a monitor polls."""
    st = cfg.state()
    if as_json:
        print(json.dumps(_state_dict(st), indent=2, default=lambda o: o.hex() if isinstance(o, bytes) else str(o)))
        return 0
    why = f" ({st.unreadable})" if st.unreadable else ""
    print(f"storage: {st.storage}{why}, saved hash 0x{st.saved_hash:08x}")
    for s in st.slots:
        print(f"  slot {s.slot}: {s.state}" + (f", connection {s.connection}" if s.connection else "")
              + (f", tried at {s.last_try_at_ns / 1e9:.3f} s" if s.last_try_at_ns is not None else ""))
    for b in st.binds:
        print(f"  port {b.port}: {b.flow}")
    if st.wifi is not None:
        print(f"wifi: {st.wifi.text()}")
    return 0


def _tids(hst) -> dict[int, str]:
    """connection -> its target_id as hex, from every wire's connections (the slot's target is the host's to check,
    probe.config §1.1, debug §2.1); best effort."""
    from . import riscv
    out = {}
    for e in core.list_entries(hst):
        if e.name in ("oep.wire.rvswd", "oep.wire.swio"):
            try:
                for c in riscv.Wire(hst, e.name).connections():
                    if c.target_id:
                        out[c.connection] = c.target_id[1][::-1].hex()
            except host.OepError:
                pass
    return out


def _show(hst, cfg, as_json: bool) -> int:
    h, _ = cfg.get()
    items = cfg.items()
    decl = cfg.describe()
    st = cfg.state()
    kinds = {i: _name(k) for i, k, _ in core.transports(hst)}
    if as_json:
        out = {"hash": h, "items": [dict(type=type(i).__name__, **_plain(i)) if hasattr(i, "__dict__") else _plain(i)
                                     for i in items],
               "declared": _plain(decl), "state": _state_dict(st), "transports": kinds}
        print(json.dumps(out, indent=2, default=lambda o: o.hex() if isinstance(o, bytes) else str(o)))
        return 0
    why = f" ({st.unreadable})" if st.unreadable else ""
    print(f"storage: {st.storage}{why} ({decl.storage_bytes} bytes), saved hash 0x{st.saved_hash:08x}; now 0x{h:08x}")
    print("transports: " + ", ".join(f"{i} {k}" for i, k in kinds.items()))
    by_slot = {s.slot: s for s in st.slots}
    tids = _tids(hst) if any(s.connection for s in st.slots) else {}
    print(f"slots (up to {decl.slots_max}):")
    for it in items:
        if isinstance(it, config.Slot):
            s = by_slot.get(it.slot)
            pins = f"{it.pins[0]}" if it.pins[1] == 0xFFFF else f"{it.pins[0]},{it.pins[1]}"
            retry = f" retry {it.retry_s:g} s" if it.attach == "at-boot" else ""
            retry += (f" max {it.max_speed} Hz" if it.max_speed else "") + (" idle-low" if it.idle_clock == "low" else "")
            live = ""
            if s:
                tried = "never tried" if s.last_try_at_ns is None else f"tried at {s.last_try_at_ns / 1e9:.3f} s"
                tid = f" target_id {tids[s.connection]}" if s.connection in tids else ""
                live = f"  -> {s.state}" + (f" (connection {s.connection})" if s.connection else f" ({tried})") + tid
            print(f"  {it.slot} {it.name}: fn {it.wire_fn} pins {pins} {it.attach}{retry} {it.mechanism}{live}")
    by_port = {b.port: b for b in st.binds}
    print("binds:")
    names = {it.slot: it.name for it in items if isinstance(it, config.Slot)}
    for it in items:
        if isinstance(it, config.Bind):
            b = by_port.get(it.port)
            k, i = it.stream
            stream = f"slot:{names.get(i, i)}" if k == "slot" else f"{k}:{i}"
            live = f"  -> {b.flow}" if b else ""
            print(f"  port {it.port} ({kinds.get(it.port, '?')}): {stream}{live}")
    if config.ITEM["wifi"] in decl.items:
        print(f"wifi (up to {decl.wifi_max}): " + (st.wifi.text() if st.wifi is not None else "no state"))
        for it in items:
            if isinstance(it, config.Wifi):
                s = it.shown()
                use = "  <- in use" if st.wifi is not None and st.wifi.entry == it.index else ""
                print(f"  {s['index']} {s['ssid']!r}: passphrase {s['passphrase']}{use}")
    for it in items:
        if not isinstance(it, (config.Slot, config.Bind, config.Wifi)):
            print(f"  {it}")
    return 0


def _name(kind: int) -> str:
    return {v: k for k, v in core.TRANSPORT_KIND.items()}.get(kind, str(kind))


if __name__ == "__main__":
    sys.exit(main())
