"""Selected extension-common contracts using the current draft core context."""
import copy
import hashlib
from pathlib import Path

from .conformance import require
from .conformance_interfaces import InterfaceChecks
from . import conformance_interfaces as _declarations


class InterfaceSmokeChecks:
    def __init__(self, core):
        self.c = core
        self.declarations = InterfaceChecks(core)
        self.results = []

    def prepare(self):
        self.c.confirm(); self.c.discovery()
        self.entries = self.c.observed['interfaces']
        for entry in self.entries:
            rows = self.c.describe(entry['fn'])
            self.declarations.snapshots[entry['fn']] = rows
            entry['describe'] = [{'tag': t, 'value_hex': value.hex()} for t, value in rows]

    def absent(self, entry):
        offered = self.declarations.declared_ops(entry)
        opcode = next((op for op in range(16, 256) if op not in offered), 3)
        before = copy.deepcopy(self.c.observe()) if self.c.observe else None
        reply = self.c.call(opcode, fn=entry['fn'])
        self.c.rejected(reply, 'unknown_operation')
        observation = {'fn': entry['fn'], 'op': opcode, 'effects_observed': self.c.observe is not None}
        self.c.observed.setdefault('unoffered_operations', []).append(observation)
        if self.c.observe:
            after = copy.deepcopy(self.c.observe())
            observation.update(effects_before=before, effects_after=after)
            require(after == before, 'unoffered operation changed observed effects')

    def stable(self, entry):
        require(self.c.describe(entry['fn']) == self.declarations.snapshots[entry['fn']], 'declaration changed within selected boot')
        self.c.clock()  # Same boot, without requiring a fine-resolution clock.

    def instances(self, entry):
        peers = sorted((row for row in self.entries if (row['name'], row['revision']) ==
                        (entry['name'], entry['revision'])), key=lambda row: row['fn'])
        require(entry['instance'] == peers.index(entry), 'instance order within name/revision')

    def check(self, case, function):
        row = {'id': case, 'level': 'interface'}
        try:
            function(); row['status'] = 'passed'
        except Exception as exc:
            row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        self.results.append(row)
        return row['status'] == 'passed'

    def run(self):
        prepared = self.check('IF-SMOKE-CONTEXT', self.prepare)
        if prepared:
            for entry in self.entries:
                for case, method in (('NAME', self.declarations.name), ('OPS', self.declarations.declared_ops),
                    ('FIXED', self.declarations.fixed), ('CHANNELS', self.declarations.channels),
                    ('INSTANCE', self.instances), ('STABLE', self.stable), ('ABSENT', self.absent)):
                    if not self.check('IF-SMOKE-' + case + '-' + str(entry['fn']), lambda: method(entry)):
                        break  # Do not send further requests with a failed prerequisite.
                if self.results[-1]['status'] == 'failed':
                    break
        return {'status': 'passed' if all(row['status'] == 'passed' for row in self.results) else 'failed',
            'full_conformance': False, 'scope': 'selected generic extension declarations and one unoffered op per fn',
            'levels': {'core': 'discovery/clock prerequisites', 'interface': 'selected shared contracts', 'oep-interface': 'not executed'},
            'spec': self.c.spec, 'checks': self.results, 'observed': self.c.observed, 'exchanges': self.c.trace,
            'checker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'declaration_checker_sha256': hashlib.sha256(Path(_declarations.__file__).read_bytes()).hexdigest(),
            'core_checker_sha256': hashlib.sha256(Path(__file__).with_name('conformance_core_smoke.py').read_bytes()).hexdigest(),
            'helper_sha256': hashlib.sha256(Path(__file__).with_name('conformance.py').read_bytes()).hexdigest(),
            'not_applicable': ['per-interface checks: no named functions'] if prepared and not self.entries else [],
            'unchecked': ['provided operation behavior and standard OEP required operations', 'resources and lifetime',
                          'all absent operations', 'full paging/state stability and other routes', 'physical pins and signals'] +
                         ([] if self.c.observe else ['rejection side effects not independently observed'])}
