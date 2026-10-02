#!/bin/sh
# Copy the generated OEP v1 registry module and the test vectors from the sibling oep-spec checkout (never edit the
# copies): src/oep_client/registry.py and tests/vectors/*.json (tests/test_vectors.py reads them).
set -e
here=$(cd "$(dirname "$0")/.." && pwd)
spec=${OEP_SPEC_DIR:-$here/../oep-spec}
cp "$spec/generated/oep-v1/oep_v1_registry.py" "$here/src/oep_client/registry.py"
mkdir -p "$here/tests/vectors"
rm -f "$here/tests/vectors/"*.json
cp "$spec/tests/vectors/"*.json "$here/tests/vectors/"
echo "synced the registry and the test vectors from oep-spec $(git -C "$spec" rev-parse --short HEAD)"
