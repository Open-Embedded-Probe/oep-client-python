#!/bin/sh
# Copy the generated OEP v1 registry module from the sibling oep-spec checkout (never edit the copy).
set -e
here=$(cd "$(dirname "$0")/.." && pwd)
spec=${OEP_SPEC_DIR:-$here/../oep-spec}
cp "$spec/generated/oep-v1/oep_v1_registry.py" "$here/src/oep_client/registry.py"
echo "synced the registry from oep-spec $(git -C "$spec" rev-parse --short HEAD)"
