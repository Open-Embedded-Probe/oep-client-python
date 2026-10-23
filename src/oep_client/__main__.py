"""The oep command: what a probe offers (dump) and its settings (settings, core §13).

  oep dump --port /run/board-identify/by-id/<probe>        oep dump --virtual p4-x035 --prefix oep.target --json
  oep settings show <probe>               (every value, the storage state; lock-free)
  oep settings items <probe>              (what each owner declares: names, forms, choices)
  oep settings set <probe> core channel.GPIO23.label=v003.nrst channel.GPIO20.idle=pull-up [--save]
  oep settings unset <probe> core channel.GPIO20.idle     oep settings unset --all <probe> --save   (empties the storage)
  oep settings save <probe>     oep settings state <probe>     oep settings network <probe>
  oep settings export <probe> jig.oep-settings.json       oep settings import <probe> jig.oep-settings.json [--save]
  oep find [--timeout 2] [--json] [--no-verify]   (probes announcing _oep._tcp by DNS-SD over mDNS: unit_id, host,
                                           port, IP; each verified first by confirm + describe's unit_id, host guide
                                           §4.1 - one that fails is dropped with a warning)
  oep speed <probe> [--candidates 921600,500000] [--verify [--flows in:2,out:2]] (port_speed, oep-if-link §3 and the host
                                           guide §17: try the candidates in order on a UART bridge, report; default
                                           500000 - a faster rate only when named, after its 1 s verify each way)
  oep linktest <probe> --rates now,921600 --patterns in,out,duplex --inflight 1,2 --sizes 128,496 --frames 300
  oep pins <probe> --power 5 --wire swio  (find the target's debug pins and reset line: classify the channels, scan,
                                           identify, hold-low + attach under reset; prints the labels, --save writes them)
  oep clock <probe> [-n 8] [--json]       (fn 0's clock, core §7.6: the probe's uptime_ns and boot_id against this
                                           host's time - the reading with the shortest round trip of n)
  oep restart <probe> [--reopen-s S]      (oep.probe.restart: the probe restarts; waits until it is back, its new boot_id)

<probe>: a serial port, tcp://HOST[:PORT] (no port: the one DNS-SD finds), tcp:UNIT_ID (found by DNS-SD) or
usb[:VID:PID[:SERIAL]]. A change takes the lock (owner "oep settings") and
ends the session after it; it takes effect at once, and stays over a restart only after `save` (or --save).
"""

from __future__ import annotations

import argparse
import sys

import json
import os

from . import catalog, core, dump, settings, virtual_bench, host, link

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
    _settings_parser(sub)
    sp = sub.add_parser("speed", help="port_speed (oep-if-link §3; this client's procedure): try faster rates on a UART bridge, "
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
                        "reset line, suggest labels (writes nothing without --save)")
    pn.add_argument("probe", help="a probe: a serial port, tcp://HOST:PORT or usb[:VID:PID[:SERIAL]]")
    pn.add_argument("--power", type=int, help="the gpio channel that powers the target: switched off, then on, to "
                    "see which channels follow it (never touched without this)")
    pn.add_argument("--exclude", default="", help="channels never to read, scan or hold, comma-separated")
    pn.add_argument("--wire", choices=("swio", "rvswd", "swd"), default="swio")
    pn.add_argument("--steps", default="classify,hold,scan,identify,reset,labels",
                    help="the steps to run, comma-separated (default all: classify, hold, scan, identify, reset, labels)")
    pn.add_argument("--save", action="store_true", help="write the suggested labels (settings set, then save)")
    pn.add_argument("--name", help="the host's connection profile name: labels <name>.nrst / <name>.power_hi (a probe "
                    "serving several targets; default: nrst / power_hi)")
    pn.add_argument("--json", action="store_true", help="the report as JSON (the steps' lines go to stderr)")
    ck = sub.add_parser("clock", help="the probe's clock (fn 0 clock, core §7.6) against this host's: lock-free")
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
    if args.command == "settings":
        return _settings(args)
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
                                save=args.save, name=args.name)
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


# ---- oep settings ---------------------------------------------------------------------------------------------------

