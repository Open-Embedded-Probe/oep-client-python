#!/bin/sh
# Snapshot a selected SPEC commit. Explicit path required; never compare runtime tests to sibling HEAD.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
: "${OEP_SPEC_DIR:?set OEP_SPEC_DIR to the intended SPEC checkout}"
python3 - "$here" "$OEP_SPEC_DIR" "${OEP_SPEC_REF:-HEAD}" <<'PY'
import hashlib
from pathlib import Path
import subprocess
import sys

root, spec, ref = sys.argv[1:]
root = Path(root)
commit = subprocess.check_output(['git', '-C', spec, 'rev-parse', ref + '^{commit}'], text=True).strip()
paths = subprocess.check_output(['git', '-C', spec, 'ls-tree', '--name-only', commit + ':tests/vectors'], text=True).splitlines()
inputs = {'src/oep_client/registry.py': 'generated/oep-v1/oep_v1_registry.py'}
inputs.update({'tests/vectors/' + name: 'tests/vectors/' + name for name in paths if name.endswith('.json')})
# Read every committed input before modifying the local snapshot.
blobs = {name: subprocess.check_output(['git', '-C', spec, 'show', commit + ':' + upstream])
         for name, upstream in inputs.items()}
for old in (root / 'tests/vectors').glob('*.json'):
    if str(old.relative_to(root)) not in blobs:
        old.unlink()
for name, raw in blobs.items():
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(raw)
(root / 'tests/vectors/SPEC_COMMIT').write_text(commit + '\n')
(root / 'tests/vectors/SPEC_SHA256').write_text(''.join(
    hashlib.sha256(raw).hexdigest() + '  ' + name + '\n' for name, raw in sorted(blobs.items())))
print('synced registry/vectors from SPEC ' + commit)
PY
