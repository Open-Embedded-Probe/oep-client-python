"""The oep command: what a probe offers (dump) and its settings (config).

  oep dump --port /run/board-identify/by-id/<probe>        oep dump --fake p4-x035 --prefix oep.target --json
  oep config show <probe>                 (the settings and what the probe declares; lock-free)
  oep config state <probe>                (the live slot / bind / storage state; lock-free)
  oep config slot <probe> --name x035 --wire rvswd --pins 2,54 --attach at-boot --retry 1 --mechanism dmseq
  oep config bind <probe> --port 1 --mode last-reset --stream slot:x035
  oep config plan <probe> oep.fixture.uart#1 rx=48 tx=49       (the fn's whole plan; fn number or name#instance)
  oep config uart <probe> oep.fixture.uart#1 115200 --format 8N1
  oep config disable <probe> 3 4           (channels the probe never uses or touches; remove disable CH re-enables)
  oep config remove <probe> bind 1        oep config save <probe>        oep config erase <probe>
  oep speed <probe> [--candidates 921600,500000] [--verify [--flows in:2,out:2]] (port_speed, oep-if-link §3 and the host
                                           guide §17: try the candidates in order on a UART bridge, report)
  oep linktest <probe> --rates now,921600 --patterns in,out,duplex --inflight 1,2 --sizes 128,496 --frames 300
  oep pins <probe> --power 5 --wire swio  (find the target's debug pins and reset line: classify the channels, scan,
                                           identify, hold-low + attach under reset; prints a slot, --save writes it)
  oep clock <probe> [-n 8] [--json]       (fn 0's clock, core §7.7: the probe's uptime_ns and boot_id against this
                                           host's time - the reading with the shortest round trip of n)
  oep restart <probe>                     (oep.probe.restart: the probe restarts; waits until it is back, its new boot_id)

<probe>: a serial port, tcp://HOST:PORT or usb[:VID:PID[:SERIAL]]. A change takes the lock (owner "oep config") and
ends the session after it; it takes effect at once, and stays over a restart only after `save` (or --save).
"""

from __future__ import annotations

import argparse
import sys

import json
import struct

from . import catalog, config, core, dump, fake, host, link

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
    sp.add_argument("--candidates", default="", help="the candidates, comma-separated (default 500000)")
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
    lt.add_argument("--timeout", type=float, default=0.3, help="seconds to wait for one answer (default 0.3)")
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
    d = sub.add_parser("dump", help="list and describe every interface a probe offers")
    src = d.add_mutually_exclusive_group(required=True)
    src.add_argument("--fake", choices=sorted(fake.PROFILES), help="in-process example probe")
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

    confirm = (0, 1)
    if args.fake:
        call = fake.PROFILES[args.fake]().call
    else:
        hst = _open_host(args.port)
        call = lambda fn, op, payload: hst.request(fn, op, payload, locked=False).payload   # noqa: E731
        confirm = hst.confirm_range()                    # confirmed already: the revision in use (core §7.1)
    caps = dump.collect(call, args.prefix, args.exact, confirm)
    sys.stdout.write(dump.to_json(caps) + "\n" if args.json else dump.to_text(caps))
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
    after = hst.restart_probe()
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
    return 0 if report.found else 1


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
        core.take(hst, 5000, owner="oep speed")
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
    slot.add_argument("--lock", help="MASK:VALUE (hex u32) the target_id (WCH DMI 0x7F) must match, e.g. ffffff0f:035e0600")
    slot.add_argument("--boot-reset", action="store_true",
                      help="at-boot: when the automatic attach gets no answer, try once more with the slot's nrst line "
                           "(label <name>.nrst; before any session took the lock this boot)")
    slot.add_argument("--save", action="store_true", help="save after the change")
    bind = cs.add_parser("bind", help="what a serial port carries")
    bind.add_argument("probe")
    bind.add_argument("--port", type=int, required=True, help="the serial port (its transport index, see show)")
    bind.add_argument("--mode", choices=sorted(config.MODE), default="last-reset")
    bind.add_argument("--stream", action="append", required=True, help="slot:NAME, slot:N or uart:FN (repeatable)")
    bind.add_argument("--select", type=int, default=0, help="manual: the stream it carries (index in --stream)")
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
                                                    "less (the levels: the gpio's describe drive_levels)")
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
            print(f"saved: hash 0x{cfg.save():08x}")
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
            if args.boot_reset and args.attach != "at-boot":
                raise SystemExit("--boot-reset goes with --attach at-boot")
            fn = _wire_fn(hst, args.wire)
            args.wire = args.wire or str(fn)
            lock = None
            if args.lock:
                mask, _, value = args.lock.partition(":")
                lock = (1, struct.pack("<I", int(mask, 16)), struct.pack("<I", int(value, 16)))
            it = config.Slot(slot=args.slot, wire_fn=fn, pins=_pins(hst, fn, args.pins, args.wire), name=args.name,
                             attach=args.attach, retry_s=args.retry, mechanism=args.mechanism, lock=lock,
                             max_speed=args.max_speed, idle_clock=args.idle_clock, boot_reset=args.boot_reset)
            _change(hst, cfg, [it], args.save)
        elif args.action == "bind":
            slots = {it.name: it.slot for it in cfg.items() if isinstance(it, config.Slot)}
            it = config.Bind(port=args.port, mode=args.mode, streams=[_stream(s, slots) for s in args.stream],
                             selected=args.select)
            _change(hst, cfg, [it], args.save)
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
            from .fixture import Drive
            drive = (Drive.max_ma(args.drive_ma) if args.drive_ma is not None else
                     Drive.level(args.drive_level) if args.drive_level is not None else None)
            if drive is not None and args.mode not in ("output-low", "output-high"):
                raise SystemExit(f"--drive-ma / --drive-level go with output-low / output-high, not {args.mode}")
            _change(hst, cfg, [config.Idle(channel=args.channel, mode=args.mode, drive=drive)], args.save)
        elif args.action == "disable":
            _change(hst, cfg, [config.Disable(channel=ch) for ch in args.channels], args.save)
        elif args.action == "remove":
            _change(hst, cfg, [config.remove(args.kind, args.key)], args.save)
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


