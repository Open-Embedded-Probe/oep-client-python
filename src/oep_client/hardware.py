"""Explicit equipment planning, virtual smoke, and guarded physical preflight."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


class ConfigurationError(ValueError):
    """Invalid explicit equipment input; never converted to a skipped test."""


def fail(where, message):
    raise ConfigurationError(f"{where}: {message}")


def table(value, where, required, optional=()):
    if not isinstance(value, dict):
        fail(where, "expected a table")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        fail(where, f"missing keys {sorted(missing)}; unknown keys {sorted(unknown)}")


def text(value, where):
    if not isinstance(value, str) or not value.strip():
        fail(where, "expected nonempty text")


def integer(value, where, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        fail(where, "integer outside permitted range")


def strings(value, where, nonempty=True):
    if not isinstance(value, list) or (nonempty and not value):
        fail(where, "expected a list of strings")
    for item in value:
        text(item, where)
    if len(set(value)) != len(value):
        fail(where, "duplicate values")


def choice(value, where, choices):
    if not isinstance(value, str) or value not in choices:
        fail(where, f"expected one of {sorted(choices)}")


@dataclass(frozen=True)
class Equipment:
    path: Path
    sha256: str
    data: dict

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return (self.path.parent / path).resolve() if not path.is_absolute() else path


def load(path: str | Path) -> Equipment:
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode('utf-8'))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"{path}: {exc}") from exc
    validate(data)
    return Equipment(path, hashlib.sha256(raw).hexdigest(), data)


def validate(d):
    arrays = ('probes', 'targets', 'power_domains', 'discovery_requests',
              'debug_links', 'signal_nets', 'usb_links', 'evidence')
    table(d, 'configuration', ('schema_version', 'kind', 'example', 'configuration_id', 'generation'),
          (*arrays, 'confirmed_at'))
    if type(d['schema_version']) is not int or d['schema_version'] != 1:
        fail('schema_version', 'unsupported schema version')
    choice(d['kind'], 'kind', {'input', 'resolved'})
    if type(d['example']) is not bool:
        fail('example', 'expected boolean')
    text(d['configuration_id'], 'configuration_id')
    integer(d['generation'], 'generation', 1)
    if d['kind'] == 'resolved':
        try:
            stamp = datetime.fromisoformat(d.get('confirmed_at', '').replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('timezone required')
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConfigurationError('confirmed_at: timezone-aware timestamp required') from exc
    indexes = {}
    for name in arrays:
        rows = d.get(name, [])
        if not isinstance(rows, list):
            fail(name, 'expected array of tables')
        indexes[name] = {}
        for row in rows:
            if not isinstance(row, dict) or 'id' not in row:
                fail(name, 'each table requires id')
            text(row['id'], name + '.id')
            if row['id'] in indexes[name]:
                fail(name, 'duplicate id ' + row['id'])
            indexes[name][row['id']] = row
    if not indexes['probes']:
        fail('probes', 'at least one probe required')
    probes, targets = indexes['probes'], indexes['targets']
    if probes.keys() & targets.keys():
        fail('nodes', 'probe and target ids must be distinct')
    nodes = probes | targets

    def ref(value, index, where):
        text(value, where)
        if value not in index:
            fail(where, 'unknown reference ' + value)

    def refs(values, index, where):
        strings(values, where)
        for value in values:
            ref(value, index, where)

    identities = set()
    for node in nodes.values():
        where = node['id']
        is_probe = where in probes
        table(node, where, ('id', 'identity', 'transport') if is_probe else ('id', 'identity', 'chip', 'board'))
        identity = node['identity']
        table(identity, where + '.identity', ('method', 'value'), ('reason',))
        choice(identity['method'], where, {'oep-unit-id', 'manual-label'} if is_probe else {'chip-uid', 'manual-label'})
        text(identity['value'], where + '.identity.value')
        if identity['method'] == 'manual-label':
            text(identity.get('reason'), where + '.identity.reason')
        key = (identity['method'], identity['value'])
        if key in identities:
            fail(where, 'duplicate physical identity')
        identities.add(key)
        if not is_probe:
            text(node['chip'], where + '.chip')
            text(node['board'], where + '.board')
            continue
        transport = node['transport']
        if not isinstance(transport, dict):
            fail(where, 'transport must be a table')
        kind = transport.get('kind')
        choice(kind, where, {'serial', 'tcp', 'usb', 'virtual'})
        fields = {'serial': ('path',), 'tcp': ('host', 'port'), 'usb': ('unit_id',),
                  'virtual': ('profile',)}[kind]
        table(transport, where + '.transport', ('kind', *fields))
        for field in fields:
            if field == 'port':
                integer(transport[field], where + '.port', 1, 65535)
            else:
                text(transport[field], where + '.' + field)
        if kind == 'virtual':
            from .virtual_bench import PROFILES
            choice(transport['profile'], where + '.profile', PROFILES)
        if kind == 'usb':
            import re
            if not re.fullmatch(r'[0-9a-fA-F]{12,32}', transport['unit_id']):
                fail(where, 'USB transport requires a complete hexadecimal unit_id')
            if identity['method'] != 'oep-unit-id' or identity['value'].lower() != transport['unit_id'].lower():
                fail(where, 'USB selector must match configured OEP identity')

    for row in indexes['evidence'].values():
        table(row, row['id'], ('id', 'kind', 'description'), ('tool', 'tool_revision', 'artifact'))
        choice(row['kind'], row['id'], {'manual', 'observation'})
        text(row['description'], row['id'])
        if row['kind'] == 'observation':
            for field in ('tool', 'tool_revision', 'artifact'):
                text(row.get(field), row['id'] + '.' + field)
    for row in indexes['power_domains'].values():
        table(row, row['id'], ('id', 'members', 'control', 'signal_voltage_v', 'ground', 'evidence'))
        refs(row['members'], nodes, row['id'])
        choice(row['control'], row['id'], {'manual'})
        voltage = row['signal_voltage_v']
        if type(voltage) not in (int, float) or not 0 < voltage <= 100:
            fail(row['id'], 'invalid signal voltage')
        text(row['ground'], row['id'])
        refs(row['evidence'], indexes['evidence'], row['id'])
    for kind in ('debug_links', 'signal_nets', 'usb_links'):
        for row in indexes[kind].values():
            where = row['id']
            choice(row.get('state'), where, {'confirmed'} if d['kind'] == 'resolved' else {'candidate'})
            refs(row.get('evidence'), indexes['evidence'], where)
            common = ('id', 'state', 'evidence')
            if kind == 'usb_links':
                table(row, where, (*common, 'host', 'device', 'vbus_source', 'vbus_voltage_v', 'ground_confirmed'))
                for role in ('host', 'device'):
                    table(row[role], where + '.' + role, ('target', 'connector'))
                    ref(row[role]['target'], targets, where)
                    text(row[role]['connector'], where)
                if row['host']['target'] == row['device']['target']:
                    fail(where, 'USB endpoints must use different targets')
                if row['vbus_source'] != row['host']['target'] or row['ground_confirmed'] is not True:
                    fail(where, 'explicit host VBUS and confirmed ground required')
                if type(row['vbus_voltage_v']) not in (int, float) or not 0 < row['vbus_voltage_v'] <= 100:
                    fail(where, 'invalid VBUS voltage')
                continue
            ref(row.get('power_domain'), indexes['power_domains'], where)
            members = indexes['power_domains'][row['power_domain']]['members']
            if kind == 'debug_links':
                table(row, where, (*common, 'probe', 'target', 'protocol', 'power_domain', 'channels', 'target_pads'))
                ref(row['probe'], probes, where)
                ref(row['target'], targets, where)
                if row['target'] not in members:
                    fail(where, 'power domain must contain target')
                choice(row['protocol'], where, {'swio', 'rvswd', 'swd'})
                required = ('data',) if row['protocol'] == 'swio' else ('data', 'clock')
                for field in ('channels', 'target_pads'):
                    table(row[field], where + '.' + field, required, ('reset',))
                if row['channels'].keys() != row['target_pads'].keys():
                    fail(where, 'channel and pad roles differ')
                for value in row['channels'].values():
                    integer(value, where + '.channel', 0, 65535)
                for value in row['target_pads'].values():
                    text(value, where + '.pad')
                if len(set(row['channels'].values())) != len(row['channels']):
                    fail(where, 'debug roles reuse the same channel')
            else:
                table(row, where, (*common, 'signal', 'power_domain', 'endpoints'))
                choice(row['signal'], where, {'digital'})
                if not isinstance(row['endpoints'], list) or len(row['endpoints']) < 2:
                    fail(where, 'at least two endpoints required')
                endpoint_keys = set()
                for ep in row['endpoints']:
                    table(ep, where, ('node', 'mode'), ('pad', 'channel'))
                    ref(ep['node'], nodes, where)
                    choice(ep['mode'], where, {'input-only', 'output-only', 'bidirectional', 'open-drain'})
                    expected = 'channel' if ep['node'] in probes else 'pad'
                    if expected not in ep or ('pad' in ep and 'channel' in ep):
                        fail(where, 'endpoint must use probe channel or target pad')
                    if expected == 'channel':
                        integer(ep[expected], where, 0, 65535)
                    else:
                        text(ep[expected], where)
                    key = (ep['node'], ep[expected])
                    if key in endpoint_keys:
                        fail(where, 'duplicate endpoint')
                    endpoint_keys.add(key)
    if d['kind'] == 'resolved' and indexes['discovery_requests']:
        fail('discovery_requests', 'not allowed in resolved input')
    for row in indexes['discovery_requests'].values():
        table(row, row['id'], ('id', 'kind', 'probe', 'target', 'protocol', 'power_domain',
                              'allowed_channels', 'excluded_channels', 'allowed_target_pads', 'allow_target_flash'))
        choice(row['kind'], row['id'], {'debug'})
        ref(row['probe'], probes, row['id'])
        ref(row['target'], targets, row['id'])
        ref(row['power_domain'], indexes['power_domains'], row['id'])
        choice(row['protocol'], row['id'], {'swio', 'rvswd', 'swd'})
        for field in ('allowed_channels', 'excluded_channels'):
            values = row[field]
            if not isinstance(values, list) or (field == 'allowed_channels' and not values):
                fail(row['id'], 'invalid channel list')
            for value in values:
                integer(value, row['id'], 0, 65535)
            if len(set(values)) != len(values):
                fail(row['id'], 'duplicate channel')
        if set(row['allowed_channels']) & set(row['excluded_channels']):
            fail(row['id'], 'allowed and excluded channels overlap')
        strings(row['allowed_target_pads'], row['id'])
        if type(row['allow_target_flash']) is not bool:
            fail(row['id'], 'allow_target_flash must be boolean')


CONTRACTS = ('PROBE-DECLARE', 'TOOL-FLASH', 'USB-DATA')


def plan(equipment: Equipment, contract='PROBE-DECLARE', probes=(), targets=(), links=()):
    """Structural assignments, not observed capabilities or an execution approval."""
    d = equipment.data
    if d['kind'] != 'resolved':
        fail('kind', 'plan requires resolved configuration')
    choice(contract, 'contract', CONTRACTS)
    probe_ids = {p['id'] for p in d['probes']}
    target_ids = {t['id'] for t in d.get('targets', [])}
    debug = d.get('debug_links', [])
    usb = d.get('usb_links', [])
    link_ids = [x['id'] for x in (*debug, *usb)]
    if len(set(link_ids)) != len(link_ids):
        fail('links', 'ambiguous ids across link kinds')
    for selection, available, name in ((probes, probe_ids, 'probe'), (targets, target_ids, 'target'), (links, link_ids, 'link')):
        if set(selection) - set(available):
            fail(name, 'unknown selector ' + ','.join(sorted(set(selection) - set(available))))
    if contract == 'PROBE-DECLARE' and (targets or links):
        fail('selectors', 'PROBE-DECLARE accepts probe selectors only')
    chosen_debug = [x for x in debug if not probes or x['probe'] in probes]
    assignments = []

    def add(roles, paths):
        resources = {f'probe:{x["probe"]}' for x in paths} | {f'target:{x["target"]}' for x in paths}
        touched = {x['probe'] for x in paths} | {x['target'] for x in paths}
        for power in d.get('power_domains', []):
            if touched & set(power['members']):
                resources.add('power:' + power['id'])
                resources.update('node:' + member for member in power['members'])
        connections = {}
        for path in paths:
            connections[path['probe']] = connections.get(path['probe'], 0) + 1
        assignments.append({'roles': roles, 'control_links': [x['id'] for x in paths],
                            'resources': sorted(resources), 'required_connections': connections})

    if contract == 'PROBE-DECLARE':
        for probe_id in sorted(probe_ids):
            if not probes or probe_id in probes:
                assignments.append({'roles': {'probe': probe_id}, 'control_links': [],
                                    'resources': ['probe:' + probe_id], 'required_connections': {}})
    elif contract == 'TOOL-FLASH':
        for path in sorted(chosen_debug, key=lambda x: x['id']):
            if (not targets or path['target'] in targets) and (not links or path['id'] in links):
                add({'dut': path['target']}, [path])
    else:
        for pair in sorted(usb, key=lambda x: x['id']):
            if links and pair['id'] not in links:
                continue
            a, b = pair['host']['target'], pair['device']['target']
            if targets and not set(targets) <= {a, b}:
                continue
            for ca in chosen_debug:
                for cb in chosen_debug:
                    if ca['target'] != a or cb['target'] != b:
                        continue
                    # Two simultaneous debug links may not reuse probe channels.
                    if ca['probe'] == cb['probe'] and set(ca['channels'].values()) & set(cb['channels'].values()):
                        continue
                    add({'dut': a, 'peer': b, 'usb': pair['id']}, [ca, cb])
    if (probes or targets or links) and not assignments:
        fail('selectors', 'explicit selection has no matching connected assignment')
    return {'configuration_path': str(equipment.path), 'configuration_sha256': equipment.sha256,
            'configuration_id': d['configuration_id'], 'generation': d['generation'], 'example': d['example'],
            'contract': contract, 'status': 'planned' if assignments else 'equipment-unavailable',
            'capabilities': 'not checked; preflight required', 'assignments': assignments}


def virtual_smoke(equipment: Equipment, probes=()):
    """Exercise actual OEP bytes in memory; refuse any physical transport before requests."""
    from . import __version__, cobs, core, dump, endpoint, host, registry, virtual_bench
    selected = plan(equipment, probes=probes)
    ids = [x['roles']['probe'] for x in selected['assignments']]
    nodes = {x['id']: x for x in equipment.data['probes']}
    if any(nodes[name]['transport']['kind'] != 'virtual' for name in ids):
        fail('smoke', 'only explicit virtual transports are allowed')
    report = {**selected, 'capabilities': 'observed during virtual smoke; no physical preflight',
              'scope': 'virtual confirm/declare/identity/session smoke; not full conformance',
              'started_at': datetime.now(timezone.utc).isoformat(),
              'client_version': __version__, 'virtual_bench_version': __version__,
              'registry_hash': registry.REGISTRY_HASH, 'probes': [], 'status': 'passed'}
    for name in ids:
        node = nodes[name]
        profile = node['transport']['profile']
        ep = endpoint.Endpoint(virtual_bench.PROFILES[profile](), lambda: int(time.monotonic() * 1000))

        def exchange(data):
            wire = cobs.frame(data)
            response = ep.handle(cobs.unframe(wire[1:-1]))
            if response is None:
                raise RuntimeError('virtual probe did not answer')
            return cobs.unframe(cobs.frame(response)[1:-1])

        hst = host.Host(exchange)
        result = {'probe': name, 'profile': profile, 'status': 'passed'}
        try:
            limits = hst.confirm()
            result['confirm'] = {k: v for k, v in limits.items() if k != 'tail'}
            result['confirm']['magic'] = limits['magic'].decode('ascii')
            capabilities = dump.collect(lambda fn, op, payload: hst.call(fn, op, payload, locked=False).payload,
                                        confirm=hst.confirm_range())
            result['declarations'] = json.loads(dump.to_json(capabilities))
            values = dict(core.describe(hst))
            result['firmware'] = values.get(virtual_bench.CORE_FIRMWARE, b'').decode('utf-8') or None
            result['unit_id'] = values.get(virtual_bench.CORE_UNIT_ID, b'').decode('utf-8') or None
            if result['unit_id'] != node['identity']['value']:
                raise RuntimeError('probe identity does not match explicit configuration')
            if capabilities.missing:
                raise RuntimeError('missing declarations: ' + ', '.join(capabilities.missing))
            opened = hst.open(owner='hardware virtual smoke')
            if opened.boot_id != limits['boot_id']:
                raise RuntimeError('boot changed between confirm and open')
        except Exception as exc:
            result.update(status='failed', error=str(exc))
            report['status'] = 'failed'
        finally:
            try:
                if hst.session is not None:
                    hst.end()
                result['session_released'] = hst.session is None
            except Exception as exc:
                result['cleanup_error'] = str(exc)
                result['session_released'] = hst.session is None
                result['status'] = report['status'] = 'failed'
        report['probes'].append(result)
    return report


@contextmanager
def equipment_lock(path):
    """Use an existing host-wide lock; never replace its inode or force a holder."""
    if not path:
        fail('lock', 'explicit shared lock path required')
    try:
        import fcntl
    except ImportError as exc:
        raise ConfigurationError('lock: this preflight requires POSIX flock') from exc
    with Path(path).open('r+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigurationError('lock: equipment is in use') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def physical_preflight(equipment: Equipment, probes=(), *, lock_path=None):
    """Confirm identity/declarations and open/end a session, without target operations."""
    from . import __version__, core, dump, link, registry
    selected = plan(equipment, probes=probes)
    nodes = {x['id']: x for x in equipment.data['probes']}
    ids = [x['roles']['probe'] for x in selected['assignments']]
    if equipment.data['example']:
        fail('preflight', 'example configuration cannot operate physical equipment')
    for name in ids:
        node = nodes[name]
        if node['transport']['kind'] == 'virtual':
            fail('preflight', 'physical transport required; use smoke for virtual probes')
        if node['identity']['method'] != 'oep-unit-id':
            fail('preflight', 'observed OEP unit identity required')
        # A serial field must not smuggle in USB auto-discovery or a TCP selector.
        if node['transport']['kind'] == 'serial' and ':' in node['transport']['path']:
            fail('preflight', 'serial path cannot contain a transport selector')
    report = {**selected, 'status': 'passed', 'client_version': __version__,
              'registry_hash': registry.REGISTRY_HASH,
              'scope': 'physical identity/declarations/session; no target or settings writes',
              'full_conformance': False,
              'unchecked': ['replay identity/history', 'corr boundaries', 'lease expiry',
                            'transport faults', 'resource lifetime', 'interface behavior'],
              'capabilities': 'observed probe declarations; target contracts not checked',
              'started_at': datetime.now(timezone.utc).isoformat(), 'probes': []}
    with equipment_lock(lock_path):
        for name in ids:
            node = nodes[name]
            transport = node['transport']
            kind = transport['kind']
            if kind == 'serial':
                address = str(equipment.resolve_path(transport['path']))
            elif kind == 'usb':
                address = 'usb:' + transport['unit_id']
            else:
                hostname = transport['host']
                hostname = '[' + hostname + ']' if ':' in hostname else hostname
                address = f'tcp://{hostname}:{transport["port"]}'
            result = {'probe': name, 'transport': dict(transport), 'status': 'passed'}
            hst = None
            try:
                # Do not recover/end sessions remembered by another runner.
                hst = link.open_host(address, keep_session=False)
                limits = hst.limits
                result['confirm'] = {k: v for k, v in limits.items() if k != 'tail'}
                result['confirm']['magic'] = limits['magic'].decode('ascii')
                values = dict(core.describe(hst))
                tags = registry.CORE.tlv['describe']
                for field in ('firmware', 'model', 'unit_id', 'chip'):
                    result[field] = values.get(tags[field], b'').decode('utf-8') or None
                if not result['unit_id'] or result['unit_id'].lower() != node['identity']['value'].lower():
                    raise RuntimeError('probe identity does not match explicit configuration')
                caps = dump.collect(lambda fn, op, payload: hst.call(fn, op, payload, locked=False).payload,
                                    confirm=hst.confirm_range())
                result['declarations'] = json.loads(dump.to_json(caps))
                if caps.missing:
                    raise RuntimeError('missing declarations: ' + ', '.join(caps.missing))
                opened = hst.open(lease_ms=3000, owner='hardware preflight')
                if opened.boot_id != limits['boot_id']:
                    raise RuntimeError('boot changed between confirm and open')
            except Exception as exc:
                result.update(status='failed', error=str(exc))
                report['status'] = 'failed'
            finally:
                if hst is not None:
                    try:
                        if hst.session is not None:
                            hst.end()
                        result['session_released'] = hst.session is None
                    except Exception as exc:
                        result.update(cleanup_error=str(exc), session_released=False, status='failed')
                        report['status'] = 'failed'
                    finally:
                        try:
                            hst.link.close()
                            result['transport_closed'] = True
                        except Exception as exc:
                            result.update(close_error=str(exc), transport_closed=False, status='failed')
                            report['status'] = 'failed'
                else:
                    result['session_released'] = True  # no session was acquired by this runner
            report['probes'].append(result)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog='oep-hardware', description=__doc__)
    parser.add_argument('command', choices=('validate', 'plan', 'smoke', 'preflight'))
    parser.add_argument('--config', help='explicit TOML; otherwise OEP_HW_CONFIG (OEP_HW_INPUT for validate)')
    parser.add_argument('--contract', choices=CONTRACTS, default='PROBE-DECLARE')
    parser.add_argument('--probe', action='append', default=[])
    parser.add_argument('--target', action='append', default=[])
    parser.add_argument('--link', action='append', default=[])
    parser.add_argument('--out', help='new JSON artifact; otherwise stdout')
    parser.add_argument('--lock', help='existing shared lock; otherwise OEP_HW_LOCK (preflight only)')
    args = parser.parse_args(argv)
    try:
        path = args.config or os.environ.get('OEP_HW_CONFIG')
        if not path and args.command == 'validate':
            path = os.environ.get('OEP_HW_INPUT')
        if not path:
            fail('config', 'explicit path required')
        equipment = load(path)
        if args.lock and args.command != 'preflight':
            fail('lock', 'applies only to physical preflight')
        if args.command in ('smoke', 'preflight') and (args.target or args.link or args.contract != 'PROBE-DECLARE'):
            fail(args.command, 'only PROBE-DECLARE and probe selection supported')
        output_path = args.out
        if not output_path and args.command in ('smoke', 'preflight') and os.environ.get('OEP_HW_RESULTS'):
            root = Path(os.environ['OEP_HW_RESULTS'])
            root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            output_path = str(root / f'{args.command}-{stamp}-{uuid.uuid4().hex[:8]}.json')
        if args.command == 'preflight' and not output_path:
            fail('results', 'preflight requires --out or OEP_HW_RESULTS')
        # Reserve evidence before any transport is opened. Existing results are never overwritten.
        with (Path(output_path).open('x', encoding='utf-8') if output_path else _stdout()) as output:
            try:
                result = _execute(args, equipment)
            except (ConfigurationError, OSError) as exc:
                if output_path:
                    output.write(json.dumps({'status': 'error', 'error': str(exc),
                                             'configuration_sha256': equipment.sha256}) + '\n')
                raise
            output.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        if output_path:
            print(output_path)
        return 1 if result['status'] == 'failed' else 0
    except (ConfigurationError, OSError) as exc:
        print(f'oep-hardware: {exc}', file=sys.stderr)
        return 2


@contextmanager
def _stdout():
    yield sys.stdout


def _execute(args, equipment):
    if args.command == 'validate':
        if args.probe or args.target or args.link or args.contract != 'PROBE-DECLARE':
            fail('validate', 'selection options apply only to plan')
        result = {'status': 'valid', 'configuration_path': str(equipment.path),
                  'configuration_sha256': equipment.sha256, 'kind': equipment.data['kind']}
    elif args.command == 'plan':
        result = plan(equipment, args.contract, args.probe, args.target, args.link)
    elif args.command == 'smoke':
        result = virtual_smoke(equipment, args.probe)
    else:
        result = physical_preflight(equipment, args.probe, lock_path=args.lock or os.environ.get('OEP_HW_LOCK'))
    return result


if __name__ == '__main__':
    raise SystemExit(main())
