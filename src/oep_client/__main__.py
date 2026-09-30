"""The oep command: what a probe offers (dump) and its settings (config).

  oep dump --port /run/board-identify/by-id/<probe>        oep dump --fake p4-x035 --prefix oep.target --json
  oep config show <probe>
  oep config slot <probe> --name x035 --wire rvswd --pins 2,54 --attach at-boot --retry 1 --mechanism dmseq
  oep config bind <probe> --port 1 --mode last-reset --stream slot:x035
  oep config plan <probe> oep.fixture.uart#2 rx=48 tx=49       (the fn's whole plan; fn number or name#instance)
  oep config remove <probe> bind 1        oep config save <probe>        oep config erase <probe>

<probe>: a serial port, tcp://HOST:PORT or usb[:VID:PID[:SERIAL]]. A change takes the lock (owner "oep config") and
ends the session after it; it takes effect at once, and stays over a restart only after `save` (or --save).
"""

from __future__ import annotations

import argparse
import sys

import json
import struct

from . import catalog, config, core, dump, fake, host, link


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="oep", description="Open Embedded Probe: what a probe offers, its settings")
    sub = parser.add_subparsers(dest="command", required=True)
    _config_parser(sub)
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

    if args.fake:
        call = fake.PROFILES[args.fake]().call
    else:
        hst = link.open_host(args.port)
        call = lambda fn, op, payload: hst.request(fn, op, payload, locked=False).payload   # noqa: E731
    caps = dump.collect(call, args.prefix, args.exact)
    sys.stdout.write(dump.to_json(caps) + "\n" if args.json else dump.to_text(caps))
    return 0


# ---- oep config ----------------------------------------------------------------------------------------------------