def _plain(o):
    return {k: (v.hex() if isinstance(v, bytes) else v) for k, v in vars(o).items()} if hasattr(o, "__dict__") \
        else list(o)


def _state_dict(st) -> dict:
    return {**{k: v for k, v in vars(st).items() if k not in ("slots", "binds")},
            "slots": [_plain(s) for s in st.slots], "binds": [_plain(b) for b in st.binds]}


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
              + (f", tried at {s.last_try_at_ns / 1e9:.3f} s" if s.last_try_at_ns is not None else "")
              + (f", target_id {s.target_id[::-1].hex()}" if s.target_id else "")
              + (f", reset retried at {s.reset_at_ns / 1e9:.3f} s" if s.reset_at_ns is not None else ""))
    for b in st.binds:
        print(f"  port {b.port}: {b.mode}, {b.flow}" + (f", carrying {b.selected}" if b.selected is not None else ""))
    return 0


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
    print(f"slots (up to {decl.slots_max}):")
    for it in items:
        if isinstance(it, config.Slot):
            s = by_slot.get(it.slot)
            pins = f"{it.pins[0]}" if it.pins[1] == 0xFFFF else f"{it.pins[0]},{it.pins[1]}"
            retry = f" retry {it.retry_s:g} s" if it.attach == "at-boot" else ""
            retry += (f" max {it.max_speed} Hz" if it.max_speed else "") + (" idle-low" if it.idle_clock == "low" else "")
            retry += " boot-reset" if it.boot_reset else ""
            lock = (f" lock {int.from_bytes(it.lock[1], 'little'):08x}:{int.from_bytes(it.lock[2], 'little'):08x}"
                    if it.lock else "")
            live = ""
            if s:
                tried = "never tried" if s.last_try_at_ns is None else f"tried at {s.last_try_at_ns / 1e9:.3f} s"
                tid = f" target_id {s.target_id[::-1].hex()}" if s.target_id else ""
                live = f"  -> {s.state}" + (f" (connection {s.connection})" if s.connection else f" ({tried})") + tid
            print(f"  {it.slot} {it.name}: fn {it.wire_fn} pins {pins} {it.attach}{retry} {it.mechanism}{lock}{live}")
    by_port = {b.port: b for b in st.binds}
    print(f"binds (modes: {', '.join(decl.bind_modes) or '-'}):")
    names = {it.slot: it.name for it in items if isinstance(it, config.Slot)}
    for it in items:
        if isinstance(it, config.Bind):
            b = by_port.get(it.port)
            streams = ", ".join(f"slot:{names.get(i, i)}" if k == "slot" else f"{k}:{i}" for k, i in it.streams)
            sel = f" selected {it.selected}" if it.mode == "manual" else ""
            live = f"  -> {b.flow}" + (f", carrying {b.selected}" if b and b.selected is not None else "") if b else ""
            print(f"  port {it.port} ({kinds.get(it.port, '?')}): {it.mode} [{streams}]{sel}{live}")
    for it in items:
        if not isinstance(it, (config.Slot, config.Bind)):
            print(f"  {it}")
    return 0


def _name(kind: int) -> str:
    return {v: k for k, v in core.TRANSPORT_KIND.items()}.get(kind, str(kind))


if __name__ == "__main__":
    sys.exit(main())