def _settings_parser(sub) -> None:
    c = sub.add_parser("settings", help="the probe's settings (core §13): show, set / unset by name, save, export / "
                                        "import a settings file, the network")
    cs = c.add_subparsers(dest="action", required=True)
    show = cs.add_parser("show", help="every value (category, name, value) and the storage state (lock-free)")
    show.add_argument("probe")
    show.add_argument("--json", action="store_true")
    items = cs.add_parser("items", help="what the probe declares: each category's items, forms and choices")
    items.add_argument("probe")
    items.add_argument("--json", action="store_true")
    st = cs.add_parser("set", help="values by name in one category: NAME=VALUE ... (core channel.GPIO2.label=v003.nrst)")
    st.add_argument("probe")
    st.add_argument("category", help="core, or an interface's name/instance (oep.fixture.uart/0)")
    st.add_argument("values", nargs="+", metavar="NAME=VALUE")
    st.add_argument("--save", action="store_true")
    un = cs.add_parser("unset", help="remove values by name (the item has none after)")
    un.add_argument("probe")
    un.add_argument("category", nargs="?")
    un.add_argument("names", nargs="*")
    un.add_argument("--all", action="store_true", help="every value the user set (then --save empties the storage)")
    un.add_argument("--save", action="store_true")
    cs.add_parser("save", help="save the values now in effect").add_argument("probe")
    state = cs.add_parser("state", help="the storage state (lock-free)")
    state.add_argument("probe")
    state.add_argument("--json", action="store_true")
    net = cs.add_parser("network", help="the network now: the TCP port, each netif's state and address (lock-free)")
    net.add_argument("probe")
    net.add_argument("--json", action="store_true")
    ex = cs.add_parser("export", help="write every value to a settings file (JSON; a secret as present only)")
    ex.add_argument("probe")
    ex.add_argument("file", help="the file to write, - for stdout")
    im = cs.add_parser("import", help="make the probe's values a settings file's (shows what changes first)")
    im.add_argument("probe")
    im.add_argument("file")
    im.add_argument("--yes", action="store_true", help="apply without asking")
    im.add_argument("--save", action="store_true")
    im.add_argument("--secret-env", action="append", default=[], metavar="CATEGORY:NAME=VAR",
                    help="a secret the file cannot carry, from an environment variable")


def _change(hst, fn, save: bool) -> None:
    core.take(hst, 3000, owner="oep settings")
    try:
        gen = fn()
        if gen is not None:
            print(f"set: generation {gen}")
        if save:
            st = settings.Settings(hst)
            if st.needs_save():
                print(f"saved: generation {st.save()}")
            else:
                print("saved already (saved_generation is the current generation): not written")
    finally:
        hst.end()


def _pairs(texts: list[str]) -> dict:
    out = {}
    for t in texts:
        name, eq, value = t.partition("=")
        if not eq:
            raise SystemExit(f"{t!r}: NAME=VALUE")
        out[name] = value
    return out


def _settings(args) -> int:
    hst = _open_host(args.probe)
    try:
        st = settings.Settings(hst)
        if args.action == "show":
            return _show(st, args.json)
        if args.action == "items":
            return _items(st, args.json)
        if args.action == "state":
            return _state(st, args.json)
        if args.action == "network":
            return _network(st, args.json)
        if args.action == "set":
            values = _pairs(args.values)
            _change(hst, lambda: st.set_named(args.category, values), args.save)
        elif args.action == "unset":
            if args.all:
                _change(hst, st.unset_all, args.save)
            else:
                if not args.category or not args.names:
                    raise SystemExit("unset: CATEGORY NAME ..., or --all")
                _change(hst, lambda: st.unset_named(args.category, args.names), args.save)
        elif args.action == "save":
            _change(hst, lambda: None, True)
        elif args.action == "export":
            doc = st.export()
            text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
            if args.file == "-":
                sys.stdout.write(text)
            else:
                with open(args.file, "w", encoding="utf-8") as f:
                    f.write(text)
                n = sum(len(v) for v in doc["settings"].values())
                print(f"wrote {n} values to {args.file}")
        elif args.action == "import":
            return _import(hst, st, args)
        return 0
    except (LookupError, ValueError) as e:
        print(f"settings: {e}", file=sys.stderr)
        return 2
    except host.Rejected as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        hst.link.close()