def _config_parser(sub) -> None:
    c = sub.add_parser("config", help="the probe's settings (oep.probe.config): slots, binds, save")
    cs = c.add_subparsers(dest="action", required=True)
    show = cs.add_parser("show", help="the settings and the live slot / bind state")
    show.add_argument("probe")
    show.add_argument("--json", action="store_true")
    slot = cs.add_parser("slot", help="register a slot (a place a target is wired to)")
    slot.add_argument("probe")
    slot.add_argument("--slot", type=int, default=0, help="the slot number (default 0)")
    slot.add_argument("--name", required=True, help="1-32 of a-z 0-9 - _ (the oep://<probe>/<name> address)")
    slot.add_argument("--wire", help="rvswd or swio (or an fn); default: the probe's only wire")
    slot.add_argument("--pins", help="swdio,swclk (one pin on swio); default: the wire's only pin set")
    slot.add_argument("--attach", choices=sorted(config.ATTACH), default="host")
    slot.add_argument("--retry", type=int, default=0, help="at-boot: try again every N s while absent (0: never)")
    slot.add_argument("--mechanism", choices=sorted(config.MECHANISM), default="dmseq")
    slot.add_argument("--max-speed", type=int, default=0,
                      help="the line's ceiling in Hz for the probe's own attach (0: none); the target's, e.g. 1000000")
    slot.add_argument("--idle-clock", choices=sorted(config.IDLE_CLOCK), default="high",
                      help="rvswd: SWCLK while the line rests (the target's: low on CH32L103 / V203)")
    slot.add_argument("--lock", help="MASK:VALUE (hex u32) the target_id (WCH DMI 0x7F) must match, e.g. ffffff0f:035e0600")
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
    plan.add_argument("fn", help="an fn, or an interface name with #k for its k-th instance (1 = the first)")
    plan.add_argument("roles", nargs="+", help="ROLE=CHANNEL, ROLE a number or the interface's role name (rx, tx, line...)")
    plan.add_argument("--save", action="store_true")
    label = cs.add_parser("label", help="name a channel (shown in oep.core's describe)")
    label.add_argument("probe")
    label.add_argument("channel", type=int)
    label.add_argument("text")
    label.add_argument("--save", action="store_true")
    idle = cs.add_parser("idle", help="the state of a free pin")
    idle.add_argument("probe")
    idle.add_argument("channel", type=int)
    idle.add_argument("mode", choices=sorted(config.IDLE))
    idle.add_argument("--save", action="store_true")
    rm = cs.add_parser("remove", help="remove one item: slot N, bind PORT, plan FN, label CH, idle CH")
    rm.add_argument("probe")
    rm.add_argument("kind", choices=["slot", "bind", "plan", "label", "idle"])
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
            roles = {v[1 + 3 * i]: struct.unpack_from("<H", v, 2 + 3 * i)[0] for i in range((len(v) - 1) // 3)}
            groups.append((roles.get(1, 0xFFFF), roles.get(2, 0xFFFF)))
    if len(groups) != 1:
        raise SystemExit(f"{wire}: {len(groups)} pin sets on this probe - name one with --pins")
    return groups[0]


def _plan_fn(hst, spec: str) -> tuple[int, dict[str, int]]:
    """fn and its role names (from the registry) for `spec`: an fn number, or name[#k] (k-th instance, 1-based)."""
    from . import registry as reg
    if spec.isdigit():
        fn = int(spec)
        name = next((e.name for e in core.list_entries(hst) if e.fn == fn), "")
    else:
        name, _, k = spec.partition("#")
        fns = core.find_all(hst, name)
        if not fns:
            raise SystemExit(f"the probe offers no {name}")
        index = int(k) - 1 if k else 0
        if not 0 <= index < len(fns):
            raise SystemExit(f"{name}: {len(fns)} instance(s), fns {fns}")
        fn = fns[index]
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
    hst = link.open_host(args.probe)
    try:
        cfg = config.ProbeConfig(hst)
        if args.action == "show":
            return _show(hst, cfg, args.json)
        if args.action == "slot":
            fn = _wire_fn(hst, args.wire)
            args.wire = args.wire or str(fn)
            lock = None
            if args.lock:
                mask, _, value = args.lock.partition(":")
                lock = (1, struct.pack("<I", int(mask, 16)), struct.pack("<I", int(value, 16)))
            it = config.Slot(slot=args.slot, wire_fn=fn, pins=_pins(hst, fn, args.pins, args.wire), name=args.name,
                             attach=args.attach, retry_s=args.retry, mechanism=args.mechanism, lock=lock,
                             max_speed=args.max_speed, idle_clock=args.idle_clock)
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
        elif args.action == "label":
            _change(hst, cfg, [config.Label(channel=args.channel, text=args.text)], args.save)
        elif args.action == "idle":
            _change(hst, cfg, [config.Idle(channel=args.channel, mode=args.mode)], args.save)
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


def _show(hst, cfg, as_json: bool) -> int:
    h, _ = cfg.get()
    items = cfg.items()
    st = cfg.state()
    kinds = {i: _name(k) for i, k, _ in core.transports(hst)}
    if as_json:
        def plain(o):
            return {k: (v.hex() if isinstance(v, bytes) else v) for k, v in vars(o).items()} if hasattr(o, "__dict__") \
                else list(o)
        out = {"hash": h, "items": [dict(type=type(i).__name__, **plain(i)) if hasattr(i, "__dict__") else plain(i)
                                     for i in items],
               "state": {**{k: v for k, v in vars(st).items() if k not in ("slots", "binds")},
                         "slots": [plain(s) for s in st.slots], "binds": [plain(b) for b in st.binds]},
               "transports": kinds}
        print(json.dumps(out, indent=2, default=lambda o: o.hex() if isinstance(o, bytes) else str(o)))
        return 0
    print(f"storage: {st.storage} ({st.storage_bytes} bytes), saved hash 0x{st.saved_hash:08x}; now 0x{h:08x}")
    print("transports: " + ", ".join(f"{i} {k}" for i, k in kinds.items()))
    by_slot = {s.slot: s for s in st.slots}
    print(f"slots (up to {st.slots_max}):")
    for it in items:
        if isinstance(it, config.Slot):
            s = by_slot.get(it.slot)
            pins = f"{it.pins[0]}" if it.pins[1] == 0xFFFF else f"{it.pins[0]},{it.pins[1]}"
            retry = f" retry {it.retry_s} s" if it.attach == "at-boot" else ""
            retry += (f" max {it.max_speed} Hz" if it.max_speed else "") + (" idle-low" if it.idle_clock == "low" else "")
            lock = (f" lock {int.from_bytes(it.lock[1], 'little'):08x}:{int.from_bytes(it.lock[2], 'little'):08x}"
                    if it.lock else "")
            live = ""
            if s:
                tried = "never tried" if s.last_try_ms is None else f"tried {s.last_try_ms} ms ago"
                tid = f" target_id {s.target_id[::-1].hex()}" if s.target_id else ""
                live = f"  -> {s.state}" + (f" (connection {s.connection})" if s.connection else f" ({tried})") + tid
            print(f"  {it.slot} {it.name}: fn {it.wire_fn} pins {pins} {it.attach}{retry} {it.mechanism}{lock}{live}")
    by_port = {b.port: b for b in st.binds}
    print(f"binds (modes: {', '.join(st.bind_modes) or '-'}):")
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