def _import(hst, st, args) -> int:
    with open(args.file, encoding="utf-8") as f:
        doc = json.load(f)
    plan = st.plan_import(doc)
    cat = st.catalog()
    secrets = {}
    for spec in args.secret_env:
        where, eq, var = spec.partition("=")
        category, _, name = where.partition(":")
        if not eq or not name:
            raise SystemExit(f"--secret-env {spec!r}: CATEGORY:NAME=VAR")
        secrets[(category, name)] = os.environ[var]
    if plan.other_probe:
        print(f"note: the file is of another probe ({doc.get('probe', {})})")
    for category, flat in plan.unresolved:
        print(f"  not on this probe: {category} {flat}")
    for fn, number, key, data in plan.sets:
        it = cat.item(fn, number)
        print(f"  set   {cat.category[fn]} {cat.flat(it, key)} = {json.dumps(cat.to_json(it, data))}")
    for fn, number, key in plan.unsets:
        it = cat.item(fn, number)
        print(f"  unset {cat.category[fn]} {cat.flat(it, key)}")
    for category, flat in plan.secret_without_value:
        if (category, flat) in secrets:
            it, key = cat.resolve(category, flat)
            plan.sets.append((it.fn, it.number, key, secrets[(category, flat)].encode()))
            print(f"  set   {category} {flat} (secret, from the environment)")
        else:
            print(f"  no value on the probe for secret {category} {flat}: give it with --secret-env")
    if not plan.sets and not plan.unsets:
        print("nothing to change")
        if args.save:
            _change(hst, lambda: None, True)
        return 0
    if not args.yes:
        if input("apply? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not applied")
            return 1
    _change(hst, lambda: st.apply_import(plan), args.save)
    return 0


def _network(st, as_json: bool) -> int:
    """op network (lock-free): the TCP port and each netif's state."""
    net = st.network()
    if as_json:
        print(json.dumps({"tcp_port": net.tcp_port, "netifs": [vars(n) for n in net.netifs]}, indent=2))
        return 0
    print(f"tcp port: {net.tcp_port or 'not listening'}")
    for n in net.netifs:
        where = f" {n.address} gateway {n.gateway} dns {n.dns}" if n.address else ""
        entry = f" entry {n.entry}" if n.entry is not None else ""
        rssi = f" {n.rssi} dBm" if n.state == "up" and n.netif == "wifi" else ""
        why = f" ({n.reason})" if n.reason != "none" else ""
        print(f"  {n.netif}: {n.state}{why}{entry}{rssi}{where}")
    return 0


def _state(st, as_json: bool) -> int:
    """The storage state alone (op state, lock-free)."""
    s = st.state()
    if as_json:
        print(json.dumps(vars(s), indent=2))
        return 0
    why = f" ({s.reason})" if s.reason else ""
    print(f"storage: {s.storage}{why}, saved generation {s.saved_generation}")
    return 0


def _show(st, as_json: bool) -> int:
    gen, _ = st.get()
    rows = st.show()
    state = st.state() if st.offers("state") else None
    if as_json:
        print(json.dumps({"generation": gen, "state": vars(state) if state else None,
                          "values": [dict(category=c, name=n, value=v) for c, n, v in rows]}, indent=2))
        return 0
    if state is not None:
        why = f" ({state.reason})" if state.reason else ""
        print(f"storage: {state.storage}{why}, saved generation {state.saved_generation}; now {gen}")
    else:
        print(f"no storage; generation {gen}")
    for c, n, v in rows:
        shown = "(secret)" if isinstance(v, dict) else json.dumps(v)
        print(f"  {c} {n} = {shown}")
    return 0


def _items(st, as_json: bool) -> int:
    cat = st.catalog()
    rows = [dict(category=cat.category[fn], number=it.number, name=it.name, form=it.form, key=it.key_kind,
                 keys=it.keys, max_len=it.max_len, secret=it.secret, choices=sorted(it.choices.values()))
            for fn in sorted(cat.items) for it in cat.items[fn]]
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        extra = f" {'|'.join(r['choices'])}" if r["choices"] else ""
        print(f"  {r['category']} {r['name']}  {r['form']}{extra}" + ("  secret" if r["secret"] else ""))
    return 0


def _name(kind: int) -> str:
    return {v: k for k, v in core.TRANSPORT_KIND.items()}.get(kind, str(kind))


if __name__ == "__main__":
    sys.exit(main())
